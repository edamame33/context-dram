"""
context-dram - a DRAM-modeled working-context memory.

The atom is a *cell*: a distilled observation + a `charge` (its relevance
"voltage"). Charge leaks exponentially every turn (the DRAM leak) and is
restored to full whenever the cell is referenced (refresh-on-use). Charge maps
to a thermal tier, which maps to placement in the context hierarchy:

    HOT      -> injected into the live window (the row buffer)
    WARM     -> resident in this store, one recall away
    COLD     -> index-only (title + id)
    ARCHIVED -> evicted to durable storage (Mempalace)

Decay is LAZY: we store the charge plus the turn it was set, and compute the
*current* charge on read. No background timer walks every cell every turn -
that blind sweep is exactly DRAM's dumb refresh, which this design improves on
by making refresh usage-driven instead.

stdlib only. Target: py -3.13.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional


# --------------------------------------------------------------------------- #
# Config - the capacitor + hierarchy knobs (spec section 5)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    half_life_turns: float = 7.0      # turns-untouched -> half relevance (main knob)
    session_gap_decay: float = 0.70   # per-day inter-session aging multiplier
    neighbor_boost: float = 0.30      # spatial-locality bump to row neighbors
    hot_threshold: float = 0.60
    warm_threshold: float = 0.20
    cold_threshold: float = 0.05      # below this -> ARCHIVED
    hot_token_budget: int = 40_000    # the row-buffer width, in tokens
    row_join_threshold: float = 0.30  # Jaccard overlap to join an existing row
    recent_window: int = 25           # turns, for "recent rows" candidates
    strengthening: bool = False       # use-frequency -> slower decay

    @property
    def lam(self) -> float:
        """Per-turn retention factor lambda, derived from the half-life."""
        return 0.5 ** (1.0 / self.half_life_turns)


DEFAULT = Config()
TIERS = ("HOT", "WARM", "COLD", "ARCHIVED")


# --------------------------------------------------------------------------- #
# Pure functions - the charge math (no DB, trivially testable)
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def effective_charge(stored_charge: float, charge_updated_turn: int,
                     last_refresh_epoch: float, now_turn: int, now_epoch: float,
                     *, pinned: bool = False, refresh_count: int = 0,
                     cfg: Config = DEFAULT) -> float:
    """The capacitor's voltage *right now*, after leaking since last written.

    Pinned cells are held at the top of the rail (the never-leak floor - the
    task header, standing constraints).
    """
    if pinned:
        return 1.0
    turns_idle = max(0, now_turn - charge_updated_turn)
    lam = cfg.lam
    if cfg.strengthening and refresh_count > 0:
        # long-term potentiation: a cell you keep using leaks slower
        half_life = cfg.half_life_turns * (1.0 + 0.5 * math.log1p(refresh_count))
        lam = 0.5 ** (1.0 / half_life)
    c = stored_charge * (lam ** turns_idle)               # intra-session leak
    days_idle = (now_epoch - last_refresh_epoch) / 86400.0
    if days_idle > 0:
        c *= cfg.session_gap_decay ** days_idle            # inter-session aging
    return _clamp(c)


def tier_for(charge: float, cfg: Config = DEFAULT) -> str:
    if charge >= cfg.hot_threshold:
        return "HOT"
    if charge >= cfg.warm_threshold:
        return "WARM"
    if charge >= cfg.cold_threshold:
        return "COLD"
    return "ARCHIVED"


def est_tokens(*texts: Optional[str]) -> int:
    """Rough token estimate at ~4 chars/token."""
    n = sum(len(t) for t in texts if t)
    return max(1, n // 4)


def _jaccard(a: set, b: set) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


# --------------------------------------------------------------------------- #
# The cell
# --------------------------------------------------------------------------- #
@dataclass
class Cell:
    id: int
    project: str
    topic_id: int
    session_id: str
    type: str
    title: str
    body: str
    facts: list
    files: list
    concepts: list
    charge: float
    charge_updated_turn: int
    last_refresh_turn: int
    created_turn: int
    refresh_count: int
    pinned: bool
    tier: str
    discovery_tokens: int
    mempalace_ref: Optional[str]
    created_at_epoch: float
    last_refresh_epoch: float

    def eff_charge(self, now_turn: int, now_epoch: float, cfg: Config = DEFAULT) -> float:
        return effective_charge(self.charge, self.charge_updated_turn,
                                self.last_refresh_epoch, now_turn, now_epoch,
                                pinned=self.pinned, refresh_count=self.refresh_count,
                                cfg=cfg)

    def est_tokens(self) -> int:
        return est_tokens(self.title, self.body, json.dumps(self.facts))


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
SCHEMA_CORE = """
CREATE TABLE IF NOT EXISTS cells (
  id                   INTEGER PRIMARY KEY,
  content_hash         TEXT UNIQUE,
  project              TEXT NOT NULL,
  topic_id             INTEGER NOT NULL,
  session_id           TEXT NOT NULL,
  type                 TEXT NOT NULL,
  title                TEXT NOT NULL,
  body                 TEXT,
  facts                TEXT,
  files                TEXT,
  concepts             TEXT,
  charge               REAL    NOT NULL DEFAULT 1.0,
  charge_updated_turn  INTEGER NOT NULL,
  last_refresh_turn    INTEGER NOT NULL,
  created_turn         INTEGER NOT NULL,
  refresh_count        INTEGER NOT NULL DEFAULT 0,
  pinned               INTEGER NOT NULL DEFAULT 0,
  tier                 TEXT    NOT NULL DEFAULT 'HOT',
  discovery_tokens     INTEGER DEFAULT 0,
  mempalace_ref        TEXT,
  created_at_epoch     REAL NOT NULL,
  last_refresh_epoch   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cells_proj_tier ON cells(project, tier);
CREATE INDEX IF NOT EXISTS idx_cells_topic     ON cells(topic_id);

CREATE TABLE IF NOT EXISTS topic_rows (
  topic_id          INTEGER PRIMARY KEY,
  project           TEXT NOT NULL,
  label             TEXT,
  files             TEXT,
  concepts          TEXT,
  last_active_turn  INTEGER
);
"""

SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS cells_fts
  USING fts5(title, facts, concepts, content='cells', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS cells_ai AFTER INSERT ON cells BEGIN
  INSERT INTO cells_fts(rowid, title, facts, concepts)
    VALUES (new.id, new.title, new.facts, new.concepts);
END;
CREATE TRIGGER IF NOT EXISTS cells_ad AFTER DELETE ON cells BEGIN
  INSERT INTO cells_fts(cells_fts, rowid, title, facts, concepts)
    VALUES ('delete', old.id, old.title, old.facts, old.concepts);
END;
CREATE TRIGGER IF NOT EXISTS cells_au AFTER UPDATE ON cells
WHEN old.title <> new.title OR old.facts <> new.facts OR old.concepts <> new.concepts
BEGIN
  INSERT INTO cells_fts(cells_fts, rowid, title, facts, concepts)
    VALUES ('delete', old.id, old.title, old.facts, old.concepts);
  INSERT INTO cells_fts(rowid, title, facts, concepts)
    VALUES (new.id, new.title, new.facts, new.concepts);
END;
"""


# --------------------------------------------------------------------------- #
# The controller
# --------------------------------------------------------------------------- #
class Memory:
    """The memory controller: write / refresh / page_in / recall / fetch / sweep."""

    def __init__(self, db_path: str = ":memory:", cfg: Config = DEFAULT):
        self.cfg = cfg
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(SCHEMA_CORE)
        try:
            self.db.executescript(SCHEMA_FTS)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False
        self.db.commit()

    # -- write path: distilled turn -> cell @ full charge --------------------
    def write(self, *, type: str, title: str, body: str = "",
              facts: Optional[list] = None, files: Optional[list] = None,
              concepts: Optional[list] = None, session_id: str, project: str,
              now_turn: int, now_epoch: Optional[float] = None,
              discovery_tokens: int = 0, pinned: bool = False) -> int:
        now_epoch = time.time() if now_epoch is None else now_epoch
        facts, files, concepts = facts or [], files or [], concepts or []
        chash = hashlib.sha256(f"{project}|{title}|{body}".encode()).hexdigest()[:16]

        existing = self.db.execute(
            "SELECT id FROM cells WHERE content_hash=?", (chash,)).fetchone()
        if existing:                       # re-observing the same thing = a refresh
            self.refresh([existing["id"]], now_turn, now_epoch)
            return existing["id"]

        topic_id = self.assign_topic(files, concepts, project, now_turn)
        cur = self.db.execute(
            """INSERT INTO cells(content_hash, project, topic_id, session_id, type,
                 title, body, facts, files, concepts, charge, charge_updated_turn,
                 last_refresh_turn, created_turn, refresh_count, pinned, tier,
                 discovery_tokens, mempalace_ref, created_at_epoch, last_refresh_epoch)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chash, project, topic_id, session_id, type, title, body,
             json.dumps(facts), json.dumps(files), json.dumps(concepts),
             1.0, now_turn, now_turn, now_turn, 0, int(pinned), "HOT",
             discovery_tokens, None, now_epoch, now_epoch))
        self.db.commit()
        return cur.lastrowid

    # -- topic clustering: join the best-overlapping recent row, else mint ---
    def assign_topic(self, files: list, concepts: list, project: str,
                     now_turn: int) -> int:
        sig = set(files) | set(concepts)
        candidates = self.db.execute(
            "SELECT topic_id, files, concepts FROM topic_rows "
            "WHERE project=? AND last_active_turn >= ?",
            (project, now_turn - self.cfg.recent_window)).fetchall()
        best_id, best_score = None, 0.0
        for r in candidates:
            rsig = set(json.loads(r["files"] or "[]")) | set(json.loads(r["concepts"] or "[]"))
            s = _jaccard(sig, rsig)
            if s > best_score:
                best_id, best_score = r["topic_id"], s
        if best_id is not None and best_score >= self.cfg.row_join_threshold:
            r = self.db.execute(
                "SELECT files, concepts FROM topic_rows WHERE topic_id=?",
                (best_id,)).fetchone()
            nf = sorted(set(json.loads(r["files"] or "[]")) | set(files))
            nc = sorted(set(json.loads(r["concepts"] or "[]")) | set(concepts))
            self.db.execute(
                "UPDATE topic_rows SET files=?, concepts=?, last_active_turn=? WHERE topic_id=?",
                (json.dumps(nf), json.dumps(nc), now_turn, best_id))
            return best_id
        label = concepts[0] if concepts else (files[0] if files else "misc")
        cur = self.db.execute(
            "INSERT INTO topic_rows(project, label, files, concepts, last_active_turn) "
            "VALUES(?,?,?,?,?)",
            (project, label, json.dumps(sorted(set(files))),
             json.dumps(sorted(set(concepts))), now_turn))
        return cur.lastrowid

    # -- refresh-on-use: restore referenced cells, bump row neighbors --------
    def refresh(self, cell_ids: Iterable[int], now_turn: int,
                now_epoch: Optional[float] = None) -> None:
        now_epoch = time.time() if now_epoch is None else now_epoch
        ids = list(cell_ids)
        if not ids:
            return
        qs = ",".join("?" * len(ids))
        self.db.execute(
            f"""UPDATE cells SET charge=1.0, charge_updated_turn=?, last_refresh_turn=?,
                last_refresh_epoch=?, refresh_count=refresh_count+1, tier='HOT'
                WHERE id IN ({qs})""",
            (now_turn, now_turn, now_epoch, *ids))

        # spatial locality: warm the neighbors in the same row(s)
        topics = [r["topic_id"] for r in self.db.execute(
            f"SELECT DISTINCT topic_id FROM cells WHERE id IN ({qs})", ids).fetchall()]
        for t in topics:
            neighbors = self.db.execute(
                f"SELECT * FROM cells WHERE topic_id=? AND pinned=0 AND id NOT IN ({qs})",
                (t, *ids)).fetchall()
            for n in neighbors:
                cur_eff = effective_charge(
                    n["charge"], n["charge_updated_turn"], n["last_refresh_epoch"],
                    now_turn, now_epoch, refresh_count=n["refresh_count"], cfg=self.cfg)
                newc = _clamp(cur_eff + self.cfg.neighbor_boost)
                self.db.execute(
                    "UPDATE cells SET charge=?, charge_updated_turn=? WHERE id=?",
                    (newc, now_turn, n["id"]))
        self.db.commit()

    def touch_by_file(self, paths: Iterable[str], now_turn: int,
                      now_epoch: Optional[float] = None) -> set:
        """Reliable refresh signal: a tool read/edited these files."""
        ids = set()
        for p in paths:
            for r in self.db.execute(
                    "SELECT id, files FROM cells WHERE files LIKE ?", (f"%{p}%",)).fetchall():
                if p in json.loads(r["files"] or "[]"):
                    ids.add(r["id"])
        self.refresh(ids, now_turn, now_epoch)
        return ids

    # -- page-in: the hot row buffer, capped by token budget -----------------
    def page_in(self, project: str, now_turn: int, now_epoch: Optional[float] = None,
                budget: Optional[int] = None) -> list:
        now_epoch = time.time() if now_epoch is None else now_epoch
        budget = self.cfg.hot_token_budget if budget is None else budget
        scored = []
        for r in self.db.execute(
                "SELECT * FROM cells WHERE project=? AND tier!='ARCHIVED'", (project,)).fetchall():
            c = self._cell(r)
            eff = c.eff_charge(now_turn, now_epoch, self.cfg)
            if c.pinned or eff >= self.cfg.hot_threshold:
                scored.append((eff, c))
        # pinned first, then highest charge first
        scored.sort(key=lambda x: (x[1].pinned, x[0]), reverse=True)
        out, used = [], 0
        for _eff, c in scored:
            t = c.est_tokens()
            if c.pinned or used + t <= budget:   # pinned always resident
                out.append(c)
                used += t
        return out

    # -- recall layer 1: cheap index (ids + titles, NO bodies) ---------------
    def recall(self, query: str, project: Optional[str] = None, limit: int = 20,
               now_turn: Optional[int] = None, now_epoch: Optional[float] = None) -> list:
        now_epoch = time.time() if now_epoch is None else now_epoch
        if self.fts_enabled:
            sql = ("SELECT c.id, c.title, c.type, c.charge, c.charge_updated_turn, "
                   "c.last_refresh_epoch, c.refresh_count, c.pinned, c.body, c.facts "
                   "FROM cells_fts f JOIN cells c ON c.id=f.rowid WHERE cells_fts MATCH ?")
            args: list = [query]
        else:
            sql = ("SELECT id, title, type, charge, charge_updated_turn, last_refresh_epoch, "
                   "refresh_count, pinned, body, facts FROM cells "
                   "WHERE title LIKE ? OR concepts LIKE ? OR facts LIKE ?")
            args = [f"%{query}%", f"%{query}%", f"%{query}%"]
        if project:
            sql += " AND c.project=?" if self.fts_enabled else " AND project=?"
            args.append(project)
        sql += " LIMIT ?"
        args.append(limit)
        rows = self.db.execute(sql, args).fetchall()

        index = []
        for r in rows:
            if now_turn is not None:
                eff = effective_charge(r["charge"], r["charge_updated_turn"],
                                       r["last_refresh_epoch"], now_turn, now_epoch,
                                       pinned=bool(r["pinned"]),
                                       refresh_count=r["refresh_count"], cfg=self.cfg)
            else:
                eff = r["charge"]
            # the cheap index row - deliberately NO body
            index.append({"id": r["id"], "title": r["title"], "type": r["type"],
                          "tier": tier_for(eff, self.cfg),
                          "read_tokens": est_tokens(r["body"], r["facts"])})
        return index

    # -- recall layer 3: full bodies for survivors (a fetch IS a reference) --
    def fetch(self, ids: Iterable[int], now_turn: int,
              now_epoch: Optional[float] = None) -> list:
        now_epoch = time.time() if now_epoch is None else now_epoch
        ids = list(ids)
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        rows = self.db.execute(f"SELECT * FROM cells WHERE id IN ({qs})", ids).fetchall()
        cells = [self._cell(r) for r in rows]
        self.refresh(ids, now_turn, now_epoch)     # recall counts as use
        return cells

    # -- sweep: reconcile cached tier, flag archive candidates ---------------
    def sweep(self, now_turn: int, now_epoch: Optional[float] = None) -> dict:
        now_epoch = time.time() if now_epoch is None else now_epoch
        counts = {t: 0 for t in TIERS}
        newly_archived = []
        for r in self.db.execute("SELECT * FROM cells").fetchall():
            c = self._cell(r)
            eff = c.eff_charge(now_turn, now_epoch, self.cfg)
            t = tier_for(eff, self.cfg)
            counts[t] += 1
            if t != r["tier"]:
                self.db.execute("UPDATE cells SET tier=? WHERE id=?", (t, r["id"]))
                if t == "ARCHIVED":
                    newly_archived.append(c.id)
        self.db.commit()
        return {"counts": counts, "newly_archived": newly_archived}

    def evict(self, cell_id: int, mempalace_ref: str) -> None:
        """Mark a cell evicted to durable storage (the Mempalace push is a hook concern)."""
        self.db.execute(
            "UPDATE cells SET tier='ARCHIVED', mempalace_ref=?, charge=0.0 WHERE id=?",
            (mempalace_ref, cell_id))
        self.db.commit()

    # -- helpers -------------------------------------------------------------
    def get(self, cell_id: int) -> Optional[Cell]:
        r = self.db.execute("SELECT * FROM cells WHERE id=?", (cell_id,)).fetchone()
        return self._cell(r) if r else None

    def _cell(self, r: sqlite3.Row) -> Cell:
        return Cell(
            id=r["id"], project=r["project"], topic_id=r["topic_id"],
            session_id=r["session_id"], type=r["type"], title=r["title"],
            body=r["body"] or "", facts=json.loads(r["facts"] or "[]"),
            files=json.loads(r["files"] or "[]"), concepts=json.loads(r["concepts"] or "[]"),
            charge=r["charge"], charge_updated_turn=r["charge_updated_turn"],
            last_refresh_turn=r["last_refresh_turn"], created_turn=r["created_turn"],
            refresh_count=r["refresh_count"], pinned=bool(r["pinned"]), tier=r["tier"],
            discovery_tokens=r["discovery_tokens"], mempalace_ref=r["mempalace_ref"],
            created_at_epoch=r["created_at_epoch"], last_refresh_epoch=r["last_refresh_epoch"])

    def close(self) -> None:
        self.db.close()


# --------------------------------------------------------------------------- #
# Demo: the discharge curve (a scope trace of one untouched cell)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cfg = Config()
    print(f"half-life {cfg.half_life_turns:g} turns  ->  lambda = {cfg.lam:.4f}\n")
    print(" turn | charge | tier")
    print("------+--------+----------")
    for t in range(0, 37, 3):
        c = effective_charge(1.0, 0, 0.0, t, 0.0, cfg=cfg)
        print(f"{t:5d} | {c:6.3f} | {tier_for(c, cfg)}")
