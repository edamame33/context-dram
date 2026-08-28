"""Subprocess-level integration tests for the context-dram hooks.

Pattern: run the REAL hook script via subprocess, feed it the same stdin JSON
Claude Code would, redirect all output dirs via env vars (CLAUDE_SCRATCHPAD_DIR
/ CDRAM_DB_DIR), then assert on the files/DB the hook produced. No live data is
touched; everything lands in a tempdir.

Both capturers spawn a DETACHED worker, so the produced files appear
asynchronously - these tests poll a short deadline rather than assuming the
file exists the instant the foreground hook returns.

Run:  py -3.13 -m unittest test_hooks_subprocess -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).parent
PY = sys.executable  # same interpreter the hooks are registered with


def synthetic_transcript(path: Path, session_id: str, cwd: str) -> None:
    rows = [
        {"type": "user", "uuid": "u0", "parentUuid": None, "sessionId": session_id,
         "cwd": cwd, "message": {"role": "user", "content": "trace the auth flow"}},
        {"type": "assistant", "uuid": "a0", "parentUuid": "u0", "sessionId": session_id,
         "cwd": cwd, "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Found the check at FUN_00401abc; fixed offset 0x4A2F."},
             {"type": "tool_use", "id": "t0", "name": "Edit",
              "input": {"file_path": "C:/proj/auth.py"}}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def run_hook(script: Path, stdin_bytes, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    if isinstance(stdin_bytes, (dict, list)):
        stdin_bytes = json.dumps(stdin_bytes).encode("utf-8")
    return subprocess.run(
        [PY, str(script)], input=stdin_bytes,
        capture_output=True, timeout=30, env=env, cwd=str(REPO))


def _poll(predicate, deadline_s=10.0, interval=0.1):
    end = time.perf_counter() + deadline_s
    while time.perf_counter() < end:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    return predicate()


class TestScratchCaptureSubprocess(unittest.TestCase):
    """hooks/scratch_capture.py end-to-end: stdin JSON -> detached worker -> .md."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdram_it_pad_"))
        self.tx = self.tmp / "transcript.jsonl"
        synthetic_transcript(self.tx, "sessX", r"C:\proj\demo")
        self.env = {"CLAUDE_SCRATCHPAD_DIR": str(self.tmp / "pad")}
        self.payload = {"session_id": "sessX", "transcript_path": str(self.tx),
                        "cwd": r"C:\proj\demo", "hook_event_name": "Stop"}
        self.pad = self.tmp / "pad" / "sessX.md"

    def test_capture_writes_scratchpad_and_cursor(self):
        r = run_hook(REPO / "hooks" / "scratch_capture.py", self.payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.pad.exists(), "scratchpad file not written")
        body = self.pad.read_text(encoding="utf-8")
        self.assertIn("trace the auth flow", body)          # user prompt captured
        self.assertIn("Edit(C:/proj/auth.py)", body)        # tool call summarized
        self.assertIn("FUN_00401abc", body)                 # assistant text captured
        self.assertTrue((self.tmp / "pad" / ".cursor_sessX").exists())

    def test_rerun_same_turn_is_deduped_by_cursor(self):
        run_hook(REPO / "hooks" / "scratch_capture.py", self.payload, self.env)
        size1 = self.pad.stat().st_size
        r = run_hook(REPO / "hooks" / "scratch_capture.py", self.payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pad.stat().st_size, size1, "duplicate turn appended")

    def test_bom_and_utf16_stdin_still_capture(self):
        # a BOM'd / UTF-16 hook payload used to silently kill the text-mode read
        for enc, label in (("utf-8-sig", "bom"), ("utf-16", "u16")):
            with self.subTest(enc=enc):
                tmp = Path(tempfile.mkdtemp(prefix=f"cdram_it_{label}_"))
                tx = tmp / "t.jsonl"
                synthetic_transcript(tx, f"s_{label}", r"C:\proj\demo")
                env = {"CLAUDE_SCRATCHPAD_DIR": str(tmp / "pad")}
                payload = {"session_id": f"s_{label}", "transcript_path": str(tx),
                           "cwd": r"C:\proj\demo"}
                raw = json.dumps(payload).encode(enc)
                r = run_hook(REPO / "hooks" / "scratch_capture.py", raw, env)
                self.assertEqual(r.returncode, 0, r.stderr)
                pad = tmp / "pad" / f"s_{label}.md"
                self.assertTrue(pad.exists(), f"{enc} payload dropped")

    def test_corrupt_transcript_line_quarantined_not_fatal(self):
        # one non-UTF-8 byte in the transcript must drop ONE line, not the turn
        tmp = Path(tempfile.mkdtemp(prefix="cdram_it_badbyte_"))
        tx = tmp / "t.jsonl"
        synthetic_transcript(tx, "sBad", r"C:\proj\demo")
        with open(tx, "ab") as f:
            f.write(b'{"type":"assistant","uuid":"a1","message":{"content":'
                    b'[{"type":"text","text":"tail \xff marker Z7"}]}}\n')
        env = {"CLAUDE_SCRATCHPAD_DIR": str(tmp / "pad")}
        r = run_hook(REPO / "hooks" / "scratch_capture.py",
                     {"session_id": "sBad", "transcript_path": str(tx),
                      "cwd": r"C:\proj\demo"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        pad = tmp / "pad" / "sBad.md"
        self.assertTrue(pad.exists(), "corrupt byte aborted the whole capture")


class TestScratchPrimeSubprocess(unittest.TestCase):
    """hooks/scratch_prime.py end-to-end: resume re-emits; clear deletes; cwd match."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdram_it_prime_"))
        self.pad_dir = self.tmp / "pad"
        self.pad_dir.mkdir(parents=True)
        (self.pad_dir / "sessY.md").write_text(
            "---\nsession_id: sessY\ncwd: C:\\proj\\demo\n---\n\n## [10:00:00]\n"
            "**User:** earlier work marker LX9Q\n\n", encoding="utf-8")
        # TTL=session skips the Windows event-log subprocess -> fast + portable
        self.env = {"CLAUDE_SCRATCHPAD_DIR": str(self.pad_dir),
                    "CLAUDE_SCRATCHPAD_TTL_HOURS": "session"}

    def test_resume_emits_own_scratchpad(self):
        r = run_hook(REPO / "hooks" / "scratch_prime.py",
                     {"session_id": "sessY", "cwd": r"C:\proj\demo", "source": "resume"},
                     self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LX9Q", r.stdout.decode("utf-8"))
        self.assertIn("Scratchpad recall", r.stdout.decode("utf-8"))

    def test_clear_deletes_scratchpad(self):
        r = run_hook(REPO / "hooks" / "scratch_prime.py",
                     {"session_id": "sessY", "cwd": r"C:\proj\demo", "source": "clear"},
                     self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.pad_dir / "sessY.md").exists())

    def test_startup_cwd_prefix_does_not_leak_across_projects(self):
        # a session in C:\proj must NOT recall the pad of C:\proj2 (prefix bug)
        (self.pad_dir / "sib.md").write_text(
            "---\nsession_id: sib\ncwd: C:\\proj2\n---\n\n## [09:00:00]\n"
            "**User:** SIBLING_MARKER\n\n", encoding="utf-8")
        r = run_hook(REPO / "hooks" / "scratch_prime.py",
                     {"session_id": "new", "cwd": r"C:\proj", "source": "startup"},
                     self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SIBLING_MARKER", r.stdout.decode("utf-8"))

    def test_ttl_hours_evicts_aged_pad(self):
        old = self.pad_dir / "aged.md"
        old.write_text("---\nsession_id: aged\ncwd: C:\\x\n---\n", encoding="utf-8")
        past = time.time() - 3 * 3600
        os.utime(old, (past, past))
        env = dict(self.env)
        env["CLAUDE_SCRATCHPAD_TTL_HOURS"] = "1"
        r = run_hook(REPO / "hooks" / "scratch_prime.py",
                     {"session_id": "z", "cwd": r"C:\x", "source": "startup"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(old.exists(), "aged pad not evicted under hours TTL")


class TestCdramWorkerSubprocess(unittest.TestCase):
    """cdram_capture.py FULL chain: foreground hook -> detached worker -> SQLite cell."""

    def test_foreground_spawns_worker_that_writes_cell(self):
        tmp = Path(tempfile.mkdtemp(prefix="cdram_it_db_"))
        tx = tmp / "transcript.jsonl"
        synthetic_transcript(tx, "sessZ", r"C:\proj\demo")
        env = {"CDRAM_DB_DIR": str(tmp / "db"),
               "CLAUDE_SCRATCHPAD_DIR": str(tmp / "pad")}
        r = run_hook(REPO / "cdram_capture.py",
                     {"session_id": "sessZ", "transcript_path": str(tx),
                      "cwd": r"C:\proj\demo", "hook_event_name": "Stop"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        db = tmp / "db" / "cells.sqlite3"

        def count():
            if not db.exists():
                return None
            try:
                con = sqlite3.connect(db)
                n = con.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
                con.close()
                return n or None
            except sqlite3.OperationalError:
                return None
        self.assertEqual(_poll(count), 1, "detached worker never wrote a cell")
        con = sqlite3.connect(db)
        title, files = con.execute("SELECT title, files FROM cells").fetchone()
        con.close()
        self.assertIn("FUN_00401abc", title)
        self.assertIn("auth.py", files)

    def test_prime_reads_back_what_capture_wrote(self):
        tmp = Path(tempfile.mkdtemp(prefix="cdram_it_rt_"))
        tx = tmp / "transcript.jsonl"
        synthetic_transcript(tx, "sessW", r"C:\proj\demo")
        env = {"CDRAM_DB_DIR": str(tmp / "db")}
        e = dict(os.environ)
        e.update(env)
        # run the worker branch synchronously (deterministic, no polling)
        subprocess.run([PY, str(REPO / "cdram_capture.py"), "--worker", str(tx)],
                       timeout=30, env=e, cwd=str(REPO), capture_output=True)
        r = run_hook(REPO / "cdram_prime.py",
                     {"session_id": "new", "cwd": r"C:\proj\demo",
                      "source": "startup"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.decode("utf-8"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("FUN_00401abc", ctx)
        self.assertIn("hot working set", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
