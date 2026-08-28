"""Tests for prune_lines - the careful, line-removing page-out.

Run:  py -3.13 test_prune_lines.py
"""
import unittest

from prune_lines import (assign_ages, stub_tool_results, apply_collapse,
                         validate, transform)


# --- tiny session-builder helpers ----------------------------------------- #
def prompt(uuid, parent, text="do the thing"):
    return {"uuid": uuid, "parentUuid": parent, "type": "user",
            "message": {"role": "user", "content": text}}


def asst(uuid, parent, text=None, tool_use=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_use:
        content.append({"type": "tool_use", "id": tool_use, "name": "Read", "input": {}})
    return {"uuid": uuid, "parentUuid": parent, "type": "assistant",
            "message": {"role": "assistant", "content": content}}


def tool_res(uuid, parent, tool_use_id, payload):
    return {"uuid": uuid, "parentUuid": parent, "type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                     "content": payload}]}}


def boundary(uuid, parent):
    return {"uuid": uuid, "parentUuid": parent, "type": "system", "subtype": "compact_boundary"}


def summary(uuid, parent):
    return {"uuid": uuid, "parentUuid": parent, "type": "user", "isCompactSummary": True,
            "message": {"role": "user", "content": "SUMMARY of prior work"}}


class TestAgeStub(unittest.TestCase):
    def test_ages_count_from_end(self):
        objs = [prompt("u0", None), asst("a0", "u0", "hi"),
                prompt("u1", "a0"), asst("a1", "u1", "yo")]
        ages = assign_ages(objs)
        self.assertEqual(ages[3], 0)   # after the last prompt
        self.assertEqual(ages[0], 1)   # one prompt sits after it

    def test_stubs_old_results_keeps_id(self):
        tr = tool_res("t", "p", "tu1", "X" * 5000)
        saved = stub_tool_results(tr, age=50, stub_age=40, minify_age=15)
        self.assertGreater(saved, 4000)
        block = tr["message"]["content"][0]
        self.assertEqual(block["tool_use_id"], "tu1")          # pairing preserved
        self.assertIn("elided", block["content"])

    def test_recent_results_untouched(self):
        tr = tool_res("t", "p", "tu1", "X" * 5000)
        saved = stub_tool_results(tr, age=3, stub_age=40, minify_age=15)
        self.assertEqual(saved, 0)
        self.assertEqual(len(tr["message"]["content"][0]["content"]), 5000)


class TestCollapse(unittest.TestCase):
    def _session_with_boundary(self):
        # pre-compaction work, then boundary + summary, then fresh work
        return [
            prompt("u0", None, "old task"),
            asst("a0", "u0", tool_use="t0"),
            tool_res("r0", "a0", "t0", "big old output " * 500),
            asst("a0b", "r0", "done old"),
            boundary("B", "a0b"),
            summary("S", "B"),
            prompt("u1", "S", "new task"),
            asst("a1", "u1", "on it"),
        ]

    def test_collapse_drops_prefix_keeps_boundary_and_summary(self):
        objs = self._session_with_boundary()
        out, rpt = apply_collapse([dict(o) for o in objs])
        self.assertTrue(rpt["applied"])
        uuids = {o["uuid"] for o in out}
        self.assertIn("B", uuids)     # boundary survives
        self.assertIn("S", uuids)     # summary survives
        self.assertIn("u1", uuids)    # post-boundary work survives
        self.assertNotIn("u0", uuids)  # pre-boundary work dropped
        self.assertNotIn("r0", uuids)

    def test_collapse_rethreads_chain_to_root(self):
        objs = self._session_with_boundary()
        out, _ = apply_collapse([dict(o) for o in objs])
        self.assertIsNone(out[0]["parentUuid"])               # first survivor is root
        ok, why = validate(out)
        self.assertTrue(ok, why)                              # no chain break

    def test_collapse_aborts_without_boundary(self):
        objs = [prompt("u0", None), asst("a0", "u0", "hi")]
        out, rpt = apply_collapse([dict(o) for o in objs])
        self.assertFalse(rpt["applied"])
        self.assertIn("no compact_boundary", rpt["reason"])

    def test_collapse_aborts_on_preserved_segment(self):
        objs = self._session_with_boundary()
        objs[4]["hasPreservedSegment"] = True
        out, rpt = apply_collapse([dict(o) for o in objs])
        self.assertFalse(rpt["applied"])
        self.assertIn("preserved", rpt["reason"])

    def test_collapse_keeps_metadata_singleton(self):
        objs = self._session_with_boundary()
        # a permission-mode line before the boundary, none after
        objs.insert(1, {"uuid": "pm", "parentUuid": "u0", "type": "permission-mode", "mode": "plan"})
        out, rpt = apply_collapse([dict(o) for o in objs])
        self.assertIn("pm", {o["uuid"] for o in out})         # singleton retained

    def test_branching_guard_skips_collapse(self):
        objs = self._session_with_boundary()
        # two messages claiming the same parent after the boundary = a fork
        objs.append(asst("a1-alt", "u1", "alternate branch"))
        out, rpt = apply_collapse([dict(o) for o in objs])
        self.assertFalse(rpt["applied"])
        self.assertIn("branching", rpt["reason"])


class TestValidation(unittest.TestCase):
    def test_catches_dangling_tool_use(self):
        # assistant asks for a tool, but the result message is gone
        objs = [prompt("u0", None), asst("a0", "u0", tool_use="t0"),
                prompt("u1", "a0"), asst("a1", "u1", "done")]
        ok, why = validate(objs)
        self.assertFalse(ok)
        self.assertIn("dangling", why)

    def test_catches_missing_roles(self):
        ok, why = validate([asst("a0", None, "lonely")])
        self.assertFalse(ok)

    def test_passes_clean_paired_session(self):
        objs = [prompt("u0", None), asst("a0", "u0", tool_use="t0"),
                tool_res("r0", "a0", "t0", "ok"), asst("a1", "r0", "done")]
        ok, why = validate(objs)
        self.assertTrue(ok, why)


class TestTransform(unittest.TestCase):
    def test_end_to_end_valid_and_saves(self):
        objs = [
            prompt("u0", None, "old"),
            asst("a0", "u0", tool_use="t0"),
            tool_res("r0", "a0", "t0", "huge old output " * 800),
            asst("a0b", "r0", "done"),
            boundary("B", "a0b"),
            summary("S", "B"),
            prompt("u1", "S", "new"),
            asst("a1", "u1", "working"),
        ]
        out, report = transform([dict(o) for o in objs], stub_age=40, minify_age=15)
        self.assertTrue(report["valid"], report["valid_reason"])
        self.assertTrue(report["collapse"]["applied"])
        self.assertLess(len(out), len(objs))            # something was dropped

    def test_collapse_rejected_keeps_agestub(self):
        # no boundary -> collapse can't apply, but age-stub still runs safely
        objs = [prompt("u0", None, "start"),
                asst("a0", "u0", tool_use="t0"),
                tool_res("r0", "a0", "t0", "X" * 9000),
                asst("a0b", "r0", "done reading")]
        prev = "a0b"
        for i in range(1, 46):                       # 45 more turns -> r0 ages past 40
            objs.append(prompt(f"u{i}", prev, "next"))
            objs.append(asst(f"a{i}", f"u{i}", "ok"))
            prev = f"a{i}"
        out, report = transform([dict(o) for o in objs])
        self.assertFalse(report["collapse"]["applied"])    # no boundary
        self.assertGreater(report["age_saved_chars"], 0)   # old tool_result stubbed
        self.assertTrue(report["valid"], report["valid_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
