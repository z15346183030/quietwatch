"""quietwatch 的回归测试。全部用临时目录，不碰真实工作区。"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quietwatch as qw  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def fs(size=10, mtime=1000, digest="a" * 64):
    return qw.FileState(size, mtime, digest)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qw-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, rel, text="hello"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def bump_mtime(self, path, seconds=2):
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


class HashFileTests(TempDirCase):
    def test_known_digest(self):
        p = self.put("a.txt", "abc")
        self.assertEqual(qw.hash_file(str(p)), hashlib.sha256(b"abc").hexdigest())

    def test_empty_file_digest(self):
        p = self.put("a.txt", "")
        self.assertEqual(qw.hash_file(str(p)), hashlib.sha256(b"").hexdigest())

    def test_streams_file_larger_than_chunk(self):
        payload = "x" * (qw.CHUNK * 3 + 17)
        p = self.put("big.txt", payload)
        self.assertEqual(qw.hash_file(str(p)),
                         hashlib.sha256(payload.encode()).hexdigest())

    def test_missing_file_is_unreadable(self):
        self.assertEqual(qw.hash_file(str(self.root / "nope")), qw.UNREADABLE)

    def test_unicode_content(self):
        p = self.put("zh.txt", "中文内容")
        self.assertEqual(qw.hash_file(str(p)),
                         hashlib.sha256("中文内容".encode()).hexdigest())


class ScanTreeTests(TempDirCase):
    def test_empty_directory(self):
        states, skipped = qw.scan_tree(self.root)
        self.assertEqual(states, {})
        self.assertEqual(skipped, {})

    def test_single_file(self):
        self.put("a.txt", "hello")
        states, _ = qw.scan_tree(self.root)
        self.assertEqual(list(states), ["a.txt"])
        self.assertEqual(states["a.txt"].size, 5)
        self.assertEqual(len(states["a.txt"].digest), 64)

    def test_nested_directories(self):
        self.put("a/b/c.txt", "x")
        states, _ = qw.scan_tree(self.root)
        self.assertEqual(list(states), ["a/b/c.txt"])

    def test_paths_use_forward_slash(self):
        self.put("a/b.txt", "x")
        states, _ = qw.scan_tree(self.root)
        self.assertIn("a/b.txt", states)

    def test_chinese_filename(self):
        self.put("文档/说明.md", "中文")
        states, _ = qw.scan_tree(self.root)
        self.assertIn("文档/说明.md", states)

    def test_noise_directory_skipped(self):
        self.put(".git/config", "x")
        self.put("src/app.py", "y")
        states, skipped = qw.scan_tree(self.root)
        self.assertEqual(list(states), ["src/app.py"])
        self.assertEqual(skipped.get(".git/"), "noise-dir")

    def test_all_builtin_noise_dirs_skipped(self):
        for name in qw.NOISE_DIRS:
            self.put(name + "/f.txt", "x")
        states, skipped = qw.scan_tree(self.root)
        self.assertEqual(states, {})
        self.assertEqual(len(skipped), len(qw.NOISE_DIRS))

    def test_previous_reuses_digest_without_reading(self):
        self.put("a.txt", "hello")
        first, _ = qw.scan_tree(self.root)
        with patch.object(qw, "hash_file", wraps=qw.hash_file) as spy:
            second, _ = qw.scan_tree(self.root, first)
            self.assertEqual(spy.call_count, 0)
        self.assertEqual(first, second)

    def test_size_change_forces_rehash(self):
        p = self.put("a.txt", "hello")
        first, _ = qw.scan_tree(self.root)
        p.write_text("hello world", encoding="utf-8")
        second, _ = qw.scan_tree(self.root, first)
        self.assertNotEqual(first["a.txt"].digest, second["a.txt"].digest)

    def test_mtime_change_forces_rehash(self):
        p = self.put("a.txt", "hello")
        first, _ = qw.scan_tree(self.root)
        self.bump_mtime(p)
        with patch.object(qw, "hash_file", wraps=qw.hash_file) as spy:
            second, _ = qw.scan_tree(self.root, first)
            self.assertEqual(spy.call_count, 1)
        self.assertEqual(first["a.txt"].digest, second["a.txt"].digest)

    def test_new_file_appears(self):
        first, _ = qw.scan_tree(self.root)
        self.put("a.txt", "hello")
        second, _ = qw.scan_tree(self.root, first)
        self.assertIn("a.txt", second)

    def test_deleted_file_disappears(self):
        p = self.put("a.txt", "hello")
        first, _ = qw.scan_tree(self.root)
        p.unlink()
        second, _ = qw.scan_tree(self.root, first)
        self.assertNotIn("a.txt", second)

    def test_non_directory_raises_friendly_error(self):
        with self.assertRaisesRegex(ValueError, "不是目录"):
            qw.scan_tree(self.root / "nope")

    def test_skipped_reports_non_regular_files(self):
        (self.root / "link").mkdir()
        _, skipped = qw.scan_tree(self.root)
        self.assertEqual(skipped, {})


class DiffStatesTests(unittest.TestCase):
    def test_both_empty(self):
        self.assertEqual(qw.diff_states({}, {}), [])

    def test_added(self):
        out = qw.diff_states({}, {"a": fs()})
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].kind, out[0].counts), ("added", True))

    def test_removed(self):
        out = qw.diff_states({"a": fs()}, {})
        self.assertEqual((out[0].kind, out[0].counts), ("removed", True))

    def test_modified(self):
        out = qw.diff_states({"a": fs(digest="a" * 64)},
                             {"a": fs(digest="b" * 64)})
        self.assertEqual((out[0].kind, out[0].counts), ("modified", True))

    def test_touched_is_not_counted(self):
        out = qw.diff_states({"a": fs(mtime=1)}, {"a": fs(mtime=2)})
        self.assertEqual((out[0].kind, out[0].counts), ("touched", False))

    def test_size_only_change_is_touched(self):
        out = qw.diff_states({"a": fs(size=1)}, {"a": fs(size=2)})
        self.assertEqual(out[0].kind, "touched")

    def test_identical_states_produce_no_changes(self):
        self.assertEqual(qw.diff_states({"a": fs()}, {"a": fs()}), [])

    def test_added_unreadable_not_counted(self):
        out = qw.diff_states({}, {"a": fs(digest=qw.UNREADABLE)})
        self.assertFalse(out[0].counts)

    def test_removed_unreadable_is_counted(self):
        out = qw.diff_states({"a": fs(digest=qw.UNREADABLE)}, {})
        self.assertTrue(out[0].counts)

    def test_modified_unreadable_not_counted(self):
        out = qw.diff_states({"a": fs(digest=qw.UNREADABLE)},
                             {"a": fs(digest="b" * 64)})
        self.assertFalse(out[0].counts)

    def test_results_sorted_by_path(self):
        out = qw.diff_states({}, {"z": fs(), "a": fs(), "m": fs()})
        self.assertEqual([c.path for c in out], ["a", "m", "z"])

    def test_mixed_kinds(self):
        out = qw.diff_states(
            {"keep": fs(), "gone": fs(), "touch": fs(mtime=1)},
            {"keep": fs(), "touch": fs(mtime=2), "new": fs()})
        self.assertEqual({c.path: c.kind for c in out},
                         {"gone": "removed", "touch": "touched", "new": "added"})

    def test_reason_is_human_readable(self):
        out = qw.diff_states({"a": fs(mtime=1)}, {"a": fs(mtime=2)})
        self.assertIn("内容哈希相同", out[0].reason)

    def test_digest_change_with_same_size_counts(self):
        out = qw.diff_states({"a": fs(size=5, digest="a" * 64)},
                             {"a": fs(size=5, digest="b" * 64)})
        self.assertEqual(out[0].kind, "modified")


class WatcherTests(TempDirCase):
    def test_calm_directory_never_triggers(self):
        w = qw.Watcher(self.root, 0.10)
        for t in (1.0, 2.0, 3.0):
            trig, _ = w.step(t)
            self.assertIsNone(trig)

    def test_new_file_triggers_after_settle(self):
        w = qw.Watcher(self.root, 0.10)
        self.put("a.txt", "hello")
        trig, _ = w.step(0.0)
        self.assertIsNone(trig)
        trig, _ = w.step(1.0)
        self.assertIsNotNone(trig)
        self.assertEqual([c.path for c in trig.changes], ["a.txt"])

    def test_no_trigger_before_settle(self):
        w = qw.Watcher(self.root, 0.50)
        self.put("a.txt", "hello")
        self.assertIsNone(w.step(0.0)[0])
        self.assertIsNone(w.step(0.49)[0])
        self.assertIsNotNone(w.step(0.50)[0])

    def test_burst_coalesces_into_single_trigger(self):
        w = qw.Watcher(self.root, 0.10)
        for i in range(5):
            self.put(f"f{i}.txt", str(i))
            w.step(float(i) / 100)
        trig, _ = w.step(1.0)
        self.assertIsNotNone(trig)
        self.assertEqual(len(trig.changes), 5)
        self.assertEqual(w.count, 1)

    def test_swap_file_leaves_no_net_change(self):
        """回归：编辑器保存的临时文件新增又删除，不该出现在触发里。"""
        w = qw.Watcher(self.root, 0.10)
        (self.root / "a.py.swp").write_text("swap", encoding="utf-8")
        w.step(0.0)
        self.put("a.py", "code")
        w.step(0.0)
        (self.root / "a.py.swp").unlink()
        w.step(0.0)
        trig, _ = w.step(1.0)
        self.assertIsNotNone(trig)
        self.assertEqual([c.path for c in trig.changes], ["a.py"])

    def test_revert_within_window_produces_no_trigger(self):
        """回归：改完又改回原内容，窗口首尾没有净变化。"""
        p = self.put("a.py", "v1")
        w = qw.Watcher(self.root, 0.10)
        p.write_text("v2", encoding="utf-8")
        w.step(0.0)
        p.write_text("v1", encoding="utf-8")
        w.step(0.0)
        self.assertIsNone(w.step(1.0)[0])
        self.assertEqual(w.count, 0)

    def test_touch_does_not_trigger(self):
        """回归：只改时间戳，内容哈希相同。"""
        p = self.put("a.txt", "hello")
        w = qw.Watcher(self.root, 0.10)
        self.bump_mtime(p)
        _, observed = w.step(1.0)
        self.assertTrue(any(c.kind == "touched" for c in observed))
        self.assertIsNone(w.step(2.0)[0])
        self.assertEqual(w.count, 0)

    def test_directory_mtime_does_not_trigger(self):
        """回归：目录事件不是文件事件。"""
        w = qw.Watcher(self.root, 0.10)
        self.bump_mtime(self.root)
        trig, observed = w.step(1.0)
        self.assertIsNone(trig)
        self.assertEqual(observed, [])
        self.assertIsNone(w.step(2.0)[0])

    def test_delete_triggers(self):
        p = self.put("a.txt", "hello")
        w = qw.Watcher(self.root, 0.10)
        p.unlink()
        w.step(0.0)
        trig, _ = w.step(1.0)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.changes[0].kind, "removed")

    def test_absorb_prevents_self_trigger(self):
        """回归：命令自己写出的产物并入基线，不引发下一轮。"""
        p = self.put("a.py", "v1")
        w = qw.Watcher(self.root, 0.10)
        p.write_text("v2", encoding="utf-8")
        w.step(0.0)
        self.assertIsNotNone(w.step(1.0)[0])
        self.put("build/out.txt", "built")
        produced = w.absorb()
        self.assertEqual([c.path for c in produced], ["build/out.txt"])
        self.assertIsNone(w.step(2.0)[0])
        self.assertEqual(w.count, 1)

    def test_absorb_on_quiet_dir_returns_empty(self):
        w = qw.Watcher(self.root, 0.10)
        self.assertEqual(w.absorb(), [])

    def test_trigger_index_increments(self):
        w = qw.Watcher(self.root, 0.10)
        for i in range(3):
            self.put("a.txt", f"v{i}")
            w.step(float(i))
            trig, _ = w.step(float(i) + 0.5)
            self.assertEqual(trig.index, i + 1)
        self.assertEqual(w.count, 3)

    def test_noise_directory_never_triggers(self):
        w = qw.Watcher(self.root, 0.10)
        self.put(".git/config", "x")
        self.assertIsNone(w.step(0.0)[0])
        self.assertIsNone(w.step(1.0)[0])

    def test_settle_boundary_tolerates_float_error(self):
        """回归：虚拟时钟的浮点误差曾让触发晚整整一个 tick。"""
        w = qw.Watcher(self.root, 0.15)
        self.put("a.txt", "hello")
        self.assertIsNone(w.step(1.20)[0])
        self.assertIsNotNone(w.step(1.20 + 0.15)[0])

    def test_zero_settle_fires_immediately(self):
        w = qw.Watcher(self.root, 0.0)
        self.put("a.txt", "hello")
        self.assertIsNotNone(w.step(0.0)[0])

    def test_changes_are_net_states_not_accumulated_events(self):
        p = self.put("a.txt", "v1")
        w = qw.Watcher(self.root, 0.10)
        for value in ("v2", "v3", "v4"):
            p.write_text(value, encoding="utf-8")
            w.step(0.0)
        trig, _ = w.step(1.0)
        self.assertEqual(len(trig.changes), 1)
        self.assertEqual(trig.changes[0].kind, "modified")

    def test_multiple_separate_windows(self):
        w = qw.Watcher(self.root, 0.10)
        self.put("a.txt", "v1")
        w.step(0.0)
        self.assertIsNotNone(w.step(1.0)[0])
        (self.root / "a.txt").write_text("v2", encoding="utf-8")
        w.step(2.0)
        self.assertIsNotNone(w.step(3.0)[0])
        self.assertEqual(w.count, 2)


class NaiveWatcherTests(TempDirCase):
    def test_calm_directory(self):
        n = qw.NaiveWatcher(self.root)
        self.assertFalse(n.step())
        self.assertEqual(n.count, 0)

    def test_stat_change_fires(self):
        self.put("a.txt", "x")
        n = qw.NaiveWatcher(self.root)
        self.put("a.txt", "y")
        self.assertTrue(n.step())

    def test_output_events_are_counted(self):
        n = qw.NaiveWatcher(self.root)
        self.put("build/out.txt", "x")
        n.step(produced=True)
        self.assertEqual(n.output_events, 1)

    def test_snapshot_includes_directory_mtime(self):
        snap = qw.naive_snapshot(self.root)
        self.assertIn("d:.", snap)


class ParseReplayTests(unittest.TestCase):
    def test_comments_and_blank_lines(self):
        self.assertEqual(qw.parse_replay("# hi\n\n  \n"), [])

    def test_every_verb(self):
        out = qw.parse_replay(
            'write a "x"\nappend a "y"\nsave a "z"\ntouch a "z"\n'
            'delete a\nwait 0.3\n')
        self.assertEqual([v for _, v, _ in out],
                         ["write", "append", "save", "touch", "delete", "wait"])

    def test_quoted_text_with_spaces(self):
        out = qw.parse_replay('write a.py "print(1 + 2)"\n')
        self.assertEqual(out[0][2][1], "print(1 + 2)")

    def test_inline_comment(self):
        out = qw.parse_replay('wait 0.3 # settle\n')
        self.assertEqual(out[0][2], ["0.3"])

    def test_unknown_verb_raises(self):
        with self.assertRaisesRegex(ValueError, "不认识的动作"):
            qw.parse_replay("reboot a b\n")

    def test_missing_args_raises(self):
        with self.assertRaisesRegex(ValueError, "需要 2 个参数"):
            qw.parse_replay("write a.py\n")

    def test_delete_takes_one_argument(self):
        """回归：delete 曾因参数个数校验过严而无法解析。"""
        out = qw.parse_replay("delete a.py\n")
        self.assertEqual(out[0][2], ["a.py"])

    def test_line_numbers_are_kept(self):
        out = qw.parse_replay("# c\n# c\nwait 0.1\n")
        self.assertEqual(out[0][0], 3)

    def test_negative_wait_raises(self):
        with tempfile.TemporaryDirectory() as td:
            r = qw.Replay(Path(td), 0.15)
            with self.assertRaisesRegex(ValueError, "不能为负"):
                r.apply("wait", ["-1"])

    def test_verbs_are_case_insensitive(self):
        out = qw.parse_replay('WRITE a "x"\n')
        self.assertEqual(out[0][1], "write")


class ReplayTests(unittest.TestCase):
    def script(self):
        return EXAMPLES / "save-replay.txt"

    def test_example_script_reports_three_triggers(self):
        with tempfile.TemporaryDirectory() as td:
            r = qw.Replay(Path(td), 0.15)
            for lineno, verb, args in qw.parse_replay(
                    self.script().read_text(encoding="utf-8")):
                r.apply(verb, args)
            self.assertEqual(len(r.triggers), 3)
            self.assertEqual(sum(len(t.changes) for t in r.triggers), 3)

    def test_no_self_inflicted_triggers(self):
        with tempfile.TemporaryDirectory() as td:
            r = qw.Replay(Path(td), 0.15)
            for _, verb, args in qw.parse_replay(
                    self.script().read_text(encoding="utf-8")):
                r.apply(verb, args)
            self.assertEqual(r.self_inflicted, 0)
            self.assertEqual(len(r.absorbed), 3)

    def test_naive_watcher_fires_more_often(self):
        with tempfile.TemporaryDirectory() as td:
            r = qw.Replay(Path(td), 0.15)
            for _, verb, args in qw.parse_replay(
                    self.script().read_text(encoding="utf-8")):
                r.apply(verb, args)
            self.assertGreater(r.naive.count, len(r.triggers))
            self.assertEqual(r.naive.output_events, 3)

    def test_without_command_output(self):
        with tempfile.TemporaryDirectory() as td:
            r = qw.Replay(Path(td), 0.15, make_output=False)
            r.apply("save", ["a.py", "hello"])
            r.apply("wait", ["0.30"])
            self.assertEqual(len(r.triggers), 1)
            self.assertEqual(r.absorbed, [])

    def test_touch_action_produces_ignored_change(self):
        with tempfile.TemporaryDirectory() as td:
            r = qw.Replay(Path(td), 0.15)
            r.apply("write", ["a.py", "hello"])
            r.apply("wait", ["0.30"])
            r.apply("touch", ["a.py", "hello"])
            r.apply("wait", ["0.30"])
            self.assertEqual(len(r.triggers), 1)

    def test_replay_json_report(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = qw.main(["--replay", str(self.script()), "--json"])
        self.assertEqual(code, 0)
        report = json.loads(buf.getvalue().strip())
        self.assertEqual(report["triggers"], 3)
        self.assertEqual(report["self_inflicted"], 0)
        self.assertEqual(report["naive_output_triggers"], 3)


class CliTests(TempDirCase):
    def test_version_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            qw.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            qw.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_once_without_command(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = qw.main(["--once", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("基线已建立", buf.getvalue())

    def test_once_runs_command_once(self):
        marker = self.root / "ran.txt"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = qw.main(["--once", str(self.root), "--",
                            sys.executable, "-c",
                            f"open({str(marker)!r},'w').write('ok')"])
        self.assertEqual(code, 0)
        self.assertTrue(marker.exists())

    def test_bad_interval_returns_two(self):
        self.assertEqual(qw.main(["--interval", "0", str(self.root)]), 2)

    def test_negative_settle_returns_two(self):
        self.assertEqual(qw.main(["--settle", "-1", str(self.root)]), 2)

    def test_missing_directory_returns_two(self):
        self.assertEqual(qw.main(["--once", str(self.root / "nope")]), 2)

    def test_bad_replay_script_returns_two(self):
        p = self.put("bad.txt", "reboot a b\n")
        self.assertEqual(qw.main(["--replay", str(p)]), 2)

    def test_json_baseline_is_parseable(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            qw.main(["--once", "--json", str(self.root)])
        obj = json.loads(buf.getvalue().strip().splitlines()[0])
        self.assertEqual(obj["type"], "baseline")
        self.assertEqual(obj["files"], 0)

    def test_json_explain_emits_only_json(self):
        self.put(".git/config", "x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            qw.main(["--once", "--json", "--explain", str(self.root)])
        for line in buf.getvalue().strip().splitlines():
            json.loads(line)

    def test_dry_run_flag_is_accepted(self):
        parser = qw.build_parser()
        args = parser.parse_args(["--dry-run", "."])
        self.assertTrue(args.dry_run)

    def test_command_split_after_double_dash(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = qw.main(["--once", str(self.root), "--", "echo", "hi"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
