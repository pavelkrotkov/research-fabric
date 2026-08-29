"""Tests for the deterministic core logic (no CAO, no network, no host)."""

from __future__ import annotations

import pathlib

import pytest

from research_fabric.core import (
    REQUIRED_PROJECT_FIELDS,
    book_task_from_project,
    extract_json,
    load_project,
    multisource_packet_defects,
    normalize_packet,
    packet_defects,
    source_mappings,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECTS_DIR = ROOT / "projects"

# A valid single-claim packet (below the 5-claim min, used for structure tests).
_ONE_SOLID = {
    "claims": [
        {
            "claim": "Telemachus awaits his father's return and the suitors' ruin.",
            "source_file": "odyssey-book-1.html",
            "locator": "Book 1 [1]-[9]",
            "excerpt": "in his heart he bethought him of his return",
            "stance": "supports",
            "confidence": 0.9,
            "uncertainty": "low",
        }
    ],
    "conflicts": [],
    "coverage_notes": [],
}

VALID_ACCEPTANCE = {"min_claims_per_book": 5, "max_claims_per_book": 30}


def _n_claims(n):
    p = {"claims": [], "conflicts": [], "coverage_notes": []}
    for i in range(n):
        c = dict(_ONE_SOLID["claims"][0])
        c["claim"] = f"claim {i}"
        p["claims"].append(c)
    return p


# ---------------------------------------------------------------- project spec


def test_load_valid_project():
    proj = load_project(PROJECTS_DIR, "odyssey")
    for f in REQUIRED_PROJECT_FIELDS:
        assert f in proj


def test_load_missing_required_field_fails():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "bad.yaml"
        p.write_text("corpus_dir: x\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="missing required fields"):
            load_project(pathlib.Path(td), "bad")


def test_load_nonexistent_project_fails():
    with pytest.raises(RuntimeError, match="no project spec"):
        load_project(PROJECTS_DIR, "does-not-exist")


# ---------------------------------------------------------------- packet_defects


def test_empty_claims_rejected():
    d = packet_defects({"claims": []}, VALID_ACCEPTANCE)
    assert any("zero claims" in x for x in d)


def test_non_object_rejected():
    assert packet_defects(["not", "a", "dict"], VALID_ACCEPTANCE)


def test_placeholder_echo_rejected():
    p = {
        "claims": [
            {"claim": "...", "source_file": "...", "locator": "...", "excerpt": "...", "stance": "supports"},
            {"claim": "real", "source_file": "s.html", "locator": "1", "excerpt": "e", "stance": "supports"},
            {"claim": "real2", "source_file": "s.html", "locator": "1", "excerpt": "e", "stance": "supports"},
            {"claim": "real3", "source_file": "s.html", "locator": "1", "excerpt": "e", "stance": "supports"},
            {"claim": "real4", "source_file": "s.html", "locator": "1", "excerpt": "e", "stance": "supports"},
        ]
    }
    d = packet_defects(p, VALID_ACCEPTANCE)
    assert any("placeholder schema echo" in x for x in d)


def test_undersized_rejected_oversized_rejected():
    assert any("below min_claims_per_book" in x for x in packet_defects(_n_claims(4), VALID_ACCEPTANCE))
    too_big = _n_claims(31)
    d = packet_defects(too_big, VALID_ACCEPTANCE)
    assert any("above max_claims_per_book" in x for x in d)


def test_valid_pass():
    assert packet_defects(_n_claims(12), VALID_ACCEPTANCE) == []


def test_missing_field_rejected():
    p = _n_claims(12)
    del p["claims"][0]["locator"]
    assert any("missing locator" in x for x in packet_defects(p, VALID_ACCEPTANCE))


def test_nonconcrete_stance_rejected():
    p = _n_claims(12)
    p["claims"][0]["stance"] = "probably-true"
    assert any("non-concrete stance" in x for x in packet_defects(p, VALID_ACCEPTANCE))


# ---------------------------------------------------------------- normalize_packet


def test_normalize_collapses_wrapping():
    p = {"claims": [dict(_ONE_SOLID["claims"][0], excerpt="in his  heart\n he  bethought")], "conflicts": []}
    normalize_packet(p)
    assert p["claims"][0]["excerpt"] == "in his heart he bethought"
    p2 = {"claims": [dict(_ONE_SOLID["claims"][0], excerpt="x")], "conflicts": []}
    normalize_packet(p2)
    assert p2["claims"][0]["excerpt"] == "x"


def test_normalize_strips_source_file_whitespace():
    p = {"claims": [dict(_ONE_SOLID["claims"][0], source_file="odyssey-book-1 .html")], "conflicts": []}
    normalize_packet(p)
    assert p["claims"][0]["source_file"] == "odyssey-book-1.html"


# ---------------------------------------------------------------- extract_json


def test_extract_plain_json():
    assert extract_json('{"claims": []}') == {"claims": []}


def test_extract_fenced_json():
    assert extract_json('```json\n{"claims": [1]}\n```') == {"claims": [1]}


def test_extract_prose_around():
    raw = 'Here is the packet.\n{"claims": [{"x": 1}]}\n\nTHAT IS ALL.'
    assert extract_json(raw) == {"claims": [{"x": 1}]}


def test_extract_garbage_returns_none():
    assert extract_json("not json at all") is None


# ---------------------------------------------------------------- templating


def test_book_task_from_project():
    proj = load_project(PROJECTS_DIR, "odyssey")
    t = book_task_from_project(1, proj, "odyssey")
    assert "Book 1" in t and "council of the gods" in t


def test_book_task_default_theme_for_absent_book():
    proj = load_project(PROJECTS_DIR, "odyssey")
    # books 12-14 intentionally have no explicit theme -> default
    t = book_task_from_project(12, proj, "odyssey")
    assert "moral and theological significance" in t


def test_source_mappings():
    proj = load_project(PROJECTS_DIR, "odyssey")
    nb, sb = source_mappings(proj, [1, 16])
    assert nb["s-odyssey-16-theoi"] == "wiki/summaries/odyssey-book-16.md"
    assert sb["odyssey-book-16.html"] == "s-odyssey-16-theoi"


# ------------------------------------------------ multi-witness (Aeneid) schema


def _aeneid_claim():
    return {
        "claim": "Juno nurses her wrath against the Trojans.",
        "source_file": "aeneid-book-1-latin.html",
        "locator": "Aen.1.1-7",
        "excerpt": "Arma virumque cano",
        "stance": "supports",
        "confidence": 0.9,
        "claim_type": "thematic",
        "english_witness": {
            "translator": "Kline",
            "source_id": "s-aeneid-1-kline",
            "locator": "1.1-7",
            "excerpt": "I sing of arms and the man",
        },
        "witnesses_consulted": ["Conington", "Mackail", "Kline"],
    }


def test_aeneid_schema_valid():
    proj = load_project(PROJECTS_DIR, "aeneid")
    p = {"claims": [_aeneid_claim() for _ in range(6)], "conflicts": [], "coverage_notes": []}
    assert multisource_packet_defects(p, proj, VALID_ACCEPTANCE) == []


def test_aeneid_bad_claim_type():
    proj = load_project(PROJECTS_DIR, "aeneid")
    p = {"claims": [_aeneid_claim() for _ in range(6)], "conflicts": [], "coverage_notes": []}
    p["claims"][0]["claim_type"] = "nonsense"
    assert any("claim_type" in d for d in multisource_packet_defects(p, proj, VALID_ACCEPTANCE))


def test_aeneid_missing_english_witness():
    proj = load_project(PROJECTS_DIR, "aeneid")
    cs = [_aeneid_claim() for _ in range(6)]
    del cs[0]["english_witness"]
    p = {"claims": cs, "conflicts": [], "coverage_notes": []}
    assert any("english_witness" in d for d in multisource_packet_defects(p, proj, VALID_ACCEPTANCE))


def test_aeneid_unknown_translator_rejected():
    proj = load_project(PROJECTS_DIR, "aeneid")
    cs = [_aeneid_claim() for _ in range(6)]
    cs[0]["english_witness"]["translator"] = "NotAPerson"
    p = {"claims": cs, "conflicts": [], "coverage_notes": []}
    assert any("translator" in d for d in multisource_packet_defects(p, proj, VALID_ACCEPTANCE))


def test_aeneid_witness_cited_as_authoritative_rejected():
    """A translation witness cited in source_file must never masquerade as evidence."""
    proj = load_project(PROJECTS_DIR, "aeneid")
    cs = [_aeneid_claim() for _ in range(6)]
    cs[0]["source_file"] = "aeneid-book-1-kline.html"
    p = {"claims": cs, "conflicts": [], "coverage_notes": []}
    assert any("translation witness" in d for d in multisource_packet_defects(p, proj, VALID_ACCEPTANCE))
