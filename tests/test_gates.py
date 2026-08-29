"""Integration tests for the deterministic gates (subprocess execution).

Builds a disposable field root and runs the real gate scripts
(bin/provenance_validate.py and bin/excerpt_grounding.py) against it. This
proves the gates' self-tests and a valid synthetic ledger pass, and that a
corrupted ledger fails closed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
PY = sys.executable

MUST_ACCEPT_SNIPPET = "the glaucous-eyed goddess Athena"


def _sub(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def test_excerpt_grounding_self_test():
    r = _sub([PY, str(BIN / "excerpt_grounding.py")])
    assert r.returncode == 0, r.stderr
    assert '"self_test": "PASS"' in r.stdout


def test_provenance_valid_ledger():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "kb"
        ev = root / "evidence" / "snapshots"
        ev.mkdir(parents=True)
        snap = ev / "snap.txt"
        snap.write_text(MUST_ACCEPT_SNIPPET, encoding="utf-8")
        digest = hashlib.sha256(MUST_ACCEPT_SNIPPET.encode()).hexdigest()
        (root / "evidence" / "sources.jsonl").write_text(
            json.dumps(
                {
                    "source_id": "s-test",
                    "url": "u",
                    "title": "t",
                    "retrieved_at": "2026-01-01",
                    "content_type": "text",
                    "sha256": digest,
                    "snapshot": "evidence/snapshots/snap.txt",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "evidence" / "claims.jsonl").write_text(
            json.dumps(
                {
                    "claim_id": "c-test-1",
                    "claim": "some claim",
                    "note": "wiki/summaries/x.md",
                    "source_ids": ["s-test"],
                    "locator": "1",
                    "excerpt": MUST_ACCEPT_SNIPPET,
                    "stance": "supports",
                    "confidence": 0.9,
                    "independence_group": "g",
                    "verified_at": "test",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # prove the claim's excerpt is grounded too
        r = _sub([PY, str(BIN / "excerpt_grounding.py"), str(root)])
        assert r.returncode == 0, r.stderr

        r = _sub([PY, str(BIN / "provenance_validate.py"), str(root)])
        assert r.returncode == 0, r.stderr
        assert '"valid": true' in r.stdout


def test_provenance_rejects_tampered_snapshot():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "kb"
        ev = root / "evidence" / "snapshots"
        ev.mkdir(parents=True)
        snap = ev / "snap.txt"
        snap.write_text(MUST_ACCEPT_SNIPPET, encoding="utf-8")
        # manifest records the ORIGINAL hash, then the file is tampered
        digest = hashlib.sha256(MUST_ACCEPT_SNIPPET.encode()).hexdigest()
        (root / "evidence" / "sources.jsonl").write_text(
            json.dumps(
                {
                    "source_id": "s-test",
                    "url": "u",
                    "title": "t",
                    "retrieved_at": "2026-01-01",
                    "content_type": "text",
                    "sha256": digest,
                    "snapshot": "evidence/snapshots/snap.txt",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "evidence" / "claims.jsonl").write_text(
            json.dumps(
                {
                    "claim_id": "c-test-1",
                    "claim": "some claim",
                    "note": "wiki/summaries/x.md",
                    "source_ids": ["s-test"],
                    "locator": "1",
                    "excerpt": MUST_ACCEPT_SNIPPET,
                    "stance": "supports",
                    "confidence": 0.9,
                    "independence_group": "g",
                    "verified_at": "test",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        snap.write_text(MUST_ACCEPT_SNIPPET + " TAMPERED", encoding="utf-8")
        r = _sub([PY, str(BIN / "provenance_validate.py"), str(root)])
        assert r.returncode != 0
        assert "sha256 mismatch" in r.stderr


# ------------------------------------------- translation-grounding gate (Aeneid)


def _translation_field_root(tmp):
    """Build a field root with a translation witness + aligned claim."""
    root = pathlib.Path(tmp) / "kb"
    ev = root / "evidence" / "snapshots"
    ev.mkdir(parents=True)
    latin = "arma virumque cano troiae qui primus ab oris"
    english = "I sing of arms and the man who first from the shores of Troy"
    (ev / "latin.html").write_text(latin, encoding="utf-8")
    (ev / "kline.html").write_text(english, encoding="utf-8")
    (root / "evidence" / "sources.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "source_id": "s-aeneid-1-latin",
                        "role": "canonical",
                        "translator": None,
                        "sha256": hashlib.sha256(latin.encode()).hexdigest(),
                        "snapshot": "evidence/snapshots/latin.html",
                    }
                ),
                json.dumps(
                    {
                        "source_id": "s-aeneid-1-kline",
                        "role": "translation",
                        "translator": "Kline",
                        "sha256": hashlib.sha256(english.encode()).hexdigest(),
                        "snapshot": "evidence/snapshots/kline.html",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root, ev


def _aligned_claim(
    *, english="I sing of arms and the man", translator="Kline", wsid="s-aeneid-1-kline", wloc="1.1-7", lloc="Aen.1.1-7"
):
    return {
        "claim_id": "c-1-1",
        "claim": "some claim",
        "note": "wiki/summaries/aeneid-book-1.md",
        "source_ids": ["s-aeneid-1-latin"],
        "locator": lloc,
        "excerpt": "arma virumque cano",
        "stance": "supports",
        "confidence": 0.9,
        "independence_group": "g",
        "english_witness": {"translator": translator, "source_id": wsid, "locator": wloc, "excerpt": english},
        "witnesses_consulted": ["Conington", "Mackail", "Kline"],
    }


def _write_alignment(tmp, rows):
    p = pathlib.Path(tmp) / "alignment.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_translation_grounding_valid():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root, ev = _translation_field_root(td)
        al = _write_alignment(
            td,
            [
                {
                    "book": 1,
                    "canonical_range": "Aen.1.1-Aen.1.756",
                    "canonical_source_id": "s-aeneid-1-latin",
                    "witnesses": {"kline": {"source_id": "s-aeneid-1-kline", "aligned_to": "Aen.1.1-Aen.1.756"}},
                }
            ],
        )
        (root / "evidence" / "claims.jsonl").write_text(json.dumps(_aligned_claim()) + "\n", encoding="utf-8")
        r = _sub([PY, str(BIN / "translation_grounding.py"), str(root), str(al)])
        assert r.returncode == 0, r.stderr
        assert '"valid": true' in r.stdout


def test_translation_grounding_rejects_paraphrased_english():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root, ev = _translation_field_root(td)
        al = _write_alignment(
            td,
            [
                {
                    "book": 1,
                    "canonical_range": "Aen.1.1-Aen.1.756",
                    "canonical_source_id": "s-aeneid-1-latin",
                    "witnesses": {"kline": {"source_id": "s-aeneid-1-kline", "aligned_to": "Aen.1.1-Aen.1.756"}},
                }
            ],
        )
        (root / "evidence" / "claims.jsonl").write_text(
            json.dumps(_aligned_claim(english="A made up sentence not in the witness")) + "\n", encoding="utf-8"
        )
        r = _sub([PY, str(BIN / "translation_grounding.py"), str(root), str(al)])
        assert r.returncode != 0
        assert "excerpt not verbatim" in r.stderr


def test_translation_grounding_rejects_bad_translator_source():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root, ev = _translation_field_root(td)
        al = _write_alignment(
            td,
            [
                {
                    "book": 1,
                    "canonical_range": "Aen.1.1-Aen.1.756",
                    "canonical_source_id": "s-aeneid-1-latin",
                    "witnesses": {"kline": {"source_id": "s-aeneid-1-kline", "aligned_to": "Aen.1.1-Aen.1.756"}},
                }
            ],
        )
        (root / "evidence" / "claims.jsonl").write_text(
            json.dumps(_aligned_claim(wsid="s-aeneid-1-latin", english="arma virumque cano")) + "\n", encoding="utf-8"
        )
        r = _sub([PY, str(BIN / "translation_grounding.py"), str(root), str(al)])
        assert r.returncode != 0
        assert "not a translation witness" in r.stderr
