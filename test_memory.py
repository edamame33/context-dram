"""Tests for context-dram - proving the capacitor physics.

Run:  py -3.13 test_memory.py
"""
import unittest

from memory import (Config, Memory, effective_charge, tier_for)

# A fixed epoch so wall-clock decay never sneaks into turn-based tests.
E = 1_700_000_000.0


class TestChargeMath(unittest.TestCase):
    """The pure decay function - no DB."""

    def test_decays_to_half_at_half_life(self):
        cfg = Config(half_life_turns=7)
        c = effective_charge(1.0, 0, E, now_turn=7, now_epoch=E, cfg=cfg)
        self.assertAlmostEqual(c, 0.5, places=5)

    def test_exponential_shape(self):
        cfg = Config(half_life_turns=7)
        c14 = effective_charge(1.0, 0, E, now_turn=14, now_epoch=E, cfg=cfg)
        self.assertAlmostEqual(c14, 0.25, places=5)  # two half-lives

    def test_pinned_never_decays(self):
        c = effective_charge(1.0, 0, E, now_turn=10_000, now_epoch=E, pinned=True)
        self.assertEqual(c, 1.0)

    def test_session_gap_wall_clock_decay(self):
        cfg = Config(session_gap_decay=0.70)
        # same turn, but last refreshed 2 days ago -> 0.70**2
        c = effective_charge(1.0, 0, E, now_turn=0, now_epoch=E + 2 * 86400, cfg=cfg)
        self.assertAlmostEqual(c, 0.49, places=4)

    def test_strengthening_slows_decay(self):
        cfg = Config(half_life_turns=7, strengthening=True)
        weak = effective_charge(1.0, 0, E, 14, E, refresh_count=0, cfg=cfg)
        strong = effective_charge(1.0, 0, E, 14, E, refresh_count=50, cfg=cfg)
        self.assertGreater(strong, weak)

    def test_tier_thresholds(self):
        self.assertEqual(tier_for(0.70), "HOT")
        self.assertEqual(tier_for(0.30), "WARM")
        self.assertEqual(tier_for(0.10), "COLD")
        self.assertEqual(tier_for(0.01), "ARCHIVED")


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.m = Memory(":memory:", Config(half_life_turns=7))

    def _w(self, title, *, turn=0, files=None, concepts=None, body="x", pinned=False):
        return self.m.write(type="fact", title=title, body=body,
                            files=files or [], concepts=concepts or [],
                            session_id="s1", project="proj", now_turn=turn,
                            now_epoch=E, pinned=pinned)

    def test_write_starts_hot(self):
        cid = self._w("a fresh discovery")
        cell = self.m.get(cid)
        self.assertAlmostEqual(cell.eff_charge(0, E), 1.0, places=6)
        self.assertEqual(cell.tier, "HOT")

    def test_dedup_refreshes_existing(self):
        a = self._w("identical cell", turn=0)
        b = self._w("identical cell", turn=5)   # same content -> same id, refreshed
        self.assertEqual(a, b)
        rows = self.m.db.execute("SELECT COUNT(*) n FROM cells").fetchone()["n"]
        self.assertEqual(rows, 1)
        self.assertEqual(self.m.get(a).refresh_count, 1)

    def test_refresh_restores_to_full(self):
        cid = self._w("decays then refreshed", turn=0)
        self.assertAlmostEqual(self.m.get(cid).eff_charge(7, E), 0.5, places=4)
        self.m.refresh([cid], now_turn=7, now_epoch=E)
        self.assertAlmostEqual(self.m.get(cid).eff_charge(7, E), 1.0, places=6)

    def test_neighbor_boost_spatial_locality(self):
        a = self._w("auth flow entry", files=["auth.py"], concepts=["auth"], turn=0)
        b = self._w("auth token check", files=["auth.py"], concepts=["auth"], turn=0)
        self.assertEqual(self.m.get(a).topic_id, self.m.get(b).topic_id)  # same row
        before = self.m.get(b).eff_charge(5, E)
        self.m.refresh([a], now_turn=5, now_epoch=E)                       # touch neighbor
        after = self.m.get(b).eff_charge(5, E)
        self.assertGreater(after, before)

    def test_touch_by_file_refreshes(self):
        cid = self._w("reads config", files=["settings.json"], turn=0)
        touched = self.m.touch_by_file(["settings.json"], now_turn=9, now_epoch=E)
        self.assertIn(cid, touched)
        self.assertAlmostEqual(self.m.get(cid).eff_charge(9, E), 1.0, places=6)

    def test_page_in_respects_token_budget(self):
        # five cells, ~100 tokens of body each (400 chars), all hot at turn 0
        for i in range(5):
            self._w(f"cell {i}", body="z" * 400, turn=0)
        out = self.m.page_in("proj", now_turn=0, now_epoch=E, budget=250)
        self.assertLessEqual(len(out), 3)
        self.assertLessEqual(sum(c.est_tokens() for c in out), 250)

    def test_page_in_orders_by_charge(self):
        self._w("older", turn=0)           # will have decayed more by turn 6
        new = self._w("newer", turn=6)
        out = self.m.page_in("proj", now_turn=6, now_epoch=E, budget=10_000)
        self.assertEqual(out[0].id, new)   # highest charge first

    def test_pinned_always_paged_in(self):
        self._w("huge pinned header", body="z" * 8000, turn=0, pinned=True)  # ~2000 tok
        out = self.m.page_in("proj", now_turn=0, now_epoch=E, budget=100)    # tiny budget
        self.assertTrue(any(c.pinned for c in out))

    def test_recall_is_index_only_no_bodies(self):
        self._w("Frida inline hook at offset 0x4A2F", body="long body here", turn=0)
        hits = self.m.recall("Frida", project="proj", now_turn=0, now_epoch=E)
        self.assertGreaterEqual(len(hits), 1)
        self.assertNotIn("body", hits[0])              # never leak bodies in the index
        self.assertIn("read_tokens", hits[0])          # but show the cost to fetch

    def test_fetch_returns_bodies_and_refreshes(self):
        cid = self._w("offset table", body="the full body", turn=0)
        # let it decay
        self.assertLess(self.m.get(cid).eff_charge(20, E), 0.2)
        cells = self.m.fetch([cid], now_turn=20, now_epoch=E)
        self.assertEqual(cells[0].body, "the full body")
        self.assertAlmostEqual(self.m.get(cid).eff_charge(20, E), 1.0, places=6)  # refreshed

    def test_sweep_reconciles_tiers_and_flags_archive(self):
        cid = self._w("will go cold", turn=0)
        res = self.m.sweep(now_turn=60, now_epoch=E)   # 60 turns >> half-life
        self.assertIn(cid, res["newly_archived"])
        self.assertEqual(self.m.get(cid).tier, "ARCHIVED")

    def test_separate_topics_when_no_overlap(self):
        a = self._w("trading signal logic", files=["strat.py"], concepts=["trading"], turn=0)
        b = self._w("gpu cracking rig", files=["hashcat.conf"], concepts=["cracking"], turn=0)
        self.assertNotEqual(self.m.get(a).topic_id, self.m.get(b).topic_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
