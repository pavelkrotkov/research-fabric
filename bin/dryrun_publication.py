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
from research_fabric.claims import accept_packet, atomic_write_json  # noqa: E402
from research_fabric.compilation import CompilationError  # noqa: E402
from research_fabric.core import normalize_packet, source_mappings  # noqa: E402
from research_fabric.publication import ledger_rows  # noqa: E402
from research_fabric.reading import ReadingPlan  # noqa: E402
from research_fabric.review import audit_compiled_wiki  # noqa: E402
from research_fabric.run_state import _publication_outputs, _verify_reviewed_outputs  # noqa: E402
from research_fabric.sources import (  # noqa: E402
    bind_manifest,
    copy_source_snapshot,
    discover_sources,
    preserve_original_bytes,
    snapshot_relative,
)


def _load_project(name):
    path = PROJECTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise SystemExit(f"no project spec at {path}")
    return yaml.safe_load(path.read_text())


def accept_and_persist_packet(packet_path, worker, source_dir, destination, reading_plan=None, acceptance=None):
    """Migrate one packet into the disposable publication destination."""
    packet_path = pathlib.Path(packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if not packet.get("packet_revision") and reading_plan is None:
        normalize_packet(packet.get("parsed") or packet)
    if reading_plan:
        reading_plan.validate_packet(packet, worker, acceptance or {})
    accepted = accept_packet(packet, worker, source_dir=source_dir)
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / packet_path.name
    atomic_write_json(target, accepted)
    return accepted, target


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
    rows = bind_manifest(source_files, rows, source_root=run_root / "sources" if "reading" in project else None)
    print(f"[dryrun] manifest digests verified for {len(source_files)} sources")
    return rows


def _publish_sources(field_root: pathlib.Path, source_files: list[pathlib.Path], source_root=None):
    preserve_original_bytes(field_root)
    snap_dest = field_root / "evidence" / "snapshots"
    snap_dest.mkdir(parents=True, exist_ok=True)
    for src in source_files:
        copy_source_snapshot(src, snap_dest, source_root=source_root)
    print(f"[dryrun] snapshots copied: {len(source_files)}")
    return snap_dest


def _materialize_claims(
    packet_dir, field_root, note_by_source, source_by_file, source_dir, reading_plan=None, acceptance=None, multi=True
):
    packets, history = {}, []
    accepted_packets = field_root / "evidence" / "accepted-packets"
    for packet_path in sorted(packet_dir.glob("worker-*.json")):
        sid = packet_path.stem.removeprefix("worker-")
        packet, _ = accept_and_persist_packet(packet_path, sid, source_dir, accepted_packets, reading_plan, acceptance)
        packets[sid] = packet
        history.extend(packet.get("claim_history") or [])
    claims, dropped = ledger_rows(
        field_root, packets, note_by_source, source_by_file, multi=multi, reading_plan=reading_plan
    )
    if dropped:
        print(f"[dryrun] DROPPED {len(dropped)} claim(s): {json.dumps(dropped, indent=2)}")
        return None
    assert claims, "no claims materialized"
    if history:
        atomic_write_json(field_root / "evidence" / "claim-history.json", history)
    return claims


def _write_claims(field_root: pathlib.Path, claims: list[dict]):
    (field_root / "evidence" / "claims.jsonl").write_text(
        "\n".join(json.dumps(claim, ensure_ascii=False) for claim in claims) + "\n"
    )
    notes_used = sorted({claim["note"] for claim in claims})
    print(f"[dryrun] claims materialized: {len(claims)}")
    print(f"[dryrun] distinct note targets: {notes_used}")


def _write_sources_manifest(field_root, source_files, manifest_rows, source_root=None):
    manifest_out = []
    paths = {path.relative_to(source_root).as_posix() if source_root else path.name: path for path in source_files}
    for row in manifest_rows:
        name = row["source_file"] if source_root else pathlib.Path(row["snapshot"]).name
        if name not in paths:
            continue
        row = dict(row)
        row["snapshot"] = "evidence/snapshots/" + snapshot_relative(paths[name], source_root).as_posix()
        manifest_out.append(json.dumps(row, ensure_ascii=False))
    (field_root / "evidence" / "sources.jsonl").write_text("\n".join(manifest_out) + "\n")


def _gate(field_root: pathlib.Path, script: str, label: str) -> bool:
    result = subprocess.run(
        [sys.executable, str(FABRIC / "bin" / script), str(field_root)], text=True, capture_output=True
    )
    print(f"[dryrun] {label} rc={result.returncode} out={result.stdout.strip()} err={result.stderr.strip()}")
    return result.returncode == 0


def _report_diff(field_root: pathlib.Path, verification: pathlib.Path):
    try:
        report = audit_compiled_wiki(field_root, verification)
    except CompilationError as exc:
        print(f"[dryrun] FAIL: {exc}")
        return None
    print(f"[dryrun] compiled-wiki review bytes={report['diff_bytes']} files={len(report['changed'])}")
    for filename in report["changed"]:
        print(f"[dryrun]   {filename}")
    return report


def _grounding_misses(snap_dest, source_by_file, claims):
    sys.path.insert(0, str(FABRIC / "bin"))
    import excerpt_grounding

    rows = [
        json.loads(line) for line in filter(str.strip, (snap_dest.parent / "sources.jsonl").read_text().splitlines())
    ]
    snapshots = {row["source_id"]: snap_dest.parent.parent / row["snapshot"] for row in rows}
    snaps = {source_id: excerpt_grounding.source_text(path) for source_id, path in snapshots.items()}
    misses = []
    for claim in claims:
        if not excerpt_grounding.grounded(claim["excerpt"], snaps[claim["source_ids"][0]]):
            misses.append((claim["claim_id"], re.sub(r"\s+", " ", claim["excerpt"])[:80]))
    print(f"[dryrun] excerpt grounding: {len(claims) - len(misses)}/{len(claims)} grounded")
    for cid, frag in misses[:10]:
        print(f"[dryrun]   MISS {cid}: {frag}...")
    return misses


def _commit(field_root, project_name: str, claim_count: int, reviewed) -> str:
    _verify_reviewed_outputs(field_root, _publication_outputs(field_root), reviewed)
    subprocess.run(["git", "-C", str(field_root), "add", "--", "evidence", ".gitattributes"], check=True)
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


def _published_notes(field_root, source_files, note_by_source, source_by_file, source_root=None):
    """Derive native note identity from the published source bundle."""
    for source in source_files:
        key = snapshot_relative(source, source_root).parent.name
        bundle_path = field_root / "wiki" / "assets" / key / "bundle.json"
        if bundle_path.is_file():
            manifest = json.loads(bundle_path.read_text())
            doc_name = pathlib.Path(manifest["input_path"]).stem
            if doc_name != key or manifest["key"] != key:
                raise ValueError("published native source identity drift")
            note_by_source[
                source_by_file[source.relative_to(source_root).as_posix() if source_root else source.name]
            ] = f"wiki/summaries/{doc_name}.md"


def _run_sources(run_root, project):
    if "reading" not in project:
        source_files, mappings = _source_context(run_root, project)
        return source_files, *mappings, None, None
    from research_fabric._source_assets import safe_path

    source_root = run_root / "sources"
    paths = {name: safe_path(source_root, name) for name in project["reading"]["sources"]}
    plan = ReadingPlan.load(run_root / "reading-plan.json", paths, project["reading"])
    sources = {name: row["source_id"] for name, row in plan.data["sources"].items()}
    return list(paths.values()), {}, sources, plan, source_root


def _write_reading_plan(field_root, reading_plan):
    _write_reading_plan(field_root, reading_plan)


def main() -> int:
    args = _parse_args()
    run_root = pathlib.Path(args.run_root).resolve()
    project = _load_project(args.project)
    source_files, note_by_source, source_by_file, reading_plan, source_root = _run_sources(run_root, project)
    workdir, field_root = _clone(args.field_repo, args.branch)
    manifest_rows = _manifest(run_root, project, source_files)
    snap_dest = _publish_sources(field_root, source_files, source_root)
    _published_notes(field_root, source_files, note_by_source, source_by_file, source_root)
    claims = _materialize_claims(
        run_root / "evidence",
        field_root,
        note_by_source,
        source_by_file,
        run_root / "sources",
        reading_plan,
        project.get("acceptance") or {},
    )
    if claims is None:
        return 1
    if reading_plan:
        atomic_write_json(field_root / "evidence/reading-plan.json", reading_plan.data)
    _write_claims(field_root, claims)
    _write_sources_manifest(field_root, source_files, manifest_rows, source_root)
    if not _gate(field_root, "provenance_validate.py", "provenance"):
        return 1
    if not _gate(field_root, "excerpt_grounding.py", "grounding"):
        return 1
    reviewed = _report_diff(field_root, workdir / "review")
    if reviewed is None:
        return 1
    misses = _grounding_misses(snap_dest, source_by_file, claims)
    status = _commit(field_root, args.project, len(claims), reviewed)
    shutil.rmtree(workdir)
    failed = bool(misses) or bool(status.strip())
    print(f"[dryrun] RESULT: {'ISSUES FOUND' if failed else 'OK'}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
