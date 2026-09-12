#!/usr/bin/env python3
"""Offline rehearsal of research.py's publication phase.

Extracts the post-verifier logic from workflows/research.py and runs it against
real accepted evidence packets in a disposable git clone. No CAO, no agents, no
network, and no writes to the real branch or the field root (main KB).

Reads the project spec (projects/<name>.yaml) for the source mapping, manifest
path and commit template, mirroring the engine. Usage:
  dryrun_publication.py <run_root> <field_repo> [--branch B] [--project P]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

FABRIC = pathlib.Path("/home/pavel/research-fabric")
PROJECTS_DIR = FABRIC / "projects"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.core import normalize_packet, source_mappings  # noqa: E402
from research_fabric.sources import bind_manifest, discover_sources, representation_for, source_bundle  # noqa: E402


def _load_project(name):
    path = PROJECTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise SystemExit(f"no project spec at {path}")
    return yaml.safe_load(path.read_text())


def _parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_root")
    ap.add_argument("field_repo")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--project", default="odyssey")
    return ap.parse_args()


def _source_context(run_root: pathlib.Path, project: dict):
    book_re = re.compile(project["snapshot_pattern"])
    source_files = []
    books = []
    for path in discover_sources(run_root / "sources"):
        match = book_re.search(path.name)
        if match:
            source_files.append(path)
            books.append(int(match.group(1)))
    assert source_files, "no supported source snapshots"
    return source_files, source_mappings(project, sorted(set(books)))


def _clone(field_repo: str, branch: str):
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="dryrun-pub-"))
    field_root = workdir / "kb"
    print(f"[dryrun] disposable clone: {field_root}")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", "--branch", branch, field_repo, str(field_root)],
        check=True,
    )
    subprocess.run(["git", "-C", str(field_root), "config", "user.email", "dryrun@local"], check=True)
    subprocess.run(["git", "-C", str(field_root), "config", "user.name", "dryrun"], check=True)
    return workdir, field_root


def _manifest(run_root: pathlib.Path, project: dict, source_files: list[pathlib.Path]):
    run_manifest = run_root / "source-manifest.jsonl"
    canonical = (
        pathlib.Path(__file__).resolve().parents[1] / "corpora" / project["corpus_dir"] / project["manifest_path"]
    )
    manifest_path = run_manifest if run_manifest.exists() else canonical
    assert manifest_path.exists(), f"no manifest at {manifest_path}"
    print(f"[dryrun] manifest: {manifest_path}")
    rows = [json.loads(line) for line in manifest_path.read_text().splitlines() if line.strip()]
    rows = bind_manifest(source_files, rows)
    print(f"[dryrun] manifest digests verified for {len(source_files)} sources")
    return rows


def _publish_sources(field_root: pathlib.Path, source_files: list[pathlib.Path]):
    snap_dest = field_root / "evidence" / "snapshots"
    snap_dest.mkdir(parents=True, exist_ok=True)
    for src in source_files:
        for original, relative in source_bundle(src):
            dest = snap_dest / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.chmod(0o644)
            shutil.copy2(original, dest)
            dest.chmod(0o444)
    print(f"[dryrun] snapshots copied: {len(source_files)}")
    return snap_dest


def _claim_row(sid: str, idx: int, claim: dict, source_id: str, note: str):
    return {
        "claim_id": f"c-{sid}-{idx}",
        "claim": claim.get("claim", ""),
        "note": note,
        "source_ids": [source_id],
        "locator": claim.get("locator", ""),
        "excerpt": claim.get("excerpt", ""),
        "stance": claim.get("stance", "supports"),
        "confidence": claim.get("confidence", 0.0),
        "independence_group": claim.get("independence_group", sid),
        "verified_at": "pilot-verifier-pass",
    }


def _materialize_claims(packet_dir, field_root, note_by_source, source_by_file):
    claims, dropped = [], []
    for packet_path in sorted(packet_dir.glob("worker-*.json")):
        sid = packet_path.stem.replace("worker-", "")
        packet = json.loads(packet_path.read_text())
        parsed = normalize_packet(packet.get("parsed") or {})
        for idx, claim in enumerate(parsed.get("claims", []), 1):
            source_file = pathlib.Path(claim.get("source_file", "")).name
            source_id = source_by_file.get(source_file)
            if not source_id:
                dropped.append({"worker": sid, "index": idx, "source_file": claim.get("source_file", "")})
                continue
            note = note_by_source[source_id]
            assert (field_root / note).is_file(), f"note target missing: {note}"
            claims.append(_claim_row(sid, idx, claim, source_id, note))
    if dropped:
        print(f"[dryrun] DROPPED {len(dropped)} claim(s): {json.dumps(dropped, indent=2)}")
        return None
    assert claims, "no claims materialized"
    (field_root / "evidence" / "claims.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in claims) + "\n"
    )
    print(f"[dryrun] claims materialized: {len(claims)}")
    print(f"[dryrun] distinct note targets: {sorted({c['note'] for c in claims})}")
    return claims


def _write_sources_manifest(field_root, source_files, manifest_rows):
    manifest_out = []
    present_names = {path.name for path in source_files}
    for row in manifest_rows:
        if pathlib.Path(row["snapshot"]).name not in present_names:
            continue
        row = dict(row)
        row["snapshot"] = "evidence/snapshots/" + pathlib.Path(row["snapshot"]).name
        manifest_out.append(json.dumps(row, ensure_ascii=False))
    (field_root / "evidence" / "sources.jsonl").write_text("\n".join(manifest_out) + "\n")


def _gate(field_root: pathlib.Path, script: str, label: str) -> bool:
    result = subprocess.run(
        [sys.executable, str(FABRIC / "bin" / script), str(field_root)], text=True, capture_output=True
    )
    print(f"[dryrun] {label} rc={result.returncode} out={result.stdout.strip()} err={result.stderr.strip()}")
    return result.returncode == 0


def _report_diff(field_root: pathlib.Path) -> bool:
    subprocess.run(["git", "-C", str(field_root), "add", "-N", "--", "evidence"], check=True)
    diff = subprocess.run(
        ["git", "-C", str(field_root), "diff", "--no-ext-diff", "--", "evidence"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if not diff.strip():
        print("[dryrun] FAIL: empty diff")
        return False
    files_in_diff = re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)
    print(f"[dryrun] diff bytes={len(diff)} files={len(files_in_diff)}")
    for filename in files_in_diff:
        print(f"[dryrun]   {filename}")
    return True


def _grounding_misses(snap_dest, source_by_file, claims):
    sys.path.insert(0, str(FABRIC / "bin"))
    import excerpt_grounding

    snaps = {}
    for name in source_by_file:
        path = snap_dest / name
        if path.is_file():
            snaps[name] = representation_for(path).text
    file_by_source = {source_id: name for name, source_id in source_by_file.items()}
    misses = []
    for claim in claims:
        snap_name = file_by_source[claim["source_ids"][0]]
        if not excerpt_grounding.grounded(claim["excerpt"], snaps[snap_name]):
            misses.append((claim["claim_id"], re.sub(r"\s+", " ", claim["excerpt"])[:80]))
    print(f"[dryrun] excerpt grounding: {len(claims) - len(misses)}/{len(claims)} grounded")
    for cid, frag in misses[:10]:
        print(f"[dryrun]   MISS {cid}: {frag}...")
    return misses


def _commit(field_root, project_name: str, claim_count: int) -> str:
    subprocess.run(["git", "-C", str(field_root), "add", "--", "evidence"], check=True)
    subprocess.run(["git", "-C", str(field_root), "diff", "--cached", "--check"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(field_root),
            "commit",
            "-q",
            "-m",
            f"Research: {project_name} evidence run ({claim_count} claims)",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(field_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(field_root), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout
    print(f"[dryrun] commit={head[:12]} clean={not status.strip()}")
    if status.strip():
        print(f"[dryrun] residual dirty state:\n{status}")
    return status


def main() -> int:
    args = _parse_args()
    run_root = pathlib.Path(args.run_root).resolve()
    project = _load_project(args.project)
    source_files, mappings = _source_context(run_root, project)
    note_by_source, source_by_file = mappings
    workdir, field_root = _clone(args.field_repo, args.branch)
    manifest_rows = _manifest(run_root, project, source_files)
    snap_dest = _publish_sources(field_root, source_files)
    claims = _materialize_claims(run_root / "evidence", field_root, note_by_source, source_by_file)
    if claims is None:
        return 1
    _write_sources_manifest(field_root, source_files, manifest_rows)
    if not _gate(field_root, "provenance_validate.py", "provenance"):
        return 1
    if not _gate(field_root, "excerpt_grounding.py", "grounding"):
        return 1
    if not _report_diff(field_root):
        return 1
    misses = _grounding_misses(snap_dest, source_by_file, claims)
    status = _commit(field_root, args.project, len(claims))
    shutil.rmtree(workdir)
    failed = bool(misses) or bool(status.strip())
    print(f"[dryrun] RESULT: {'ISSUES FOUND' if failed else 'OK'}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
