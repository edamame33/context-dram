"""
distiller - turn a raw turn-block into a memory cell (the input to Memory.write).

Two modes:
  * heuristic (default, free, deterministic): regex + keyword extraction - file
    paths, hex offsets, a type guess, a title, candidate facts/concepts. This is
    the zero-cost ledger; it needs nothing and runs inline.
  * model-backed (pluggable): pass model_fn(text)->dict to get an LLM-distilled
    cell. Deferred decision - a fast local model fire-and-forget, or batch via the
    nightly task. The heuristic mode ships today; the model slot is just an upgrade.

stdlib only. Target: py -3.13.
"""
from __future__ import annotations

import re
from collections import Counter

CODE_EXT = ("py", "rs", "c", "cc", "cpp", "h", "hpp", "cs", "js", "ts", "tsx",
            "jsx", "json", "dylib", "so", "dll", "exe", "md", "toml", "yaml",
            "yml", "sh", "ps1", "bat", "sql", "txt", "cfg", "ini", "html", "css",
            "go", "java", "rb", "lua")

_WIN_PATH = re.compile(r"[A-Za-z]:\\(?:[^\s\"'<>|]+)")
_FILE = re.compile(r"\b[\w.\-/\\]+\.(?:%s)\b" % "|".join(CODE_EXT))
_OFFSET = re.compile(r"\b0x[0-9A-Fa-f]+\b")
_IDENT = re.compile(r"\b(?:[A-Z][a-z]+){2,}\b|\b\w+_\w+\b")     # CamelCase / snake_case
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")

_STOP = {"this", "that", "with", "from", "have", "will", "your", "when", "then",
         "than", "they", "them", "what", "which", "were", "been", "into", "also",
         "just", "like", "here", "there", "does", "done", "make", "made", "need",
         "want", "know", "good", "would", "could", "should", "about", "because",
         "their", "other", "some", "most", "more", "much", "very", "only", "even",
         "still", "being", "both", "rather", "really", "going"}

TYPE_RULES = [
    ("decision",  ("decided", "we'll go", "go with", "chose", "choosing", "let's use", "recommend")),
    ("bugfix",    ("fixed", "the bug", "root cause", "regression", "broken", "crash", "race ")),
    ("discovery", ("found", "turns out", "it's actually", "discovered", "confirmed", "the reason")),
    ("refactor",  ("refactor", "renamed", "extracted", "moved ", "cleanup", "consolidat")),
    ("feature",   ("added", "implemented", "built", "wrote", "shipped", "new ")),
    ("reference", ("https://", "http://", "see the docs", "per the", "reference")),
]


def classify_type(text: str) -> str:
    low = text.lower()
    for t, kws in TYPE_RULES:
        if any(k in low for k in kws):
            return t
    return "fact"


def title_of(text: str, limit: int = 90) -> str:
    for raw in text.splitlines():
        s = raw.strip().lstrip("#*->•- ").strip()
        if len(s) >= 8 and not s.startswith("```"):
            return s[:limit]
    return text.strip()[:limit] or "untitled"


def extract_files(text: str) -> list:
    files = set()
    for m in _WIN_PATH.findall(text):
        files.add(m.rstrip(".,);:'\""))
    for m in _FILE.findall(text):
        files.add(m.rstrip(".,);:'\""))
    return sorted(files)


def extract_offsets(text: str) -> list:
    return sorted(set(_OFFSET.findall(text)))


def extract_concepts(text: str, top: int = 6) -> list:
    idents = [m.group(0) for m in _IDENT.finditer(text)]
    words = [w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP]
    common = [w for w, _ in Counter(words).most_common(top)]
    out = []
    for c in idents + common:               # specific identifiers first, then frequent words
        if c not in out:
            out.append(c)
    return out[:top]


def extract_facts(text: str, cap: int = 6) -> list:
    facts = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if _OFFSET.search(s) or "->" in s or "→" in s or re.search(r"\bv?\d+\.\d+", s):
            facts.append(s[:200])
        elif s[:2] in ("- ", "* ") and len(s) < 160:
            facts.append(s[2:])
        if len(facts) >= cap:
            break
    return facts


def distill(text: str, *, model_fn=None) -> dict:
    """Return a dict of cell fields ready to splat into Memory.write(**d).

    model_fn(text)->dict overrides the heuristics (the LLM-backed upgrade);
    we still backfill type/title if the model omits them.
    """
    if model_fn is not None:
        d = model_fn(text)
        d.setdefault("type", classify_type(text))
        d.setdefault("title", title_of(text))
        d.setdefault("discovery_tokens", max(1, len(text) // 4))
        return d

    offsets = extract_offsets(text)
    concepts = extract_concepts(text)
    if offsets:                              # offsets are high-value recall keys - lead with them
        concepts = list(dict.fromkeys(offsets[:3] + concepts))[:6]
    return {
        "type": classify_type(text),
        "title": title_of(text),
        "facts": extract_facts(text),
        "files": extract_files(text),
        "concepts": concepts,
        "discovery_tokens": max(1, len(text) // 4),
    }
