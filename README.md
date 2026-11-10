# quietwatch

**A file watcher that reacts to content, not events.**

One editor save produces a burst of filesystem noise: a swap file appears, the
target is rewritten, the swap disappears, the directory timestamp changes.
Most watchers turn that into several runs, so you end up writing ignore rules
to explain which of those events "count". quietwatch answers the question with
a hash instead: if the bytes did not change, nothing happened.

**Zero dependencies. Single file. Pure Python stdlib. No config file, no
ignore patterns, no plugin system.**

## Why

A watcher has to answer one question over and over: *does this change count?*

The usual answer is a stack of path filters, glob rules and ignore files. That
works, but it puts the burden on the user: every new tool in the pipeline
writes output files, and every one of those needs a rule, or the command
triggers itself forever.

quietwatch makes three decisions instead:

- **Content is the only signal.** `touch`, directory timestamp changes and
  short-lived swap files cannot trigger anything, because their bytes are
  either unchanged or gone by the time the window closes.
- **A window is diffed, not a list of events.** At the end of the settle
  window the snapshot from before the first event is compared with the
  snapshot now. A file edited and reverted leaves no net change; a swap file
  created and deleted leaves no net change.
- **Command output is absorbed.** After the command finishes, its own output
  files are folded into the baseline. A build that writes `build/out.txt`
  does not start a second run. This is the structural reason ignore rules
  are not needed.

## Install

Copy the file. That is the install.

```
curl -O https://raw.githubusercontent.com/z15346183030/quietwatch/main/quietwatch.py
python quietwatch.py --help
```

Requires Python 3.10 or newer. Nothing to pip install.

## Usage

Watch the current directory and run tests when source content changes:

```
python quietwatch.py . -- python -m unittest discover -s tests
```

Add `--explain` to see every decision. This is real output from a real run,
with `--dry-run` so the command is printed instead of executed:

```
python quietwatch.py --interval 0.05 --settle 0.10 --explain --dry-run . -- echo BUILD
```

```

quietwatch 1.0.0 · 基线 1 个文件 · 跳过 0 个噪声目录 · settle=0.10s
  [13:31:31] 计入 modified a.txt (2 → 2 B)  内容哈希变化
  [13:31:31] 触发 #1 1 个内容变化  [a.txt]
  [13:31:31] 试运行，不执行：echo BUILD
```

Note the `(2 → 2 B)`: the file was rewritten with the same length. Size alone
would have missed it; the hash did not.

No command given? It only observes — useful for checking what a build does:

```
python quietwatch.py --explain .
```

Run the command once and exit (baseline + one run, no watch loop):

```
python quietwatch.py --once --explain .
```

```

quietwatch 1.0.0 · 基线 7 个文件 · 跳过 1 个噪声目录 · settle=0.15s
  [13:28:01] 跳过 __pycache__/  noise-dir
  [13:28:01] 基线已建立；没有指定命令，只做观察。
```

Machine-readable output, one JSON object per line. `--replay --json` prints
a single summary object instead:

```
python quietwatch.py --replay examples/save-replay.txt --json
```

```

{"instructions": 11, "filesystem_events": 9, "observed_content_changes": 7, "net_content_changes": 3, "triggers": 3, "naive_triggers": 12, "naive_output_triggers": 3, "self_inflicted": 0, "absorbed": 3, "settle": 0.15}
```

The command receives two environment variables: `QW_CHANGED` (comma-separated
paths) and `QW_TRIGGER` (the 1-based trigger number).

## Replay: reproducible proof

`--replay` runs a scripted sequence against a real temporary directory and
prints the bill. A naive watcher — one that fires on any stat change and has
no absorption — runs alongside for comparison.

```
python quietwatch.py --replay examples/save-replay.txt
```

Real output:

```

quietwatch 1.0.0 · 回放验证 · settle=0.15s · 11 条指令

 [ 3] save src/app.py
      观察  added    src/app.py.swp           新增文件
      观察  added    src/app.py               新增文件
      观察  removed  src/app.py.swp           文件被删除
 [ 4] wait 0.30s
      → 触发 #1  t=0.15s  1 个内容变化  [src/app.py]
      → 吸收  命令产出 build/out.txt，并入基线，不再触发
 [ 7] touch src/app.py
      忽略  touched  src/app.py               时间戳或大小变了，内容哈希相同
 [ 8] wait 0.30s
 [11] write src/app.py
      观察  modified src/app.py               内容哈希变化
 [12] wait 0.30s
      → 触发 #2  t=0.75s  1 个内容变化  [src/app.py]
      → 吸收  命令产出 build/out.txt，并入基线，不再触发
 [15] write src/app.py
      观察  modified src/app.py               内容哈希变化
 [16] write src/app.py
      观察  modified src/app.py               内容哈希变化
 [17] wait 0.30s
 [20] delete src/app.py
      观察  removed  src/app.py               文件被删除
 [21] wait 0.30s
      → 触发 #3  t=1.35s  1 个内容变化  [src/app.py]
      → 吸收  命令产出 build/out.txt，并入基线，不再触发

──────────────────────────────────────────────
指令                    11
文件系统动作            9
观察到的内容变化         7
去抖后的净内容变化       3
实际触发                3
朴素事件监听触发         12（其中 3 次来自命令自己的产物）
命令产物自激            0
已吸收的命令产物         3
```

Seven observed content changes collapse to three net changes and three runs.
The naive watcher fires twelve times, three of them caused by the command's
own output. Note lines 15–16: the file is changed and changed back inside one
window, so it produces no trigger at all.

Script syntax — one action per line, `#` starts a comment:

| Action | Meaning |
|---|---|
| `write PATH "TEXT"` | write content |
| `append PATH "TEXT"` | append content |
| `save PATH "TEXT"` | simulate an editor save (4 filesystem actions) |
| `touch PATH "TEXT"` | change the timestamp only |
| `delete PATH` | remove a file |
| `wait SECONDS` | advance the virtual clock |

`--replay --json` prints the summary as a single JSON object, suitable for CI.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--interval SEC` | `0.20` | polling interval |
| `--settle SEC` | `0.15` | quiet period after the last content change |
| `--once` | off | build the baseline, run once, exit |
| `--explain` | off | print every decision and its reason |
| `--json` | off | JSON Lines output |
| `--dry-run` | off | print the command instead of running it |
| `--replay FILE` | — | run a scripted replay and print the bill |

That is the whole configuration surface. There is deliberately no `--ignore`,
no glob filter and no config file.

## How it works

```
   scan_tree(root, previous)
        │  os.scandir -> (size, mtime_ns, sha256)
        │  reuse the stored digest when size + mtime_ns are unchanged
        ▼
   diff_states(before, after)
        │  added / removed / modified  -> counts
        │  touched (same digest)       -> ignored
        ▼
   Watcher.step(now)
        │  remember the snapshot from before the FIRST event of a window
        │  after `settle` seconds of quiet, diff the two snapshots
        ▼
   Trigger  ->  run command  ->  absorb()  ->  back to observing
```

`scan_tree`, `diff_states` and `Watcher.step` are time-independent: the caller
supplies `now`, so the whole pipeline is testable without sleeping. The first
scan necessarily hashes every file; steady scans only stat, and a file is only
re-hashed when size or mtime changes.

Fourteen directory names (`.git`, `__pycache__`, `node_modules`, …) are
skipped as never-source input. `--explain` names every one it skipped. This is
a fixed list, not a rule language.

## Measured numbers

Measured on Windows with Python 3.13, on the CPython 3.13 standard library
(1,791 files, 147.18 MB, 109 skipped directories):

| What | Number |
|---|---|
| First scan (full hash) | 3.41 s |
| Peak Python heap during first scan | 0.74 MB |
| Steady scan, median of 5 | 55.4 ms |
| Re-hashes during a steady scan | 0 |
| `Watcher.step`, median of 5 | 69.3 ms |
| Hashing one 8 MB file | 0.032 s, 136.7 KB peak heap |

The steady scan was 315.4 ms before switching from `os.walk` + `os.stat` to
`os.scandir` with `DirEntry.stat()`, a 5.7× improvement.

Replay numbers above come from `examples/save-replay.txt`: 9 filesystem
actions, 3 triggers, 0 self-inflicted triggers, versus 12 for the naive
watcher.

## Known limits

Be aware of these before choosing it for a big tree:

- **It polls.** Reaction latency is `interval + settle` (0.35 s by default),
  and a steady scan costs about 55 ms per 1,791 files. On a very large tree,
  watch a narrower subdirectory or raise `--interval`. Native event APIs are
  faster and idle cheaper; this trade buys portability and a much simpler
  decision model.
- **First scan reads everything.** 147 MB took 3.41 s. Hashing is streamed in
  64 KB chunks, so memory stays flat.
- **Same size and same mtime means "unchanged".** If a file is rewritten with
  identical size and its timestamp is deliberately restored, the change is
  invisible. That is the price of not reading every file on every scan.
- **Changes made while the command runs are absorbed** together with the
  command's output. `--explain` reports how many paths were absorbed.
- No process groups, signal forwarding, restart mode or shell pipelines.
  Pass a command as separate arguments; shell metacharacters belong to your
  shell, not to quietwatch.

## Tests

```
python -m unittest discover -s tests
```

82 tests, all passing. They cover the two-level index, every change kind,
settle-window coalescing, swap-file and revert cancellation, command-output
absorption, replay parsing, the CLI, and the regressions found while building
this (float-boundary settle, `delete` argument count, JSON-only output).

## License

MIT. See [LICENSE](LICENSE).
