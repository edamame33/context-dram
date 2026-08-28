"""Tests for the context-dram hooks - capture + prime, against a temp DB.

Run:  py -3.13 test_hooks.py
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cdram_test_")
os.environ["CDRAM_DB_DIR"] = _TMP        # MUST precede the cdram imports

import cdram_capture          # noqa: E402
import cdram_prime            # noqa: E402
from memory import Memory     # noqa: E402
from cdram_config import DB_PATH, project_for   # noqa: E402


def write_transcript(path, cwd):
    rows = [
        {"sessionId": "sess1", "cwd": cwd, "type": "user", "uuid": "u0", "parentUuid": None,
         "message": {"role": "user", "content": "patch the license check"}},
        {"sessionId": "sess1", "cwd": cwd, "type": "assistant", "uuid": "a0", "parentUuid": "u0",
         "message": {"role": "assistant", "content": [
             {"type": "text",
              "text": "Patched byte at 0x4A2F in libfoo.dylib via an inline hook."},
             {"type": "tool_use", "id": "t0", "name": "Edit",
              "input": {"file_path": "C:/proj/patch.py"}}]}},
    ]
    Path(path).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class TestCaptureHook(unittest.TestCase):
    def setUp(self):
        self.cwd = r"C:\proj\demo"
        self.tx = os.path.join(_TMP, "transcript.jsonl")
        write_transcript(self.tx, self.cwd)

    def test_capture_writes_a_cell(self):
        cid = cdram_capture.capture(self.tx)
        self.assertIsNotNone(cid)
        m = Memory(str(DB_PATH))
        cell = m.get(cid)
        m.close()
        self.assertTrue(cell.title.startswith("Patched byte"))
        self.assertTrue(any("libfoo" in f or "patch.py" in f for f in cell.files))
        self.assertEqual(cell.project, project_for(self.cwd))

    def test_capture_empty_transcript_is_safe(self):
        empty = os.path.join(_TMP, "empty.jsonl")
        Path(empty).write_text("", encoding="utf-8")
        self.assertIsNone(cdram_capture.capture(empty))

    def test_capture_missing_file_is_safe(self):
        self.assertIsNone(cdram_capture.capture(os.path.join(_TMP, "nope.jsonl")))


class TestPrimeHook(unittest.TestCase):
    def test_prime_emits_hot_set(self):
        cwd = r"C:\proj\primedemo"
        tx = os.path.join(_TMP, "t2.jsonl")
        write_transcript(tx, cwd)
        cdram_capture.capture(tx)                      # seed a cell for this project
        ctx = cdram_prime.build_context(cwd)
        self.assertIsNotNone(ctx)
        self.assertIn("Patched byte", ctx)
        self.assertNotIn("inline hook via", ctx)        # index shows titles, not bodies

    def test_prime_unknown_project_returns_none(self):
        self.assertIsNone(cdram_prime.build_context(r"C:\nothing\here\ever"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
