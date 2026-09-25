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

FABRIC = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from research_fabric.core import load_project, source_mappings  # noqa: E402
from research_fabric.publication import publish_candidate  # noqa: E402
from research_fabric.reading import ReadingPlan  # noqa: E402
from research_fabric.sources import discover_sources  # noqa: E402


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root")
    parser.add_argument("field_repo")
    branch = parser.add_mutually_exclusive_group(required=True)
    branch.add_argument("--branch", help="existing proposal branch (not main/master)")
    branch.add_argument("--base", help="existing local base branch; create an isolated rehearsal proposal")
    parser.add_argument("--project", default="odyssey")
    parser.add_argument("--engine-root", type=pathlib.Path, default=FABRIC)
    parser.add_argument("--projects-dir", type=pathlib.Path)
    return parser.parse_args()


def _source_context(run_root, project):
    book_re = re.compile(project["snapshot_pattern"])
    matched = [(path, book_re.search(path.name)) for path in discover_sources(run_root / "sources")]
    source_files = [path for path, match in matched if match]
    if not source_files:
        raise RuntimeError("no supported source snapshots")
    books = sorted({int(match.group(1)) for _, match in matched if match})
    return source_files, *source_mappings(project, books), books


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
        shutil.rmtree(dst, ignore_errors=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)


def _require_local_tree(root):
    # Root aliases are fine; nested links could write back into the input tree.
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"nested symlink not allowed in rehearsal input: {path}")


def _clone(field_repo, branch, *, workdir=None, proposal=False):
    workdir = workdir or pathlib.Path(tempfile.mkdtemp(prefix="dryrun-pub-")).resolve()
    field_root = workdir / "kb"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", "--branch", branch, str(field_repo), str(field_root)],
        check=True,
    )
    subprocess.run(["git", "-C", str(field_root), "config", "user.email", "dryrun@local"], check=True)
    subprocess.run(["git", "-C", str(field_root), "config", "user.name", "dryrun"], check=True)
    if proposal:
        subprocess.run(["git", "-C", str(field_root), "switch", "-c", f"rehearsal/{workdir.name}"], check=True)
    _require_local_tree(field_root)
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
    workdir = None
    try:
        engine_root = args.engine_root.resolve()
        projects_dir = (args.projects_dir or engine_root / "projects").resolve()
        project = load_project(projects_dir, args.project)
        for script in ("provenance_validate.py", "excerpt_grounding.py", "translation_grounding.py"):
            if not (engine_root / "bin" / script).is_file():
                raise ValueError(f"missing publisher gate: {engine_root / 'bin' / script}")
        field_repo = pathlib.Path(args.field_repo).resolve()
        branch = args.branch or args.base
        if args.branch in ("main", "master"):
            raise ValueError("--branch must be a proposal branch, not main/master; use --base instead")
        subprocess.run(["git", "check-ref-format", "--branch", branch], check=True)
        subprocess.run(["git", "-C", str(field_repo), "show-ref", "--verify", "refs/heads/" + branch], check=True)
        prepared = pathlib.Path(args.run_root).resolve()
        _require_local_tree(prepared)
        _require_local_tree(field_repo)
        _manifest(prepared)
        _compiled(prepared)
        _run_sources(prepared, project)
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="dryrun-pub-")).resolve()
        _, field_root = _clone(field_repo, branch, workdir=workdir, proposal=args.base is not None)
        run_root = workdir / "run"
        shutil.copytree(prepared, run_root, symlinks=True)
        source_files, notes, sources, reading_plan, books = _run_sources(run_root, project)
        workers = sorted(path.stem.removeprefix("worker-") for path in (run_root / "evidence").glob("worker-*.json"))
        result = publish_candidate(
            field_root=field_root,
            run_root=run_root,
            engine_root=engine_root,
            source_dir=run_root / "sources",
            source_files=source_files,
            manifest_rows=_manifest(run_root),
            worker_ids=workers,
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
        if workdir is not None:
            print(f"[dryrun] disposable diagnostics preserved: {workdir}")
        return 1
    print(f"[dryrun] commit={result['commit'][:12]} claims={len(result['claims'])} gates={len(result['gates'])}")
    shutil.rmtree(workdir)
    print("[dryrun] RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
