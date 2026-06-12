"""
Test suite for warm_dispatch.py.

Covers every code path WITHOUT calling the real `claude` CLI (so no quota is
spent and failure paths like timeout / dead-binary can be forced):

  * find_claude            - locates the CLI
  * oldest_task            - empty / ignores non-json / picks oldest by mtime
  * load_task              - valid / missing prompt / empty prompt / non-dict /
                             malformed JSON / default cwd / nonexistent cwd
  * archive_task           - moves file, timestamp+status name, collision suffix
  * run_claude             - success / nonzero / timeout-kill / missing binary
  * main (integration)     - warmup ok, productivity ok+archive, bad-task archives
                             & advances queue, claude-fail archives FAILED,
                             no-claude exits 2, oldest-first (newer task untouched)

The real `claude` is mocked at the subprocess boundary or via patched
run_claude/find_claude. Each test uses an isolated temp sandbox for queue/
archive/ so nothing touches your real folders.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import warm_dispatch as w


def quiet_logger():
    """A logger that swallows output but lets us capture RUN lines."""
    import logging
    lg = logging.getLogger("test_warm_dispatch")
    lg.handlers = []
    lg.addHandler(logging.NullHandler())
    lg.setLevel(logging.CRITICAL)  # silence
    return lg


class SandboxTest(unittest.TestCase):
    """Base: redirect module dirs into a throwaway temp tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wd_test_"))
        self.queue = self.tmp / "queue"
        self.archive = self.tmp / "archive"
        self.queue.mkdir()
        # archive intentionally NOT created -> tests prove archive_task makes it

        # Patch module globals to point at the sandbox.
        self._orig = {
            "QUEUE_DIR": w.QUEUE_DIR,
            "ARCHIVE_DIR": w.ARCHIVE_DIR,
            "BASE_DIR": w.BASE_DIR,
        }
        w.QUEUE_DIR = self.queue
        w.ARCHIVE_DIR = self.archive
        w.BASE_DIR = self.tmp

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(w, k, v)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def write_task(self, name, prompt="do it", cwd=None, raw=None):
        p = self.queue / name
        if raw is not None:
            p.write_text(raw, encoding="utf-8")
        else:
            body = {"prompt": prompt}
            if cwd is not None:
                body["cwd"] = cwd
            p.write_text(json.dumps(body), encoding="utf-8")
        return p


class TestFindClaude(unittest.TestCase):
    def test_finds_real_cli(self):
        # claude IS installed in this environment; should resolve.
        self.assertIsNotNone(w.find_claude(), "claude CLI should be on PATH")

    def test_returns_none_when_absent(self):
        with mock.patch.object(w.shutil, "which", return_value=None):
            self.assertIsNone(w.find_claude())


class TestOldestTask(SandboxTest):
    def test_empty_queue_returns_none(self):
        self.assertIsNone(w.oldest_task())

    def test_ignores_non_json(self):
        (self.queue / "note.txt").write_text("x", encoding="utf-8")
        (self.queue / "tmpl.json.sample").write_text("{}", encoding="utf-8")
        self.assertIsNone(w.oldest_task(), "only *.json should count")

    def test_picks_oldest_by_mtime(self):
        a = self.write_task("a.json")
        time.sleep(0.02)
        b = self.write_task("b.json")
        # force a to be older
        old = time.time() - 100
        os.utime(a, (old, old))
        self.assertEqual(w.oldest_task().name, "a.json")
        # make b older instead
        older = time.time() - 200
        os.utime(b, (older, older))
        self.assertEqual(w.oldest_task().name, "b.json")


class TestLoadTask(SandboxTest):
    def test_valid(self):
        p = self.write_task("ok.json", prompt="hello", cwd=str(self.tmp))
        prompt, cwd = w.load_task(p)
        self.assertEqual(prompt, "hello")
        self.assertEqual(cwd, str(self.tmp))

    def test_default_cwd_is_base(self):
        p = self.write_task("nocwd.json", prompt="hi")  # no cwd key
        prompt, cwd = w.load_task(p)
        self.assertEqual(cwd, str(self.tmp))  # BASE_DIR patched to tmp

    def test_missing_prompt(self):
        p = self.write_task("bad.json", raw='{"cwd": "."}')
        with self.assertRaises(ValueError):
            w.load_task(p)

    def test_empty_prompt(self):
        p = self.write_task("bad.json", raw='{"prompt": "   "}')
        with self.assertRaises(ValueError):
            w.load_task(p)

    def test_non_dict_json(self):
        p = self.write_task("bad.json", raw='["not", "an", "object"]')
        with self.assertRaises(ValueError):
            w.load_task(p)

    def test_malformed_json(self):
        p = self.write_task("bad.json", raw='{ this is not json ')
        with self.assertRaises(ValueError):
            w.load_task(p)

    def test_nonexistent_cwd(self):
        p = self.write_task("bad.json", prompt="hi",
                             cwd=str(self.tmp / "does_not_exist"))
        with self.assertRaises(ValueError):
            w.load_task(p)


class TestArchiveTask(SandboxTest):
    def test_moves_with_status_and_timestamp(self):
        p = self.write_task("job.json")
        dest = w.archive_task(p, status="OK")
        self.assertFalse(p.exists(), "source removed")
        self.assertTrue(dest.exists(), "lands in archive")
        self.assertTrue(self.archive.is_dir(), "archive dir created")
        self.assertIn("_OK_job.json", dest.name)
        self.assertTrue(dest.name[:8].isdigit(), "timestamp prefix")

    def test_collision_gets_suffix(self):
        # Two archives of same name in same second -> second gets counter.
        with mock.patch("warm_dispatch.datetime") as mdt:
            mdt.now.return_value.strftime.return_value = "20260101-000000"
            p1 = self.write_task("dup.json")
            d1 = w.archive_task(p1, status="OK")
            p2 = self.write_task("dup.json")
            d2 = w.archive_task(p2, status="OK")
        self.assertNotEqual(d1.name, d2.name)
        self.assertTrue(d1.exists() and d2.exists())


class TestRunClaude(SandboxTest):
    def test_success(self):
        fake = mock.Mock(returncode=0, stdout="OK", stderr="")
        with mock.patch("warm_dispatch.subprocess.run", return_value=fake):
            rc = w.run_claude("claude", "p", str(self.tmp), 10, quiet_logger())
        self.assertEqual(rc, 0)

    def test_nonzero(self):
        fake = mock.Mock(returncode=7, stdout="", stderr="boom")
        with mock.patch("warm_dispatch.subprocess.run", return_value=fake):
            rc = w.run_claude("claude", "p", str(self.tmp), 10, quiet_logger())
        self.assertEqual(rc, 7)

    def test_timeout_is_killed(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=10)
        with mock.patch("warm_dispatch.subprocess.run", side_effect=boom):
            rc = w.run_claude("claude", "p", str(self.tmp), 10, quiet_logger())
        self.assertEqual(rc, w.EXIT_TIMEOUT)

    def test_missing_binary(self):
        with mock.patch("warm_dispatch.subprocess.run",
                        side_effect=FileNotFoundError()):
            rc = w.run_claude("claude", "p", str(self.tmp), 10, quiet_logger())
        self.assertEqual(rc, w.EXIT_NO_CLAUDE)

    def test_stdin_is_devnull(self):
        # Prove we never block on input: stdin must be DEVNULL.
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("warm_dispatch.subprocess.run", return_value=fake) as m:
            w.run_claude("claude", "p", str(self.tmp), 10, quiet_logger())
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("timeout"), 10)


class TestMainIntegration(SandboxTest):
    """Drive main() end-to-end with claude mocked out."""

    def _run_main(self, claude_rc=0, claude_path="claude"):
        """Patch find_claude + run_claude, run main(), return exit code."""
        with mock.patch("warm_dispatch.setup_logging", return_value=quiet_logger()), \
             mock.patch("warm_dispatch.find_claude", return_value=claude_path), \
             mock.patch("warm_dispatch.run_claude", return_value=claude_rc) as rc_mock:
            code = w.main()
        return code, rc_mock

    def test_warmup_ok(self):
        code, rc_mock = self._run_main(claude_rc=0)
        self.assertEqual(code, w.EXIT_OK)
        # warmup sends "ping"
        args = rc_mock.call_args[0]
        self.assertEqual(args[1], "ping")

    def test_warmup_claude_fails(self):
        code, _ = self._run_main(claude_rc=3)
        self.assertEqual(code, w.EXIT_TASK_FAILED)

    def test_no_claude_exits_2(self):
        with mock.patch("warm_dispatch.setup_logging", return_value=quiet_logger()), \
             mock.patch("warm_dispatch.find_claude", return_value=None):
            self.assertEqual(w.main(), w.EXIT_NO_CLAUDE)

    def test_productivity_ok_archives(self):
        self.write_task("job.json", prompt="work", cwd=str(self.tmp))
        code, rc_mock = self._run_main(claude_rc=0)
        self.assertEqual(code, w.EXIT_OK)
        # prompt passed through (not "ping")
        self.assertEqual(rc_mock.call_args[0][1], "work")
        # queue empty, archived OK
        self.assertEqual(list(self.queue.glob("*.json")), [])
        arch = list(self.archive.glob("*_OK_job.json"))
        self.assertEqual(len(arch), 1)

    def test_productivity_claude_fail_archives_FAILED(self):
        self.write_task("job.json", prompt="work", cwd=str(self.tmp))
        code, _ = self._run_main(claude_rc=4)
        self.assertEqual(code, w.EXIT_TASK_FAILED)
        self.assertEqual(list(self.queue.glob("*.json")), [],
                         "queue advances even on failure")
        self.assertEqual(len(list(self.archive.glob("*_FAILED_job.json"))), 1)

    def test_productivity_timeout_archives_TIMEOUT(self):
        self.write_task("job.json", prompt="work", cwd=str(self.tmp))
        code, _ = self._run_main(claude_rc=w.EXIT_TIMEOUT)
        self.assertEqual(code, w.EXIT_TIMEOUT)
        self.assertEqual(len(list(self.archive.glob("*_TIMEOUT_job.json"))), 1)

    def test_bad_task_archives_BADTASK_and_advances(self):
        self.write_task("broken.json", raw="{ not valid json")
        with mock.patch("warm_dispatch.setup_logging", return_value=quiet_logger()), \
             mock.patch("warm_dispatch.find_claude", return_value="claude"), \
             mock.patch("warm_dispatch.run_claude") as rc_mock:
            code = w.main()
        self.assertEqual(code, w.EXIT_BAD_TASK)
        rc_mock.assert_not_called()  # never invoked claude on a broken task
        self.assertEqual(list(self.queue.glob("*.json")), [],
                         "broken task removed so it can't block the queue")
        self.assertEqual(len(list(self.archive.glob("*_BADTASK_broken.json"))), 1)

    def test_only_oldest_runs_newer_untouched(self):
        old = self.write_task("old.json", prompt="old", cwd=str(self.tmp))
        new = self.write_task("new.json", prompt="new", cwd=str(self.tmp))
        t_old = time.time() - 500
        os.utime(old, (t_old, t_old))
        code, rc_mock = self._run_main(claude_rc=0)
        self.assertEqual(code, w.EXIT_OK)
        self.assertEqual(rc_mock.call_args[0][1], "old", "oldest prompt ran")
        # new.json still waiting
        self.assertTrue(new.exists(), "newer task left in queue for next run")
        self.assertEqual(len(list(self.queue.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
