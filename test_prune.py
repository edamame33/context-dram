"""Tests for prune_jsonl - the lossless page-out stripper.

Run:  py -3.13 test_prune.py
"""
import json
import unittest

from prune_jsonl import strip_obj, process_lines, slugify_cwd


def line(**kw):
    return json.dumps(kw, separators=(",", ":"))


class TestStrip(unittest.TestCase):

    def test_strips_tooluseresult(self):
        obj = {"type": "user",
               "message": {"content": [{"type": "tool_result", "content": "ok"}]},
               "toolUseResult": {"oldString": "a" * 5000, "newString": "b" * 5000}}
        strip_obj(obj)
        self.assertNotIn("toolUseResult", obj)
        self.assertIn("message", obj)              # model-visible content kept

    def test_keeps_message_content(self):
        obj = {"type": "assistant",
               "message": {"content": [{"type": "text", "text": "hello"}]},
               "toolUseResult": {"x": 1}}
        strip_obj(obj)
        self.assertEqual(obj["message"]["content"][0]["text"], "hello")

    def test_metadata_off_by_default(self):
        obj = {"type": "assistant", "message": {"usage": {"input_tokens": 10}}, "costUSD": 0.01}
        strip_obj(obj)                              # strip_metadata defaults False
        self.assertIn("costUSD", obj)
        self.assertIn("usage", obj["message"])

    def test_metadata_strip(self):
        obj = {"type": "assistant",
               "message": {"usage": {"input_tokens": 10}, "stop_reason": "end_turn",
                           "content": [{"type": "text", "text": "x"}]},
               "costUSD": 0.01, "durationMs": 50}
        strip_obj(obj, strip_metadata=True)
        self.assertNotIn("costUSD", obj)
        self.assertNotIn("durationMs", obj)
        self.assertNotIn("usage", obj["message"])
        self.assertNotIn("stop_reason", obj["message"])
        self.assertIn("content", obj["message"])    # content always survives

    def test_line_count_preserved(self):
        lines = [line(type="user", toolUseResult={"a": 1}), "",
                 line(type="assistant", message={"content": []})]
        out, _saved, _n = process_lines(lines)
        self.assertEqual(len(out), len(lines))

    def test_unparseable_passthrough(self):
        lines = ["{not json", line(type="user", toolUseResult={"a": 1})]
        out, _saved, _n = process_lines(lines)
        self.assertEqual(out[0], "{not json")

    def test_savings_positive_and_still_valid_json(self):
        big = line(type="user",
                   message={"content": [{"type": "tool_result", "content": "r"}]},
                   toolUseResult={"structuredPatch": ["x"] * 1000})
        out, saved, changed = process_lines([big])
        self.assertGreater(saved, 0)
        self.assertEqual(changed, 1)
        reparsed = json.loads(out[0])               # output is valid JSON
        self.assertNotIn("toolUseResult", reparsed)

    def test_no_change_when_nothing_to_strip(self):
        ln = line(type="assistant", message={"content": [{"type": "text", "text": "hi"}]})
        out, _saved, changed = process_lines([ln])
        self.assertEqual(changed, 0)
        self.assertEqual(json.loads(out[0]), json.loads(ln))

    def test_slugify_matches_claude_layout(self):
        self.assertEqual(slugify_cwd(r"C:\Users\alice"), "C--Users-alice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
