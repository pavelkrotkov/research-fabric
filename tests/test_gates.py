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
