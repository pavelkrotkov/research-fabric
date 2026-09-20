#!/usr/bin/env python3
"""Offline rehearsal of the real shared post-compilation publisher.

The input run supplies accepted packets, frozen sources/manifest and the accepted
compile receipt. The field_repo argument is a prepared compiled candidate
worktree. Both are copied before publication, so rehearsal writes only to a
disposable clone and never calls OpenKB, CAO, an LLM, or a private corpus.
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
from research_fabric.core import normalize_packet, source_mappings  # noqa: E402
from research_fabric.publication import publish_candidate  # noqa: E402
from research_fabric.reading import ReadingPlan  # noqa: E402
from research_fabric.sources import discover_sources  # noqa: E402


def accept_and_persist_packet(packet_path, worker, source_dir, destination, reading_plan=None, acceptance=None):
    """Legacy preparation helper; persist migration only in a disposable destination."""
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
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root")
    parser.add_argument("field_repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--project", default="odyssey")
    return parser.parse_args()


def _load_project(name):
    path = PROJECTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise RuntimeError(f"no project spec at {path}")
    return yaml.safe_load(path.read_text())


def _source_context(run_root, project):
    book_re = re.compile(project["snapshot_pattern"])
    source_files, books = [], []
    for path in discover_sources(run_root / "sources"):
        match = book_re.search(path.name)
        if match:
            source_files.append(path)
            books.append(int(match.group(1)))
    if not source_files:
        raise RuntimeError("no supported source snapshots")
    return source_files, *source_mappings(project, sorted(set(books))), sorted(set(books))


def _run_sources(run_root, project):
    if "reading" not in project:
        source_files, notes, sources, books = _source_context(run_root, project)
        return source_files, notes, sources, None, books
    from research_fabric._source_assets import safe_path

    source_root = run_root / "sources"
    paths = {name: safe_path(source_root, name) for name in project["reading"]["sources"]}
    plan = ReadingPlan.load(run_root / "reading-plan.json", paths, project["reading"])
    sources = {name: row["source_id"] for name, row in plan.data["sources"].items()}
    return list(paths.values()), {}, sources, plan, []


def _manifest(run_root):
    path = run_root / "source-manifest.jsonl"
    if not path.is_file():
        raise RuntimeError(f"offline rehearsal requires prepared manifest: {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _compiled(run_root):
    receipts = list((run_root / "verification/compile").glob("*/completed.json"))
    if len(receipts) != 1:
        raise RuntimeError(f"offline rehearsal requires one accepted compile receipt, found {len(receipts)}")
    return json.loads(receipts[0].read_text())["report"]


def _overlay_candidate(source, destination):
    source = pathlib.Path(source).resolve()
    for name in ("wiki", "raw"):
        src, dst = source / name, destination / name
        if dst.exists():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)


def _clone(field_repo, branch):
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="dryrun-pub-"))
    field_root = workdir / "kb"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", "--branch", branch, str(field_repo), str(field_root)],
        check=True,
    )
    subprocess.run(["git", "-C", str(field_root), "config", "user.email", "dryrun@local"], check=True)
    subprocess.run(["git", "-C", str(field_root), "config", "user.name", "dryrun"], check=True)
    _overlay_candidate(field_repo, field_root)
    return workdir, field_root


def _message(project_name, project, run_root, books, reading_plan):
    default = (
        "{project} evidence run ({claims} claims; run {run})"
        if reading_plan
        else "{project} evidence run ({claims} claims, Books {books}; run {run})"
    )
    template = project.get("commit_message") or default
    return lambda claims: template.format(
        project=project_name.capitalize(),
        claims=claims,
        books="" if reading_plan else "-".join(map(str, (books[0], books[-1]))),
        run=run_root.name,
    )


def main():
    args = _parse_args()
    input_run = pathlib.Path(args.run_root).resolve()
    field_repo = pathlib.Path(args.field_repo).resolve()
    project = _load_project(args.project)
    workdir, field_root = _clone(field_repo, args.branch)
    run_root = workdir / "run"
    shutil.copytree(input_run, run_root)
    try:
        source_files, notes, sources, reading_plan, books = _run_sources(run_root, project)
        result = publish_candidate(
            field_root=field_root,
            run_root=run_root,
            engine_root=FABRIC,
            source_dir=run_root / "sources",
            source_files=source_files,
            manifest_rows=_manifest(run_root),
            worker_ids=sorted(path.stem.removeprefix("worker-") for path in (run_root / "evidence").glob("worker-*.json")),
            notes=notes,
            sources=sources,
            compiled=_compiled(run_root),
            message=_message(args.project, project, run_root, books, reading_plan),
            multi=bool(project.get("witnesses")),
            reading_plan=reading_plan,
            acceptance=project.get("acceptance") or {},
            alignment=run_root / "alignment.jsonl",
        )
    except (AssertionError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[dryrun] FAIL: {exc}")
        print(f"[dryrun] disposable diagnostics preserved: {workdir}")
        return 1
    print(f"[dryrun] commit={result['commit'][:12]} claims={len(result['claims'])} gates={len(result['gates'])}")
    shutil.rmtree(workdir)
    print("[dryrun] RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
