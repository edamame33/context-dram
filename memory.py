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

Turn decay is session-scoped: the turn clock restarts at 0 every session, so
comparing turn numbers across sessions is meaningless. A cell's stored session
travels with its charge; turn decay applies only within the same session, and
cross-session aging is driven by wall-clock (epoch) decay alone.

stdlib only. Target: py -3.13 (works on 3.10+).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
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
    topic_recent_days: float = 2.0    # epoch window for cross-session topic joins
    strengthening: bool = False       # use-frequency -> slower decay
    cell_budget_frac: Optional[float] = 0.4  # max fraction of the page_in budget one
                                             # non-pinned cell may occupy (None = uncapped)

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
                     cell_session: Optional[str] = None,
                     now_session: Optional[str] = None,
                     cfg: Config = DEFAULT) -> float:
    """The capacitor's voltage *right now*, after leaking since last written.

    Pinned cells are held at the top of the rail (the never-leak floor - the
    task header, standing constraints).

    Turn decay is session-scoped: it applies only when cell_session and
    now_session are both known and equal. When either is None we assume the
    same session (backward compatible); when they differ, aging is epoch-only,
    because turn numbers from different sessions share no clock.
    """
    if pinned:
        return 1.0
    same_session = (cell_session is None or now_session is None
                    or cell_session == now_session)
    turns_idle = max(0, now_turn - charge_updated_turn) if same_session else 0
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


def fts_phrase_query(query: str) -> str:
    """Sanitize arbitrary user text into an FTS5 phrase query.

    Every whitespace token becomes a quoted phrase, so operator characters
    ('-', '(', unbalanced '"', AND/OR/NOT) are matched literally instead of
    raising a syntax error. Returns '' for an empty/whitespace query.
    """
    return " ".join('"' + t.replace('"', '""') + '"' for t in query.split())


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
    charge_updated_session: str = field(default="")

    def eff_charge(self, now_turn: int, now_epoch: float, cfg: Config = DEFAULT,
                   *, now_session: Optional[str] = None) -> float:
        return effective_charge(self.charge, self.charge_updated_turn,
                                self.last_refresh_epoch, now_turn, now_epoch,
                                pinned=self.pinned, refresh_count=self.refresh_count,
                                cell_session=self.charge_updated_session or None,
                                now_session=now_session, cfg=cfg)

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
  last_refresh_epoch   REAL NOT NULL,
  charge_updated_session TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cells_proj_tier ON cells(project, tier);
CREATE INDEX IF NOT EXISTS idx_cells_topic     ON cells(topic_id);

CREATE TABLE IF NOT EXISTS topic_rows (
  topic_id            INTEGER PRIMARY KEY,
  project             TEXT NOT NULL,
  label               TEXT,
  files               TEXT,
  concepts            TEXT,
  last_active_turn    INTEGER,
  last_active_epoch   REAL NOT NULL DEFAULT 0,
  last_active_session TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_topics_proj_active ON topic_rows(project, last_active_turn);

CREATE TABLE IF NOT EXISTS cell_files (
  file    TEXT    NOT NULL,
  cell_id INTEGER NOT NULL,
  PRIMARY KEY (file, cell_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v INTEGER NOT NULL DEFAULT 0
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

USER_VERSION = 1   # bumped when a migration must run on pre-existing DBs


# --------------------------------------------------------------------------- #
# The controller
# --------------------------------------------------------------------------- #
class Memory:
    """The memory controller: write / refresh / page_in / recall / fetch / sweep."""

    def __init__(self, db_path: str = ":memory:", cfg: Config = DEFAULT):
        self.cfg = cfg
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._explicit_txn = False        # capture_turn() suppresses inner commits
        try:
            # WAL + NORMAL is the standard durable-enough pairing for a cache DB;
            # both wrapped so read-only/odd filesystems still work.
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
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
        self._migrate()
        if self.fts_enabled:
            self._fts_selfheal()

    def _migrate(self) -> None:
        """Bring a pre-existing DB up to USER_VERSION, atomically and race-safely.

        Detached capture workers can race a session-start prime here; the loser
        of BEGIN IMMEDIATE waits (5s default busy timeout), re-checks, no-ops.
        On any failure the DB is left as-is and this connection runs in legacy
        mode (self._v1 False) with exactly the old behavior.
        """
        try:
            ver = self.db.execute("PRAGMA user_version").fetchone()[0]
            if ver < USER_VERSION:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    ver = self.db.execute("PRAGMA user_version").fetchone()[0]
                    if ver < USER_VERSION:
                        for ddl in (
                            "ALTER TABLE cells ADD COLUMN charge_updated_session TEXT NOT NULL DEFAULT ''",
                            "ALTER TABLE topic_rows ADD COLUMN last_active_epoch REAL NOT NULL DEFAULT 0",
                            "ALTER TABLE topic_rows ADD COLUMN last_active_session TEXT NOT NULL DEFAULT ''",
                        ):
                            try:
                                self.db.execute(ddl)
                            except sqlite3.OperationalError as e:
                                if "duplicate column" not in str(e).lower():
                                    raise           # real failure -> rollback below
                        # backfill the file->cell junction from the JSON column
                        for r in self.db.execute("SELECT id, files FROM cells").fetchall():
                            for f in json.loads(r["files"] or "[]"):
                                self.db.execute(
                                    "INSERT OR IGNORE INTO cell_files(file, cell_id) VALUES(?,?)",
                                    (f, r["id"]))
                        self.db.execute(f"PRAGMA user_version={USER_VERSION}")
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            self._v1 = True
        except sqlite3.OperationalError:
            self._v1 = False     # locked or read-only: legacy behavior this run

    def _fts_selfheal(self) -> None:
        """Rebuild a diverged external-content FTS index (cheap gate).

        The reopen-divergence signature is cells rows present but an empty FTS
        index (SCHEMA_FTS created fresh over an existing cells table). Two
        count()s gate the O(n) rebuild so normal inits stay fast; the thorough
        integrity-check probe lives in maintenance(), not here.
        """
        try:
            n_cells = self.db.execute("SELECT count(*) FROM cells").fetchone()[0]
            if n_cells == 0:
                return
            n_fts = self.db.execute("SELECT count(*) FROM cells_fts").fetchone()[0]
            if n_fts == 0:
                self.db.execute("INSERT INTO cells_fts(cells_fts) VALUES('rebuild')")
                self.db.commit()
        except sqlite3.Error:
            pass

    def _commit(self) -> None:
        """Commit unless capture_turn() holds an explicit outer transaction."""
        if not self._explicit_txn:
            self.db.commit()

    # -- write path: distilled turn -> cell @ full charge --------------------
    def write(self, *, type: str, title: str, body: str = "",
              facts: Optional[list] = None, files: Optional[list] = None,
              concepts: Optional[list] = None, session_id: str, project: str,
              now_turn: int, now_epoch: Optional[float] = None,
              discovery_tokens: int = 0, pinned: bool = False) -> int:
        now_epoch = time.time() if now_epoch is None else now_epoch
        facts, files, concepts = facts or [], files or [], concepts or []
        # Hash the full distilled payload, not just title|body: distinct turns
        # sharing a generic title must not silently collapse into one cell.
        chash = hashlib.sha256(
            f"{project}|{title}|{body}|{json.dumps(facts)}|{json.dumps(sorted(files))}"
            .encode()).hexdigest()[:16]

        existing = self.db.execute(
            "SELECT id FROM cells WHERE content_hash=?", (chash,)).fetchone()
        if existing:                       # re-observing the same thing = a refresh
            self.refresh([existing["id"]], now_turn, now_epoch, session_id=session_id)
            return existing["id"]

        topic_id = self.assign_topic(files, concepts, project, now_turn,
                                     now_epoch=now_epoch, session_id=session_id)
        cur = self.db.execute(
            """INSERT INTO cells(content_hash, project, topic_id, session_id, type,
                 title, body, facts, files, concepts, charge, charge_updated_turn,
                 last_refresh_turn, created_turn, refresh_count, pinned, tier,
                 discovery_tokens, mempalace_ref, created_at_epoch, last_refresh_epoch,
                 charge_updated_session)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(content_hash) DO NOTHING""",
            (chash, project, topic_id, session_id, type, title, body,
             json.dumps(facts), json.dumps(files), json.dumps(concepts),
             1.0, now_turn, now_turn, now_turn, 0, int(pinned), "HOT",
             discovery_tokens, None, now_epoch, now_epoch, session_id))
        if cur.rowcount == 0:
            # a concurrent writer won the race between our SELECT and INSERT;
            # lastrowid is stale after a conflict - re-select by hash instead
            row = self.db.execute(
                "SELECT id FROM cells WHERE content_hash=?", (chash,)).fetchone()
            if row:
                self.refresh([row["id"]], now_turn, now_epoch, session_id=session_id)
                return row["id"]
            self._commit()
            return 0
        cid = cur.lastrowid
        if getattr(self, "_v1", False):
            for f in files:
                self.db.execute(
                    "INSERT OR IGNORE INTO cell_files(file, cell_id) VALUES(?,?)",
                    (f, cid))
        self._commit()
        return cid

    def capture_turn(self, *, files_touched: Optional[list] = None,
                     session_id: str, project: str, now_turn: int,
                     now_epoch: Optional[float] = None, **cell_fields) -> Optional[int]:
        """All-or-nothing capture: write() + touch_by_file() in one transaction.

        Without this, a failure between the two commits leaves a half-applied
        turn. BEGIN IMMEDIATE also serializes racing detached workers.
        """
        now_epoch = time.time() if now_epoch is None else now_epoch
        if self.db.in_transaction:
            self.db.rollback()
        self.db.execute("BEGIN IMMEDIATE")
        self._explicit_txn = True
        try:
            cid = self.write(session_id=session_id, project=project,
                             now_turn=now_turn, now_epoch=now_epoch, **cell_fields)
            if files_touched:
                self.touch_by_file(files_touched, now_turn, now_epoch,
                                   session_id=session_id)
            self.db.commit()
            return cid
        except Exception:
            self.db.rollback()
            raise
        finally:
            self._explicit_txn = False

    # -- topic clustering: join the best-overlapping recent row, else mint ---
    def assign_topic(self, files: list, concepts: list, project: str,
                     now_turn: int, now_epoch: Optional[float] = None,
                     session_id: str = "") -> int:
        now_epoch = time.time() if now_epoch is None else now_epoch
        sig = set(files) | set(concepts)
        if getattr(self, "_v1", False):
            # recency = same-session turn window OR wall-clock window; the old
            # pure-turn predicate shrank/inverted when the turn clock reset
            candidates = self.db.execute(
                "SELECT topic_id, files, concepts FROM topic_rows WHERE project=? "
                "AND ((last_active_session=? AND last_active_turn >= ?) "
                "     OR last_active_epoch >= ?)",
                (project, session_id, now_turn - self.cfg.recent_window,
                 now_epoch - self.cfg.topic_recent_days * 86400.0)).fetchall()
        else:
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
                "UPDATE topic_rows SET files=?, concepts=?, last_active_turn=?, "
                "last_active_epoch=?, last_active_session=? WHERE topic_id=?",
                (json.dumps(nf), json.dumps(nc), now_turn, now_epoch,
                 session_id, best_id))
            return best_id
        label = concepts[0] if concepts else (files[0] if files else "misc")
        cur = self.db.execute(
            "INSERT INTO topic_rows(project, label, files, concepts, last_active_turn, "
            "last_active_epoch, last_active_session) VALUES(?,?,?,?,?,?,?)",
            (project, label, json.dumps(sorted(set(files))),
             json.dumps(sorted(set(concepts))), now_turn, now_epoch, session_id))
        return cur.lastrowid

    # -- refresh-on-use: restore referenced cells, bump row neighbors --------
    def refresh(self, cell_ids: Iterable[int], now_turn: int,
                now_epoch: Optional[float] = None,
                session_id: Optional[str] = None) -> None:
        now_epoch = time.time() if now_epoch is None else now_epoch
        sess = session_id or ""    # unknown session -> '' sentinel (epoch-only later)
        ids = list(cell_ids)
        if not ids:
            return
        qs = ",".join("?" * len(ids))
        self.db.execute(
            f"""UPDATE cells SET charge=1.0, charge_updated_turn=?, last_refresh_turn=?,
                last_refresh_epoch=?, refresh_count=refresh_count+1, tier='HOT',
                charge_updated_session=?
                WHERE id IN ({qs})""",
            (now_turn, now_turn, now_epoch, sess, *ids))

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
                    now_turn, now_epoch, refresh_count=n["refresh_count"],
                    cell_session=(n["charge_updated_session"] or None
                                  if "charge_updated_session" in n.keys() else None),
                    now_session=session_id, cfg=self.cfg)
                newc = _clamp(cur_eff + self.cfg.neighbor_boost)
                # last_refresh_epoch must advance with the stored charge, or the
                # boost gets epoch-decayed a second time on the next read (a
                # strict penalty across session gaps). last_refresh_turn and
                # refresh_count stay untouched: boosted != referenced.
                self.db.execute(
                    "UPDATE cells SET charge=?, charge_updated_turn=?, "
                    "last_refresh_epoch=?, charge_updated_session=? WHERE id=?",
                    (newc, now_turn, now_epoch, sess, n["id"]))
        self._commit()

    def touch_by_file(self, paths: Iterable[str], now_turn: int,
                      now_epoch: Optional[float] = None,
                      session_id: Optional[str] = None) -> set:
        """Reliable refresh signal: a tool read/edited these files."""
        paths = [p for p in paths if p]
        if not paths:
            return set()
        ids = set()
        if getattr(self, "_v1", False):
            qs = ",".join("?" * len(paths))
            for r in self.db.execute(
                    f"SELECT DISTINCT cell_id FROM cell_files WHERE file IN ({qs})",
                    paths).fetchall():
                ids.add(r["cell_id"])
        else:
            # legacy LIKE scan (kept as mid-upgrade fallback); note LIKE treats
            # backslashes/percents literally enough here because the Python
            # membership re-check below is the actual filter
            for p in paths:
                for r in self.db.execute(
                        "SELECT id, files FROM cells WHERE files LIKE ?",
                        (f"%{p}%",)).fetchall():
                    if p in json.loads(r["files"] or "[]"):
                        ids.add(r["id"])
        self.refresh(ids, now_turn, now_epoch, session_id=session_id)
        return ids

    # -- page-in: the hot row buffer, capped by token budget -----------------
    def page_in(self, project: str, now_turn: int, now_epoch: Optional[float] = None,
                budget: Optional[int] = None,
                session_id: Optional[str] = None) -> list:
        now_epoch = time.time() if now_epoch is None else now_epoch
        budget = self.cfg.hot_token_budget if budget is None else budget

        # SQL-side prefilter: a non-pinned cell can only reach hot_threshold if
        # its epoch age is small enough, because stored charge <= 1.0 everywhere
        # (write=1.0, refresh=1.0, neighbor boost clamped). If a future feature
        # ever stores charge > 1.0, this bound becomes unsound - remove it then.
        sgd, hot = self.cfg.session_gap_decay, self.cfg.hot_threshold
        if 0.0 < sgd < 1.0 and 0.0 < hot <= 1.0:
            k_days = math.log(hot) / math.log(sgd)
            cutoff = now_epoch - (k_days + 0.01) * 86400.0
            rows = self.db.execute(
                "SELECT * FROM cells WHERE project=? AND tier!='ARCHIVED' "
                "AND (pinned=1 OR last_refresh_epoch >= ?)",
                (project, cutoff)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM cells WHERE project=? AND tier!='ARCHIVED'",
                (project,)).fetchall()

        scored = []
        for r in rows:
            c = self._cell(r)
            eff = c.eff_charge(now_turn, now_epoch, self.cfg, now_session=session_id)
            if c.pinned or eff >= self.cfg.hot_threshold:
                scored.append((eff, c))
        # pinned first, then highest charge first; id breaks ties deterministically
        scored.sort(key=lambda x: (x[1].pinned, x[0], x[1].id), reverse=True)

        frac = self.cfg.cell_budget_frac
        cap = int(frac * budget) if frac else None
        out, used = [], 0
        for _eff, c in scored:
            if cap is not None and not c.pinned and c.est_tokens() > cap:
                # one giant cell must not starve every other topic: demote it to
                # a title-only stub (never mutates the DB row)
                c = replace(c, body="")
            t = c.est_tokens()
            if c.pinned or used + t <= budget:   # pinned always resident
                out.append(c)
                used += t
        return out

    # -- recall layer 1: cheap index (ids + titles, NO bodies) ---------------
    def _recall_like(self, query: str, project: Optional[str], limit: int) -> list:
        sql = ("SELECT id, title, type, charge, charge_updated_turn, last_refresh_epoch, "
               "refresh_count, pinned, body, facts FROM cells "
               "WHERE (title LIKE ? OR concepts LIKE ? OR facts LIKE ?)")
        args: list = [f"%{query}%", f"%{query}%", f"%{query}%"]
        if project:
            sql += " AND project=?"
            args.append(project)
        sql += " LIMIT ?"
        args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def recall(self, query: str, project: Optional[str] = None, limit: int = 20,
               now_turn: Optional[int] = None, now_epoch: Optional[float] = None) -> list:
        now_epoch = time.time() if now_epoch is None else now_epoch
        if not query.split():
            return []
        rows = None
        if self.fts_enabled:
            sql = ("SELECT c.id, c.title, c.type, c.charge, c.charge_updated_turn, "
                   "c.last_refresh_epoch, c.refresh_count, c.pinned, c.body, c.facts "
                   "FROM cells_fts f JOIN cells c ON c.id=f.rowid WHERE cells_fts MATCH ?")
            args: list = [fts_phrase_query(query)]
            if project:
                sql += " AND c.project=?"
                args.append(project)
            sql += " LIMIT ?"
            args.append(limit)
            try:
                rows = self.db.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                rows = None            # malformed MATCH despite sanitizing -> LIKE
        if rows is None:
            rows = self._recall_like(query, project, limit)

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
              now_epoch: Optional[float] = None,
              session_id: Optional[str] = None) -> list:
        now_epoch = time.time() if now_epoch is None else now_epoch
        ids = list(ids)
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        rows = self.db.execute(f"SELECT * FROM cells WHERE id IN ({qs})", ids).fetchall()
        cells = [self._cell(r) for r in rows]
        self.refresh(ids, now_turn, now_epoch, session_id=session_id)  # recall counts as use
        return cells

    # -- sweep: reconcile cached tier, flag archive candidates ---------------
    def sweep(self, now_turn: int, now_epoch: Optional[float] = None,
              session_id: Optional[str] = None) -> dict:
        now_epoch = time.time() if now_epoch is None else now_epoch
        counts = {t: 0 for t in TIERS}
        newly_archived = []
        for r in self.db.execute("SELECT * FROM cells").fetchall():
            c = self._cell(r)
            eff = c.eff_charge(now_turn, now_epoch, self.cfg, now_session=session_id)
            t = tier_for(eff, self.cfg)
            counts[t] += 1
            if t != r["tier"]:
                self.db.execute("UPDATE cells SET tier=? WHERE id=?", (t, r["id"]))
                if t == "ARCHIVED":
                    newly_archived.append(c.id)
        self._commit()
        return {"counts": counts, "newly_archived": newly_archived}

    # -- maintenance: sweep + lossless prune + index health (detached only) --
    def bump_counter(self, key: str) -> int:
        """Atomic increment of a named meta counter; returns the new value."""
        self.db.execute(
            "INSERT INTO meta(k, v) VALUES(?, 1) "
            "ON CONFLICT(k) DO UPDATE SET v = v + 1", (key,))
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        self.db.commit()
        return row["v"] if row else 0

    def maintenance(self, now_turn: int, now_epoch: Optional[float] = None,
                    session_id: Optional[str] = None,
                    archive_after_days: float = 30.0,
                    sidecar_path: Optional[str] = None) -> dict:
        """Periodic upkeep. Run ONLY from a detached worker, never a prime hook.

        1. sweep() reconciles tiers.
        2. Long-idle ARCHIVED cells are exported to an append-only JSONL sidecar
           (fsync'd BEFORE the delete commits - the lossless-eviction promise),
           then deleted, with their cell_files rows; the cells_ad trigger keeps
           the FTS index consistent.
        3. Unreferenced topic rows are pruned (safe: rowid reuse only matters
           for rows still referenced by cells).
        4. FTS external-content integrity check (the thorough flag=1 form);
           divergence triggers a rebuild.
        """
        now_epoch = time.time() if now_epoch is None else now_epoch
        report = self.sweep(now_turn, now_epoch, session_id=session_id)

        cutoff = now_epoch - archive_after_days * 86400.0
        doomed = self.db.execute(
            "SELECT * FROM cells WHERE tier='ARCHIVED' AND pinned=0 "
            "AND last_refresh_epoch < ?", (cutoff,)).fetchall()
        exported = 0
        if doomed:
            if sidecar_path:
                with open(sidecar_path, "a", encoding="utf-8") as f:
                    for r in doomed:
                        f.write(json.dumps({k: r[k] for k in r.keys()},
                                           default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            qs = ",".join("?" * len(doomed))
            ids = [r["id"] for r in doomed]
            self.db.execute(f"DELETE FROM cell_files WHERE cell_id IN ({qs})", ids)
            self.db.execute(f"DELETE FROM cells WHERE id IN ({qs})", ids)
            exported = len(ids)
        self.db.execute(
            "DELETE FROM topic_rows WHERE topic_id NOT IN "
            "(SELECT DISTINCT topic_id FROM cells)")
        self.db.commit()

        if self.fts_enabled:
            try:
                self.db.execute(
                    "INSERT INTO cells_fts(cells_fts, rank) VALUES('integrity-check', 1)")
            except sqlite3.DatabaseError:
                try:
                    self.db.execute("INSERT INTO cells_fts(cells_fts) VALUES('rebuild')")
                    self.db.commit()
                except sqlite3.DatabaseError:
                    pass
        report["pruned"] = exported
        return report

    def vacuum(self) -> None:
        """VACUUM (must run outside any transaction; caller owns the cadence)."""
        self.db.commit()
        self.db.execute("VACUUM")

    def evict(self, cell_id: int, mempalace_ref: str) -> None:
        """Mark a cell evicted to durable storage (the Mempalace push is a hook concern)."""
        self.db.execute(
            "UPDATE cells SET tier='ARCHIVED', mempalace_ref=?, charge=0.0 WHERE id=?",
            (mempalace_ref, cell_id))
        self._commit()

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
            created_at_epoch=r["created_at_epoch"], last_refresh_epoch=r["last_refresh_epoch"],
            charge_updated_session=(r["charge_updated_session"]
                                    if "charge_updated_session" in r.keys() else ""))

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
