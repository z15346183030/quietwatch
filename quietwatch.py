#!/usr/bin/env python3
"""quietwatch - run a command only when file content actually changes.

One file, standard library only, no config file and no ignore patterns.

Most of the knobs in a file watcher exist to answer a single question:
"does this filesystem event count?".  Deriving the answer from content
hashes instead of event paths makes that question mostly disappear:

  * `touch` changes mtime but not content          -> no trigger
  * a directory mtime change is not a file        -> no trigger
  * an editor writes a swap file, then deletes it -> no trigger
  * a build command rewrites its own output files -> absorbed, no loop
  * several events from one save                  -> one trigger

Core logic (scan_tree / diff_states / Watcher.step) is pure with respect
to time: the caller injects `now`, so it can be tested and replayed
deterministically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

__version__ = "1.0.0"

# The only built-in path knowledge. Deliberately not configurable: these are
# directories that are never source input, and --explain names every one.
NOISE_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", "target", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
})

CHUNK = 64 * 1024
UNREADABLE = "<unreadable>"


class FileState(NamedTuple):
    size: int
    mtime_ns: int
    digest: str


class Change(NamedTuple):
    path: str
    kind: str          # added | removed | modified | touched
    counts: bool
    reason: str
    before: FileState | None
    after: FileState | None


class Trigger(NamedTuple):
    at: float
    index: int
    changes: tuple[Change, ...]


def hash_file(path: str) -> str:
    """SHA-256 of a file, streamed so large files never land in memory."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(CHUNK)
                if not block:
                    break
                h.update(block)
    except OSError:
        return UNREADABLE
    return h.hexdigest()


def scan_tree(root: Path, previous: dict[str, FileState] | None = None
              ) -> tuple[dict[str, FileState], dict[str, str]]:
    """Snapshot every regular file under root as (size, mtime_ns, digest).

    `previous` enables the two-level index: when size and mtime_ns both match
    an earlier snapshot the stored digest is reused and the file is not read.
    Returns (states, skipped) where skipped explains every exclusion.
    """
    if not root.is_dir():
        raise ValueError(f"不是目录或目录不存在：{root}")
    states: dict[str, FileState] = {}
    skipped: dict[str, str] = {}
    previous = previous or {}

    def visit(dirpath: str, prefix: str) -> None:
        # os.scandir already carries file metadata on Windows, so entry.stat()
        # avoids a separate syscall per file. os.walk + os.stat doubled that
        # cost and made a 1,791-file steady scan take 315 ms.
        try:
            with os.scandir(dirpath) as it:
                entries = list(it)
        except OSError as exc:
            skipped[prefix.rstrip("/") + "/"] = "scan-failed:" + type(exc).__name__
            return
        for entry in entries:
            rel = prefix + entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in NOISE_DIRS:
                        skipped[rel + "/"] = "noise-dir"
                    else:
                        visit(entry.path, rel + "/")
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                skipped[rel] = "stat-failed:" + type(exc).__name__
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped[rel] = "not-regular-file"
                continue
            old = previous.get(rel)
            if (old is not None and old.size == st.st_size
                    and old.mtime_ns == st.st_mtime_ns):
                states[rel] = old
                continue
            states[rel] = FileState(st.st_size, st.st_mtime_ns,
                                    hash_file(entry.path))

    visit(str(root), "")
    return states, skipped


def diff_states(before: dict[str, FileState],
                after: dict[str, FileState]) -> list[Change]:
    """Turn two snapshots into explained changes. Pure function."""
    out: list[Change] = []
    for rel in sorted(set(before) | set(after)):
        a, b = before.get(rel), after.get(rel)
        if a is None and b is not None:
            readable = b.digest != UNREADABLE
            out.append(Change(rel, "added", readable,
                              "新增文件" if readable else "新增文件但读不到内容，无法判定",
                              a, b))
        elif b is None and a is not None:
            out.append(Change(rel, "removed", True, "文件被删除", a, b))
        elif a is not None and b is not None and a.digest != b.digest:
            if UNREADABLE in (a.digest, b.digest):
                out.append(Change(rel, "modified", False,
                                  "内容不可读，无法判定，宁可不报", a, b))
            else:
                out.append(Change(rel, "modified", True, "内容哈希变化", a, b))
        elif (a is not None and b is not None
              and (a.size, a.mtime_ns) != (b.size, b.mtime_ns)):
            out.append(Change(rel, "touched", False,
                              "时间戳或大小变了，内容哈希相同", a, b))
    return out


class Watcher:
    """Content-based watcher with a settle window and post-run absorption.

    A trigger window remembers the snapshot from before the first event, not
    a list of events. At the end of the window we diff those two snapshots:
    a swap file created and deleted inside one save leaves no net change,
    and a file edited and then reverted leaves no net change either.
    """

    def __init__(self, root: Path, settle: float = 0.15):
        self.root = Path(root)
        self.settle = settle
        self.baseline, self.skipped = scan_tree(self.root)
        self.window_start: dict[str, FileState] | None = None
        self.last_change: float | None = None
        self.count = 0

    def step(self, now: float) -> tuple[Trigger | None, list[Change]]:
        """One observation at time `now`. Returns (trigger, all changes seen)."""
        snap, self.skipped = scan_tree(self.root, self.baseline)
        observed = diff_states(self.baseline, snap)
        real = [c for c in observed if c.counts]
        if real:
            if self.window_start is None:
                self.window_start = self.baseline
            self.last_change = now
        self.baseline = snap
        trigger = None
        # The replay clock advances in fractions of a second; without a tiny
        # epsilon, 1.35 - 1.20 lands at 0.149999... and fires one tick late.
        if (self.window_start is not None and self.last_change is not None
                and now - self.last_change >= self.settle - 1e-9):
            net = [c for c in diff_states(self.window_start, snap)
                   if c.counts]
            if net:
                self.count += 1
                trigger = Trigger(now, self.count, tuple(net))
            self.window_start = None
            self.last_change = None
        return trigger, observed

    def absorb(self) -> list[Change]:
        """Fold files produced by the command into the baseline.

        Without this, a build command that writes output triggers itself
        forever. This is the structural replacement for ignore rules.
        """
        snap, self.skipped = scan_tree(self.root, self.baseline)
        produced = [c for c in diff_states(self.baseline, snap) if c.counts]
        self.baseline = snap
        return produced


# ---------------------------------------------------------------------------
# Deterministic replay. A naive watcher is run alongside, so the report can
# show real numbers instead of adjectives. It reacts to any stat change and
# has no absorption, which is exactly the behaviour that needs ignore rules.
# ---------------------------------------------------------------------------

def naive_snapshot(root: Path) -> dict[str, object]:
    snap: dict[str, object] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        try:
            snap["d:" + rel_dir] = os.stat(dirpath).st_mtime_ns
        except OSError:
            pass
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                st = os.stat(full)
                snap["f:" + rel] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return snap


class NaiveWatcher:
    def __init__(self, root: Path):
        self.root = root
        self.prev = naive_snapshot(root)
        self.count = 0
        self.output_events = 0

    def step(self, produced: bool = False) -> bool:
        cur = naive_snapshot(self.root)
        fired = cur != self.prev
        if fired:
            self.count += 1
            if produced:
                self.output_events += 1
            self.prev = cur
        return fired


def parse_replay(text: str) -> list[tuple[int, str, list[str]]]:
    actions: list[tuple[int, str, list[str]]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        verb = parts[0].lower()
        if verb not in ("write", "append", "save", "touch", "delete", "wait"):
            raise ValueError(f"回放脚本第 {lineno} 行：不认识的动作 {verb!r}")
        need = 1 if verb in ("wait", "delete") else 2
        if len(parts) - 1 < need:
            raise ValueError(
                f"回放脚本第 {lineno} 行：{verb} 需要 {need} 个参数，"
                f"写法见 README 的回放脚本一节")
        actions.append((lineno, verb, parts[1:]))
    return actions


class Replay:
    """Runs a script against a real temporary directory and reports the bill."""

    def __init__(self, root: Path, settle: float, make_output: bool = True):
        self.root = root
        self.watcher = Watcher(root, settle)
        self.naive = NaiveWatcher(root)
        self.make_output = make_output
        self.clock = 0.0
        self.fs_events = 0
        self.content_changes = 0
        self.self_inflicted = 0
        self.absorbed: list[Change] = []
        self.triggers: list[Trigger] = []
        self.lines: list[str] = []

    def tick(self, action: bool = False) -> None:
        trigger, observed = self.watcher.step(self.clock)
        self.naive.step()
        if action:
            self.fs_events += 1
        for change in observed:
            mark = "观察" if change.counts else "忽略"
            if change.counts:
                self.content_changes += 1
            self.lines.append(
                f"      {mark}  {change.kind:<8} {change.path:<24} "
                f"{change.reason}")
        if trigger:
            self.triggers.append(trigger)
            paths = ", ".join(c.path for c in trigger.changes)
            self.lines.append(
                f"      → 触发 #{trigger.index}  t={trigger.at:.2f}s  "
                f"{len(trigger.changes)} 个内容变化  [{paths}]")
            if self.make_output:
                out = self.root / "build" / "out.txt"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(f"build #{trigger.index}\n", encoding="utf-8")
                self.naive.step(produced=True)
                produced = self.watcher.absorb()
                self.absorbed.extend(produced)
                names = ", ".join(c.path for c in produced) or "无"
                self.lines.append(
                    f"      → 吸收  命令产出 {names}，并入基线，不再触发")
                extra, _ = self.watcher.step(self.clock)
                if extra:
                    self.self_inflicted += 1

    def apply(self, verb: str, args: list[str]) -> None:
        root = self.root
        if verb == "wait":
            seconds = float(args[0])
            if seconds < 0:
                raise ValueError("wait 的秒数不能为负")
            steps = max(1, int(round(seconds / 0.05)))
            start = self.clock
            for i in range(1, steps + 1):
                self.clock = start + seconds * i / steps
                self.tick(action=False)
            return
        rel = args[0]
        text = args[1] if len(args) > 1 else ""
        path = root / rel
        if verb == "delete":
            if path.exists():
                path.unlink()
            self.tick(action=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if verb == "touch":
            if not path.exists():
                path.write_text(text, encoding="utf-8")
            os.utime(path, None)
            self.tick(action=True)
            return
        if verb == "save":
            # One editor save = swap file written, target rewritten, swap
            # removed, directory mtime bumped. Four real filesystem events.
            swap = root / (rel + ".swp")
            swap.write_text("swap", encoding="utf-8")
            self.tick(action=True)
            path.write_text(text, encoding="utf-8")
            self.tick(action=True)
            swap.unlink()
            self.tick(action=True)
            os.utime(path.parent, None)
            self.tick(action=True)
            return
        mode = "a" if verb == "append" else "w"
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(text)
        self.tick(action=True)


def run_replay(script: Path, settle: float, as_json: bool) -> int:
    text = script.read_text(encoding="utf-8")
    actions = parse_replay(text)
    with tempfile.TemporaryDirectory(prefix="quietwatch-") as tmp:
        root = Path(tmp)
        replay = Replay(root, settle)
        for lineno, verb, args in actions:
            label = f"{verb} {args[0]}" if verb != "wait" else f"wait {args[0]}s"
            replay.lines.append(f" [{lineno:>2}] {label:<32}")
            try:
                replay.apply(verb, args)
            except (OSError, ValueError) as exc:
                raise ValueError(f"回放脚本第 {lineno} 行执行失败：{exc}") from exc
        net_changes = sum(len(t.changes) for t in replay.triggers)
        report = {
            "instructions": len(actions),
            "filesystem_events": replay.fs_events,
            "observed_content_changes": replay.content_changes,
            "net_content_changes": net_changes,
            "triggers": len(replay.triggers),
            "naive_triggers": replay.naive.count,
            "naive_output_triggers": replay.naive.output_events,
            "self_inflicted": replay.self_inflicted,
            "absorbed": len(replay.absorbed),
            "settle": settle,
        }
        if as_json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f"quietwatch {__version__} · 回放验证 · "
                  f"settle={settle:.2f}s · {len(actions)} 条指令")
            print()
            for line in replay.lines:
                print(line)
            print()
            print("──────────────────────────────────────────────")
            print(f"指令                    {len(actions)}")
            print(f"文件系统动作            {replay.fs_events}")
            print(f"观察到的内容变化         {replay.content_changes}")
            print(f"去抖后的净内容变化       {net_changes}")
            print(f"实际触发                {len(replay.triggers)}")
            print(f"朴素事件监听触发         {replay.naive.count}"
                  f"（其中 {replay.naive.output_events} 次来自命令自己的产物）")
            print(f"命令产物自激            {replay.self_inflicted}")
            print(f"已吸收的命令产物         {len(replay.absorbed)}")
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def describe_change(change: Change) -> str:
    sizes = ""
    if change.before and change.after:
        sizes = f" ({change.before.size} → {change.after.size} B)"
    return f"{change.kind:<8} {change.path}{sizes}  {change.reason}"


def run_command(command: list[str], trigger: Trigger) -> int:
    env = dict(os.environ)
    env["QW_CHANGED"] = ",".join(c.path for c in trigger.changes)
    env["QW_TRIGGER"] = str(trigger.index)
    try:
        cp = subprocess.run(command, env=env, check=False)
        return cp.returncode
    except FileNotFoundError:
        print(f"找不到命令 {command[0]!r}。检查拼写，或改用可执行文件的完整路径。",
              file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"命令没能启动：{exc}", file=sys.stderr)
        return 126


def watch(args: argparse.Namespace, command: list[str]) -> int:
    watcher = Watcher(Path(args.path), args.settle)
    stamp = time.strftime("%H:%M:%S")
    if args.json:
        emit({"type": "baseline", "root": str(Path(args.path).resolve()),
              "files": len(watcher.baseline), "skipped": watcher.skipped,
              "settle": args.settle})
    else:
        print(f"quietwatch {__version__} · 基线 {len(watcher.baseline)} 个文件 · "
              f"跳过 {len(watcher.skipped)} 个噪声目录 · settle={args.settle:.2f}s")
        if args.explain and watcher.skipped:
            for rel, why in sorted(watcher.skipped.items()):
                if args.json:
                    emit({"type": "skipped", "path": rel, "reason": why})
                else:
                    print(f"  [{stamp}] 跳过 {rel}  {why}")

    if args.once:
        if command and not args.dry_run:
            code = run_command(command, Trigger(time.monotonic(), 0, ()))
            watcher.absorb()
            if not args.json:
                print(f"  [{stamp}] 运行一次 → 退出码 {code}")
        else:
            if not args.json:
                print(f"  [{stamp}] 基线已建立；没有指定命令，只做观察。")
        return 0

    try:
        while True:
            now = time.monotonic()
            trigger, observed = watcher.step(now)
            if args.explain:
                for change in observed:
                    mark = "计入" if change.counts else "忽略"
                    if args.json:
                        emit({"type": "observe", "counts": change.counts,
                              "path": change.path, "kind": change.kind,
                              "reason": change.reason})
                    else:
                        print(f"  [{time.strftime('%H:%M:%S')}] {mark} "
                              f"{describe_change(change)}")
            if trigger:
                paths = ", ".join(c.path for c in trigger.changes)
                if args.json:
                    emit({"type": "trigger", "index": trigger.index,
                          "paths": [c.path for c in trigger.changes]})
                else:
                    print(f"  [{time.strftime('%H:%M:%S')}] 触发 #{trigger.index} "
                          f"{len(trigger.changes)} 个内容变化  [{paths}]")
                if command:
                    if args.dry_run:
                        if not args.json:
                            print(f"  [{time.strftime('%H:%M:%S')}] 试运行，"
                                  f"不执行：{' '.join(shlex.quote(c) for c in command)}")
                    else:
                        started = time.monotonic()
                        code = run_command(command, trigger)
                        produced = watcher.absorb()
                        if args.json:
                            emit({"type": "absorb",
                                  "paths": [c.path for c in produced],
                                  "exit_code": code})
                        elif args.explain:
                            print(f"  [{time.strftime('%H:%M:%S')}] 命令退出码 {code} "
                                  f"({time.monotonic() - started:.2f}s)，"
                                  f"吸收 {len(produced)} 个命令产物")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.json:
            print("\n停止监听。")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quietwatch",
        description="文件变化只认内容：一次保存只跑一次命令，不需要写忽略规则。",
        epilog=("示例：python quietwatch.py . -- python -m unittest\n"
                "      python quietwatch.py --replay examples/save-replay.txt"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", default=".", help="要监听的目录，默认当前目录")
    p.add_argument("--interval", type=float, default=0.20, metavar="SEC",
                   help="轮询间隔，默认 0.20 秒")
    p.add_argument("--settle", type=float, default=0.15, metavar="SEC",
                   help="去抖窗口：最后一次内容变化后静默多久才触发，默认 0.15 秒")
    p.add_argument("--once", action="store_true",
                   help="只建立基线并执行命令一次，然后退出")
    p.add_argument("--explain", action="store_true",
                   help="打印每一次判定：为什么计入、为什么忽略")
    p.add_argument("--json", action="store_true", help="输出 JSON Lines")
    p.add_argument("--dry-run", action="store_true", help="打印会执行的命令但不执行")
    p.add_argument("--replay", metavar="FILE",
                   help="回放脚本，输出可复现的触发账单")
    p.add_argument("--version", action="version", version="quietwatch " + __version__)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        cut = argv.index("--")
        tool_args, command = argv[:cut], argv[cut + 1:]
    else:
        tool_args, command = argv, []
    args = build_parser().parse_args(tool_args)
    if args.interval <= 0 or args.settle < 0:
        print("--interval 必须大于 0，--settle 不能为负。", file=sys.stderr)
        return 2
    try:
        if args.replay:
            return run_replay(Path(args.replay), args.settle, args.json)
        return watch(args, command)
    except (ValueError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
