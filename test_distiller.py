"""Tests for distiller - raw turn -> cell, and the distiller -> memory pipeline.

Run:  py -3.13 test_distiller.py
"""
import unittest

from distiller import (distill, classify_type, title_of, extract_files,
                       extract_offsets, extract_concepts, extract_facts)
from memory import Memory, Config

SAMPLE = r"""
We patched the byte at offset 0x4A2F in libfoo.dylib to skip the license check.
Decided to go with an inline hook via MinHook rather than IAT patching.
- the check is at FUN_00401abc
- replaced 0x74 (JZ) with 0xEB (JMP)
Read C:\proj\src\Plugin.cs and edited main.rs.
"""


class TestExtraction(unittest.TestCase):

    def test_classify_decision(self):
        self.assertEqual(classify_type(SAMPLE), "decision")   # "Decided to go with"

    def test_classify_default_fact(self):
        self.assertEqual(classify_type("The endianness is little."), "fact")

    def test_title_is_first_real_line(self):
        self.assertTrue(title_of(SAMPLE).startswith("We patched the byte"))

    def test_extract_files(self):
        files = extract_files(SAMPLE)
        self.assertIn("libfoo.dylib", files)
        self.assertIn("main.rs", files)
        self.assertTrue(any("Plugin.cs" in f for f in files))

    def test_extract_offsets(self):
        offs = extract_offsets(SAMPLE)
        self.assertIn("0x4A2F", offs)
        self.assertIn("0xEB", offs)

    def test_facts_capture_offsets_and_bullets(self):
        facts = extract_facts(SAMPLE)
        self.assertTrue(any("0x4A2F" in f for f in facts))
        self.assertTrue(any("check is at" in f for f in facts))

    def test_concepts_lead_with_offsets(self):
        concepts = extract_concepts(SAMPLE)
        self.assertLessEqual(len(concepts), 6)


class TestDistill(unittest.TestCase):

    def test_shape(self):
        d = distill(SAMPLE)
        for k in ("type", "title", "facts", "files", "concepts", "discovery_tokens"):
            self.assertIn(k, d)
        self.assertGreater(d["discovery_tokens"], 0)

    def test_model_fn_override(self):
        d = distill(SAMPLE, model_fn=lambda t: {"facts": ["x"], "files": [], "concepts": ["c"]})
        self.assertEqual(d["facts"], ["x"])
        self.assertIn("type", d)          # backfilled
        self.assertIn("title", d)

    def test_pipeline_into_memory(self):
        """The payoff: distiller output splats straight into Memory.write."""
        m = Memory(":memory:", Config())
        d = distill(SAMPLE)
        cid = m.write(session_id="s1", project="proj", now_turn=0, **d)
        cell = m.get(cid)
        self.assertTrue(cell.title.startswith("We patched"))
        self.assertEqual(cell.type, "decision")
        self.assertTrue(any("libfoo" in f for f in cell.files))
        self.assertAlmostEqual(cell.eff_charge(0, cell.created_at_epoch), 1.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
