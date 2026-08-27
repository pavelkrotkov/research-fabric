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
import argparse, hashlib, json, pathlib, re, shutil, subprocess, sys, tempfile, yaml

FABRIC = pathlib.Path("/home/pavel/research-fabric")
PROJECTS_DIR = FABRIC / "projects"


def _load_project(name):
    path = PROJECTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise SystemExit(f"no project spec at {path}")
    return yaml.safe_load(path.read_text())


def _template(tpl, **kw):
    return tpl.format(**kw)


def normalize_packet(value):
    for claim in value.get("claims", []):
        if isinstance(claim, dict):
            if isinstance(claim.get("source_file"), str):
                claim["source_file"] = re.sub(r"\s+", "", claim["source_file"])
            if isinstance(claim.get("excerpt"), str):
                claim["excerpt"] = re.sub(r"\s+", " ", claim["excerpt"]).strip()
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_root")
    ap.add_argument("field_repo")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--project", default="odyssey")
    args = ap.parse_args()
    run_root = pathlib.Path(args.run_root).resolve()
    project = _load_project(args.project)
    # Mirror the engine's dynamic source mapping from the project spec.
    BOOK_RE = re.compile(project["snapshot_pattern"])
    source_dir = run_root / "sources"
    packet_dir = run_root / "evidence"
    source_files = sorted(p for p in source_dir.glob("*.html") if BOOK_RE.search(p.name))
    books = [int(m.group(1)) for p in source_files if (m := BOOK_RE.search(p.name))]
    _sid = project["source_id_template"]
    _nt = project["note_template"]
    _bt = project.get("book_label_template", "{n}.html")
    SOURCE_BY_FILE = {_bt.format(n=b): _sid.format(n=b) for b in books}
    NOTE_BY_SOURCE = {_sid.format(n=b): _nt.format(n=b) for b in books}

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="dryrun-pub-"))
    field_root = workdir / "kb"
    print(f"[dryrun] disposable clone: {field_root}")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks",
         "--branch", args.branch,
         args.field_repo, str(field_root)],
        check=True)
    subprocess.run(["git", "-C", str(field_root), "config", "user.email", "dryrun@local"], check=True)
    subprocess.run(["git", "-C", str(field_root), "config", "user.name", "dryrun"], check=True)

    # --- manifest resolution + digest re-verification -----------------------
    run_manifest = run_root / "source-manifest.jsonl"
    canonical = pathlib.Path(__file__).resolve().parents[1] / "corpora" / project["corpus_dir"] / project["manifest_path"]
    manifest_path = run_manifest if run_manifest.exists() else canonical
    assert manifest_path.exists(), f"no manifest at {manifest_path}"
    print(f"[dryrun] manifest: {manifest_path}")
    manifest_rows = [json.loads(l) for l in manifest_path.read_text().splitlines() if l.strip()]
    by_name = {pathlib.Path(r["snapshot"]).name: r for r in manifest_rows}
    for src in source_files:
        row = by_name.get(src.name)
        assert row is not None, f"manifest missing {src.name}"
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        assert digest == row["sha256"], f"sha256 mismatch {src.name}"
    print(f"[dryrun] manifest digests verified for {len(source_files)} sources")

    # --- snapshots ----------------------------------------------------------
    snap_dest = field_root / "evidence" / "snapshots"
    snap_dest.mkdir(parents=True, exist_ok=True)
    for src in source_files:
        dest = snap_dest / src.name
        if dest.exists():
            dest.chmod(0o644)
        shutil.copy2(src, dest)
        dest.chmod(0o444)
    print(f"[dryrun] snapshots copied: {len(source_files)}")

    # --- ledger materialization --------------------------------------------
    claims, dropped = [], []
    for packet_path in sorted(packet_dir.glob("worker-*.json")):
        sid = packet_path.stem.replace("worker-", "")
        packet = json.loads(packet_path.read_text())
        parsed = normalize_packet(packet.get("parsed") or {})
        for idx, claim in enumerate(parsed.get("claims", []), 1):
            source_file = pathlib.Path(claim.get("source_file", "")).name
            source_id = SOURCE_BY_FILE.get(source_file)
            if not source_id:
                dropped.append({"worker": sid, "index": idx, "source_file": claim.get("source_file", "")})
                continue
            note = NOTE_BY_SOURCE[source_id]
            assert (field_root / note).is_file(), f"note target missing: {note}"
            claims.append({
                "claim_id": f"c-{sid}-{idx}", "claim": claim.get("claim", ""), "note": note,
                "source_ids": [source_id], "locator": claim.get("locator", ""),
                "excerpt": claim.get("excerpt", ""), "stance": claim.get("stance", "supports"),
                "confidence": claim.get("confidence", 0.0),
                "independence_group": claim.get("independence_group", sid),
                "verified_at": "pilot-verifier-pass",
            })
    if dropped:
        print(f"[dryrun] DROPPED {len(dropped)} claim(s): {json.dumps(dropped, indent=2)}")
        return 1
    assert claims, "no claims materialized"
    (field_root / "evidence" / "claims.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in claims) + "\n")
    notes_used = sorted({c["note"] for c in claims})
    print(f"[dryrun] claims materialized: {len(claims)}")
    print(f"[dryrun] distinct note targets: {notes_used}")

    manifest_out = []
    present_names = {p.name for p in source_files}
    for row in manifest_rows:
        # Only emit rows for snapshots present in this run (mirrors engine).
        if pathlib.Path(row["snapshot"]).name not in present_names:
            continue
        row = dict(row)
        row["snapshot"] = "evidence/snapshots/" + pathlib.Path(row["snapshot"]).name
        manifest_out.append(json.dumps(row, ensure_ascii=False))
    (field_root / "evidence" / "sources.jsonl").write_text("\n".join(manifest_out) + "\n")

    # --- deterministic provenance gate --------------------------------------
    prov = subprocess.run([sys.executable, str(FABRIC / "bin" / "provenance_validate.py"), str(field_root)],
                          text=True, capture_output=True)
    print(f"[dryrun] provenance rc={prov.returncode} out={prov.stdout.strip()} err={prov.stderr.strip()}")
    if prov.returncode != 0:
        return 1

    # --- deterministic excerpt-grounding gate -------------------------------
    ground = subprocess.run([sys.executable, str(FABRIC / "bin" / "excerpt_grounding.py"), str(field_root)],
                            text=True, capture_output=True)
    print(f"[dryrun] grounding rc={ground.returncode} out={ground.stdout.strip()} err={ground.stderr.strip()}")
    if ground.returncode != 0:
        return 1

    # --- diff generation ----------------------------------------------------
    subprocess.run(["git", "-C", str(field_root), "add", "-N", "--", "evidence"], check=True)
    diff = subprocess.run(["git", "-C", str(field_root), "diff", "--no-ext-diff", "--", "evidence"],
                          check=True, text=True, capture_output=True).stdout
    if not diff.strip():
        print("[dryrun] FAIL: empty diff")
        return 1
    files_in_diff = re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)
    print(f"[dryrun] diff bytes={len(diff)} files={len(files_in_diff)}")
    for f in files_in_diff:
        print(f"[dryrun]   {f}")

    # --- excerpt grounding (mirrors the deterministic gate) -----------------
    sys.path.insert(0, str(FABRIC / "bin"))
    import excerpt_grounding
    snaps = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in snap_dest.iterdir()}
    file_by_source = {v: k for k, v in SOURCE_BY_FILE.items()}
    misses = []
    for c in claims:
        snap_name = file_by_source[c["source_ids"][0]]
        if not excerpt_grounding.grounded(c["excerpt"], snaps[snap_name]):
            misses.append((c["claim_id"], re.sub(r"\s+", " ", c["excerpt"])[:80]))
    print(f"[dryrun] excerpt grounding: {len(claims) - len(misses)}/{len(claims)} grounded")
    for cid, frag in misses[:10]:
        print(f"[dryrun]   MISS {cid}: {frag}...")

    # --- commit -------------------------------------------------------------
    subprocess.run(["git", "-C", str(field_root), "add", "--", "evidence"], check=True)
    subprocess.run(["git", "-C", str(field_root), "diff", "--cached", "--check"], check=True)
    subprocess.run(["git", "-C", str(field_root), "commit", "-q", "-m",
                    f"Research: {args.project} evidence run ({len(claims)} claims)"], check=True)
    head = subprocess.run(["git", "-C", str(field_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    status = subprocess.run(["git", "-C", str(field_root), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    print(f"[dryrun] commit={head[:12]} clean={not status.strip()}")
    if status.strip():
        print(f"[dryrun] residual dirty state:\n{status}")
    shutil.rmtree(workdir)
    print(f"[dryrun] RESULT: {'OK' if not misses and not status.strip() else 'ISSUES FOUND'}")
    return 0 if not misses and not status.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
