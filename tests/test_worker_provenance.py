"""Worker-boundary regressions for source representation provenance."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

from research_fabric.sources import representation_for, source_attestation

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIRECT = ROOT / "bin" / "direct_worker.py"
AENEID = ROOT / "bin" / "aeneid_worker.py"


def _fake_openai(tmp_path: pathlib.Path) -> dict[str, str]:
    (tmp_path / "openai.py").write_text(
        textwrap.dedent(
            """
            import json
            import re
            from types import SimpleNamespace


            class _Completions:
                def create(self, *, messages, **_kwargs):
                    prompt = messages[-1]["content"]
                    if "=== NUMBERED LATIN PASSAGES ===" in prompt:
                        match = re.search(r"\\[([^]]+)\\] <([^>]+)>", prompt)
                        return _response({
                            "selections": [{
                                "i": 1,
                                "translator": match.group(1),
                                "source_id": match.group(2),
                                "excerpt": "translation",
                            }]
                        })
                    if "=== ITEMS ===" in prompt:
                        return _response({
                            "selections": [{"i": 1, "translator": "conington", "excerpt": "translation"}]
                        })
                    if "=== LATIN (canonical) ===" in prompt:
                        if "Arma virumque cano" not in prompt:
                            raise RuntimeError("canonical representation was not supplied")
                        return _response({
                            "claims": [{
                                "claim": "A Latin claim",
                                "claim_type": "textual",
                                "locator": "Aen.1.1",
                                "excerpt": "Arma virumque cano",
                                "stance": "supports",
                                "confidence": 0.9,
                            }]
                        })
                    if "Hello world" not in prompt:
                        raise RuntimeError("worker representation was not supplied")
                    return _response({
                        "claims": [{
                            "claim": "An HTML claim",
                            "source_file": "book-1.html",
                            "locator": "Book 1",
                            "excerpt": "Hello world",
                            "stance": "supports",
                            "confidence": 0.9,
                        }]
                    })


            def _response(value):
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])


            class OpenAI:
                def __init__(self, **_kwargs):
                    self.chat = SimpleNamespace(completions=_Completions())
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(ROOT / "src")))
    env["OPENROUTER_API_KEY"] = "test-key"
    env.pop("NVIDIA_API_KEY", None)
    return env


def test_direct_worker_stamps_consumed_representation(tmp_path):
    source_dir = tmp_path / "direct-sources"
    run_root = tmp_path / "direct-run"
    source_dir.mkdir()
    source = source_dir / "book-1.html"
    source.write_text("<p>Hello <b>world</b></p>", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(DIRECT), str(run_root), str(source_dir), "1", source.name],
        env=_fake_openai(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    packet = json.loads((run_root / "evidence" / "worker-book-1.json").read_text(encoding="utf-8"))
    assert packet["source_provenance"] == [source_attestation(representation_for(source))]


def test_multi_witness_worker_stamps_all_consumed_representations(tmp_path):
    source_dir = tmp_path / "aeneid-sources"
    run_root = tmp_path / "aeneid-run"
    source_dir.mkdir()
    files = {
        "latin": source_dir / "aeneid-book-1-latin.html",
        "conington": source_dir / "aeneid-book-1-conington.html",
        "mackail": source_dir / "aeneid-book-1-mackail.html",
        "kline": source_dir / "aeneid-book-1-kline.html",
    }
    files["latin"].write_text("<p>Arma virumque cano</p>", encoding="utf-8")
    for variant in ("conington", "mackail", "kline"):
        files[variant].write_text("<p>translation</p>", encoding="utf-8")
    args = [
        sys.executable,
        str(AENEID),
        str(run_root),
        str(source_dir),
        "1",
        files["latin"].name,
        "",
    ]
    for variant in ("conington", "mackail", "kline"):
        args += ["--witness", f"{variant}:s-aeneid-1-{variant}:{files[variant].name}"]
    result = subprocess.run(args, env=_fake_openai(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    packet = json.loads((run_root / "evidence" / "worker-book-1.json").read_text(encoding="utf-8"))
    expected = sorted(
        [source_attestation(representation_for(path)) for path in files.values()],
        key=lambda row: row["source_file"],
    )
    assert packet["source_provenance"] == expected
