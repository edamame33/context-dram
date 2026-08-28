"""Tests for context-dram - proving the capacitor physics.

Run:  py -3.13 test_memory.py
"""
import json
import tempfile
import unittest
from pathlib import Path

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
        # five cells, ~100 tokens of body each (400 chars), all hot at turn 0.
        # The budget invariant must hold; with the fairness cap active, oversized
        # cells arrive as title-only stubs, so the count may exceed the uncapped 3.
        for i in range(5):
            self._w(f"cell {i}", body="z" * 400, turn=0)
        out = self.m.page_in("proj", now_turn=0, now_epoch=E, budget=250)
        self.assertLessEqual(sum(c.est_tokens() for c in out), 250)

    def test_page_in_uncapped_matches_legacy_selection(self):
        m = Memory(cfg=Config(cell_budget_frac=None))
        for i in range(5):
            m.write(type="fact", title=f"cell {i}", body="z" * 400,
                    session_id="s", project="proj", now_turn=0, now_epoch=E)
        out = m.page_in("proj", now_turn=0, now_epoch=E, budget=250)
        self.assertLessEqual(len(out), 3)          # legacy greedy fill, whole cells
        self.assertLessEqual(sum(c.est_tokens() for c in out), 250)
        m.close()

    def test_page_in_giant_cell_cannot_starve_topics(self):
        # one ~500-token monster plus small cells: the cap demotes the monster
        # to a stub so the small cells still page in
        self._w("monster", body="z" * 2000, turn=0)
        small = [self._w(f"small {i}", body="z" * 40, turn=0) for i in range(3)]
        out = self.m.page_in("proj", now_turn=0, now_epoch=E, budget=300)
        got = {c.id for c in out}
        for cid in small:
            self.assertIn(cid, got)
        self.assertLessEqual(sum(c.est_tokens() for c in out), 300)

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


class TestRobustnessUpgrades(unittest.TestCase):
    """Regression locks for the faster+bulletproof upgrade pass."""

    def setUp(self):
        self.m = Memory(":memory:", Config(half_life_turns=7))

    def _w(self, title, **kw):
        kw.setdefault("body", "x")
        return self.m.write(type="fact", title=title, session_id="s1",
                            project="proj", now_turn=kw.pop("turn", 0),
                            now_epoch=E, **kw)

    # -- recall-hardening ----------------------------------------------------
    def test_recall_survives_fts_special_chars(self):
        self._w("license-check at 0x4A2F", concepts=["license"])
        for q in ['license-check', '"unbalanced', 'a AND b', '(paren', 'a OR', 'NOT x']:
            with self.subTest(q=q):
                self.m.recall(q, project="proj", now_turn=0, now_epoch=E)  # must not raise

    def test_recall_empty_query_returns_empty(self):
        self._w("something")
        self.assertEqual(self.m.recall("   ", project="proj"), [])

    def test_recall_offset_query_hits(self):
        self._w("Frida inline hook at offset 0x4A2F")
        hits = self.m.recall("0x4A2F", project="proj", now_turn=0, now_epoch=E)
        self.assertGreaterEqual(len(hits), 1)

    def test_non_fts_fallback_respects_project(self):
        # force the LIKE path and prove the project filter binds (was a precedence bug)
        self.m.fts_enabled = False
        self._w("shared title token QZ", concepts=["x"])
        self.m.write(type="fact", title="shared title token QZ", body="y",
                     session_id="s1", project="other", now_turn=0, now_epoch=E)
        hits = self.m.recall("QZ", project="proj", now_turn=0, now_epoch=E)
        self.assertEqual(len(hits), 1)   # only the 'proj' cell, not 'other'

    # -- distiller noise gate + payload-aware dedup --------------------------
    def test_distinct_turns_same_title_do_not_collide(self):
        a = self._w("update", files=["a.py"])
        b = self._w("update", files=["b.py"])   # same title, different evidence
        self.assertNotEqual(a, b)
        n = self.m.db.execute("SELECT COUNT(*) n FROM cells").fetchone()["n"]
        self.assertEqual(n, 2)

    # -- cell_files junction: reliable refresh incl. Windows paths -----------
    def test_touch_by_windows_path_refreshes(self):
        cid = self._w("edits a module", files=[r"C:\proj\auth.py"])
        touched = self.m.touch_by_file([r"C:\proj\auth.py"], now_turn=9, now_epoch=E)
        self.assertIn(cid, touched)

    def test_touch_by_file_no_superstring_false_positive(self):
        cid = self._w("edits auth", files=["auth.py"])
        touched = self.m.touch_by_file(["h.py"], now_turn=9, now_epoch=E)  # substring of auth.py
        self.assertNotIn(cid, touched)

    # -- session-scoped turn decay ------------------------------------------
    def test_cross_session_uses_epoch_not_turn(self):
        # a cell written in session A at turn 40; a new session B starts at turn 0.
        # Turn decay must NOT fire (40 > 0 would clamp), only epoch aging applies.
        cid = self.m.write(type="fact", title="cross", body="x", session_id="A",
                           project="proj", now_turn=40, now_epoch=E)
        c = self.m.get(cid)
        same_epoch = c.eff_charge(0, E, now_session="B")
        self.assertAlmostEqual(same_epoch, 1.0, places=6)   # no turn decay across sessions

    def test_same_session_turn_decay_still_applies(self):
        cid = self.m.write(type="fact", title="intra", body="x", session_id="A",
                           project="proj", now_turn=0, now_epoch=E)
        c = self.m.get(cid)
        self.assertAlmostEqual(c.eff_charge(7, E, now_session="A"), 0.5, places=4)

    # -- neighbor-boost epoch write -----------------------------------------
    def test_neighbor_boost_persists_across_session_gap(self):
        a = self.m.write(type="fact", title="entry", body="x", files=["m.py"],
                         concepts=["m"], session_id="A", project="proj",
                         now_turn=0, now_epoch=E)
        b = self.m.write(type="fact", title="near", body="x", files=["m.py"],
                         concepts=["m"], session_id="A", project="proj",
                         now_turn=0, now_epoch=E)
        self.assertEqual(self.m.get(a).topic_id, self.m.get(b).topic_id)
        later = E + 3 * 86400
        pre = self.m.get(b).eff_charge(0, later, now_session="B")
        self.m.refresh([a], now_turn=0, now_epoch=later, session_id="B")  # boost b
        post = self.m.get(b).eff_charge(0, later, now_session="B")
        self.assertGreater(post, pre)   # boost is a gain, not epoch-decayed away

    # -- atomic capture ------------------------------------------------------
    def test_capture_turn_is_atomic_and_touches(self):
        seed = self._w("existing", files=["shared.py"])
        cid = self.m.capture_turn(type="fact", title="new work",
                                  files=["shared.py"], files_touched=["shared.py"],
                                  session_id="s1", project="proj", now_turn=3,
                                  now_epoch=E)
        self.assertTrue(cid)
        self.assertEqual(self.m.get(seed).refresh_count, 1)  # neighbor/file touch landed

    # -- maintenance: lossless archive prune --------------------------------
    def test_maintenance_prunes_archived_and_exports(self):
        cid = self._w("will archive", turn=0)
        pin = self.m.write(type="fact", title="pinned header", body="x",
                           session_id="s1", project="proj", now_turn=0,
                           now_epoch=E, pinned=True)
        sidecar = Path(tempfile.mkdtemp()) / "archived.jsonl"
        far = E + 999 * 86400   # far future -> the unpinned cell is long-idle ARCHIVED
        rep = self.m.maintenance(now_turn=0, now_epoch=far, archive_after_days=30,
                                 sidecar_path=str(sidecar))
        self.assertEqual(rep["pruned"], 1)
        self.assertIsNone(self.m.get(cid))          # deleted
        self.assertIsNotNone(self.m.get(pin))       # pinned survives
        lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)             # exported before delete
        json.loads(lines[0])                        # valid JSON row

    def test_fts_selfheal_rebuilds_on_reopen(self):
        db = Path(tempfile.mkdtemp()) / "cells.sqlite3"
        m1 = Memory(str(db))
        m1.write(type="fact", title="findme token PLUTO", body="x",
                 session_id="s", project="proj", now_turn=0, now_epoch=E)
        m1.close()
        m2 = Memory(str(db))                         # reopen -> self-heal path
        hits = m2.recall("PLUTO", project="proj", now_turn=0, now_epoch=E)
        m2.close()
        self.assertGreaterEqual(len(hits), 1)


class TestChargeProperties(unittest.TestCase):
    """Property checks on the pure decay function (deterministic pseudo-random)."""

    def test_charge_bounded_and_monotone_in_time(self):
        # LCG so the test is dependency-free and reproducible
        seed = 12345
        for _ in range(4000):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            stored = (seed % 1000) / 1000.0
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            ut = seed % 50
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            nt = ut + (seed % 50)
            c1 = effective_charge(stored, ut, E, nt, E)
            c2 = effective_charge(stored, ut, E, nt, E + 86400)  # one day later
            self.assertTrue(0.0 <= c1 <= 1.0)
            self.assertTrue(0.0 <= c2 <= 1.0)
            self.assertLessEqual(c2, c1 + 1e-9)   # more elapsed time never raises charge


if __name__ == "__main__":
    unittest.main(verbosity=2)
