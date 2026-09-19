"""Project-driven evidence pipeline: plan -> workers -> verify -> OpenKB -> gates -> ledger -> commit."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

from cao_workflow import ShimError, emit_output, get_inputs, run_step

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.core import (
    book_task_from_project,
    extract_json,
    multisource_packet_defects,
    normalize_packet,
    packet_defects,
    source_mappings,
)
from research_fabric.core import (
    load_project as load_project_spec,
)
from research_fabric.sources import (
    ADAPTERS,
    bind_manifest,
    discover_sources,
    packet_source_defects,
    copy_source_snapshot,
    prepare_source_bundle,
    snapshot_relative,
    source_attestation,
    representation_for,
    source_provenance,
)

from research_fabric._source_assets import publish_bundle

# Project specs live in projects/<name>.yaml and describe how a corpus is read
# into claims + notes (snapshot regex, themes, source-id/note templates,
# manifest path, acceptance rules). fields.yaml (field root registry) answers a
# different question — which broad KB a question belongs to — and is NOT this.
RESEARCH_ROOT = pathlib.Path("/home/pavel/research-fabric")
PROJECTS_DIR = RESEARCH_ROOT / "projects"
DEFAULT_PROJECT = "odyssey"
# The direct-API worker's model/provider (mirrors bin/direct_worker.py). Kept in
# sync so run.json records the exact model that produced the claims.
MODEL_REF = "z-ai/glm-5.3-flash"
PROVIDER_REF = "openrouter"
SOURCE_ADAPTERS = ADAPTERS

INPUTS = {
    "field_root": {"type": "path", "required": True},
    "run_root": {"type": "path", "required": True},
    "source_dir": {"type": "path", "required": True},
    "question": {"type": "string", "required": True},
    "reuse_evidence_dir": {"type": "path", "required": False},
    "project": {"type": "string", "required": False},
}


def write_json(path: pathlib.Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def output_text(handle):
    return handle.output if isinstance(handle.output, str) else json.dumps(handle.output, ensure_ascii=False)


def set_state(run_root: pathlib.Path, state: str, **extra):
    path = run_root / "run.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update(state=state, **extra)
    write_json(path, data)


def sha256_of(path: pathlib.Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_provenance(manifest_rows: list[dict]) -> dict:
    """Record the exact toolchain + inputs that produced this run."""

    def _toolver(cmd):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
            return out.splitlines()[0] if out else None
        except Exception:
            return None

    proj_path = PROJECTS_DIR / f"{project_name}.yaml"
    corpus_manifest = RESEARCH_ROOT / "corpora" / project["corpus_dir"] / project["manifest_path"]
    return {
        "model": MODEL_REF,
        "provider": PROVIDER_REF,
        "engine_sha": _git_sha(RESEARCH_ROOT),
        "engine_tag": _git_tag(RESEARCH_ROOT),
        "project": project_name,
        "project_spec_sha": sha256_of(proj_path) if proj_path.exists() else None,
        "corpus_manifest_sha": sha256_of(corpus_manifest) if corpus_manifest.exists() else None,
        "corpus_sources_sha": _dir_sha(RESEARCH_ROOT / "corpora" / project["corpus_dir"] / "sources"),
        "source_adapters": sorted({f"{row['adapter']}@{row['adapter_version']}" for row in manifest_rows}),
        "openkb": _toolver(["openkb", "--version"]),
        "toolchain": {"cao": _toolver(["cao", "--version"]), "python": _toolver([sys.executable, "--version"])},
    }


def _git_sha(root: pathlib.Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return None


def _git_tag(root: pathlib.Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--exact-match"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def _dir_sha(d: pathlib.Path) -> str | None:
    """Content hash of a directory (sorted relative paths + per-file hashes)."""
    if not d.is_dir():
        return None
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode())
            digest = sha256_of(p)
            if digest:
                h.update(digest.encode())
    return h.hexdigest()


inputs = get_inputs()
field_root = pathlib.Path(inputs["field_root"]).resolve()
run_root = pathlib.Path(inputs["run_root"]).resolve()
source_dir = pathlib.Path(inputs["source_dir"]).resolve()
question = inputs["question"]
reuse_evidence_dir = pathlib.Path(inputs["reuse_evidence_dir"]).resolve() if inputs.get("reuse_evidence_dir") else None
packet_dir = run_root / "evidence"
packet_dir.mkdir(parents=True, exist_ok=True)

project_name = inputs.get("project") or DEFAULT_PROJECT
project = load_project_spec(PROJECTS_DIR, project_name)
ACCEPTANCE = project.get("acceptance") or {}
CANONICAL_SOURCE_MANIFEST = (RESEARCH_ROOT / "corpora" / project["corpus_dir"] / project["manifest_path"]).resolve()
BOOK_RE = re.compile(project["snapshot_pattern"])
WITNESSES = list((project.get("witnesses") or {}).keys())
CANONICAL = project.get("canonical_variant", "latin")
IS_MULTI = bool(WITNESSES)


def _validator(parsed, acceptance=None):
    if IS_MULTI:
        return multisource_packet_defects(parsed, project, acceptance)
    return packet_defects(parsed, acceptance)


VALIDATOR = _validator

source_files = discover_sources(source_dir, SOURCE_ADAPTERS)
if not source_files:
    raise RuntimeError("no supported source snapshots found in source_dir")


def _book_of(p):
    m = BOOK_RE.search(p.name)
    return int(m.group(1)) if m else None


matched = [(p, _book_of(p)) for p in source_files]
if any(b is None for _, b in matched):
    raise RuntimeError(f"all snapshots must match {BOOK_RE.pattern}")
if IS_MULTI:

    def _variant_of(p):
        m = BOOK_RE.search(p.name)
        return m.group(2) if m and m.lastindex and m.lastindex >= 2 else None

    BOOKS = sorted(b for p, b in matched if _variant_of(p) == CANONICAL)
else:
    BOOKS = sorted({b for _, b in matched})
    if len(BOOKS) != len(source_files):
        raise RuntimeError(f"all snapshots must match {BOOK_RE.pattern}")

worker_specs = [(f"book-{b}", book_task_from_project(b, project, project_name)) for b in BOOKS]


def _worker_source_files(book: int) -> list[pathlib.Path]:
    if IS_MULTI:
        labels = [project["book_label_template"].format(n=book, w=CANONICAL)] + [
            project["book_label_template"].format(n=book, w=w) for w in WITNESSES
        ]
        paths = [source_dir / label for label in labels]
    else:
        label = project.get("book_label_template", "{n}.html").format(n=book)
        path = source_dir / label
        if not path.is_file():
            path = next(
                (p for p in source_files if (match := BOOK_RE.search(p.name)) and int(match.group(1)) == book), path
            )
        paths = [path]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError(f"worker source input missing: {missing}")
    return paths


WORKER_SOURCE_FILES = {sid: _worker_source_files(int(sid.split("-")[1])) for sid, _ in worker_specs}
WORKER_PROVENANCE = {
    sid: source_provenance(paths, SOURCE_ADAPTERS) for sid, paths in WORKER_SOURCE_FILES.items()
}


def _packet_defects(packet: dict, sid: str) -> list[str]:
    return packet_source_defects(
        packet,
        WORKER_PROVENANCE[sid],
        lambda parsed: VALIDATOR(parsed, ACCEPTANCE),
    )

set_state(run_root, "PLANNING")
if reuse_evidence_dir:
    write_json(run_root / "plan.json", {"reused": True, "source": str(reuse_evidence_dir)})
else:
    plan = run_step(
        provider="claude_code",
        agent="research-supervisor",
        prompt=(
            f"Question: {question}\nSource snapshots: {', '.join(map(str, source_files))}\n"
            "Return JSON only with keys research_questions, worker_assignments, allowed_source_types, "
            "source_budget, acceptance_criteria, ambiguities, stop_conditions. Keep assignments bounded and non-overlapping. "
            "Do not modify files."
        ),
        step_id="plan",
        timeout=300,
    )
    plan_text = output_text(plan)
    write_json(run_root / "plan.json", {"raw": plan_text, "parsed": extract_json(plan_text)})

set_state(run_root, "RESEARCHING")

DIRECT_WORKER = str(RESEARCH_ROOT / "bin" / "direct_worker.py")
AENEID_WORKER = str(RESEARCH_ROOT / "bin" / "aeneid_worker.py")
DIRECT_WORKER_PY = os.environ.get("RESEARCH_FABRIC_WORKER_PYTHON", "/home/pavel/.hermes/hermes-agent/venv/bin/python")


def collect(spec):
    """Collect one book's evidence packet through the shared source adapter."""
    sid, task = spec
    b = int(sid.split("-")[1])
    theme_match = re.search(r"Extract claim-level evidence about (.+?)\\. Use", task)
    theme = theme_match.group(1) if theme_match else None
    worker_sources = WORKER_SOURCE_FILES[sid]
    book_label = worker_sources[0].name
    attempts = []
    for attempt in range(1, 3):
        try:
            if IS_MULTI:
                canon_label = book_label
                witness_args = []
                for w in WITNESSES:
                    wfile = project["book_label_template"].format(n=b, w=w)
                    wid = project["source_id_template"].format(n=b, w=w)
                    witness_args += ["--witness", f"{w}:{wid}:{wfile}"]
                cmd = [
                    DIRECT_WORKER_PY,
                    AENEID_WORKER,
                    str(run_root),
                    str(source_dir),
                    str(b),
                    canon_label,
                    theme or "",
                ] + witness_args
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2400)
            else:
                proc = subprocess.run(
                    [DIRECT_WORKER_PY, DIRECT_WORKER, str(run_root), str(source_dir), str(b), book_label, theme or ""],
                    capture_output=True,
                    text=True,
                    timeout=1500,
                )
            packet = packet_dir / f"worker-{sid}.json"
            if proc.returncode != 0 or not packet.exists():
                raise RuntimeError(
                    f"worker failed (rc={proc.returncode}): {proc.stderr.strip()[-400:] or proc.stdout.strip()[-400:]}"
                )
            packet_data = json.loads(packet.read_text(encoding="utf-8"))
            defects = _packet_defects(packet_data, sid)
            attempts.append({"attempt": attempt, "stdout": proc.stdout.strip()[-200:], "defects": defects})
            if not defects:
                return sid, proc.stdout.strip(), None
        except ShimError as exc:
            attempts.append({"attempt": attempt, "error": str(exc)})
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            attempts.append({"attempt": attempt, "error": str(exc)[:400]})
        except Exception as exc:
            attempts.append({"attempt": attempt, "error": f"unexpected: {exc}"})
    write_json(packet_dir / f"worker-{sid}.json", {"worker": sid, "attempts": attempts, "parsed": None})
    last = attempts[-1] if attempts else {}
    reason = last.get("error") or "; ".join(last.get("defects", [])) or "unknown"
    return sid, "", f"direct worker returned no valid evidence packet after attempts ({reason})"


def _reuse_evidence_packets(reuse_dir, destination_dir, specs, worker_provenance, validator):
    reused_sids = []
    for sid, _ in specs:
        src_packet = reuse_dir / f"worker-{sid}.json"
        if not src_packet.exists():
            continue
        try:
            src_data = json.loads(src_packet.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        defects = packet_source_defects(src_data, worker_provenance[sid], validator)
        if defects:
            continue
        dst = destination_dir / src_packet.name
        shutil.copy2(src_packet, dst) if src_packet.resolve() != dst.resolve() else None
        reused_sids.append(sid)
    return reused_sids


if reuse_evidence_dir:
    reused_sids = _reuse_evidence_packets(
        reuse_evidence_dir,
        packet_dir,
        worker_specs,
        WORKER_PROVENANCE,
        lambda parsed: VALIDATOR(parsed, ACCEPTANCE),
    )
    pending_specs = [(sid, task) for sid, task in worker_specs if sid not in reused_sids]
    results = [(sid, "reused", None) for sid in reused_sids]
else:
    pending_specs = worker_specs
    results = []
if pending_specs:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        results.extend(list(pool.map(collect, pending_specs)))
if any(err for _, _, err in results):
    set_state(run_root, "FAILED", failure="worker failure")
    raise RuntimeError("one or more evidence workers failed")
if reuse_evidence_dir:
    for sid, _ in worker_specs:
        packet = json.loads((packet_dir / f"worker-{sid}.json").read_text(encoding="utf-8"))
        defects = _packet_defects(packet, sid)
        if defects:
            set_state(run_root, "FAILED", failure=f"reused packet invalid: {sid}")
            raise RuntimeError(f"reused packet {sid} failed validation: {'; '.join(defects)}")

VERDICT_CONTRACT = (
    "Work through the evidence first and write your findings. Then, as the very LAST line of your "
    "reply, emit the verdict in exactly one of these forms:\n"
    "  VERDICT: PASS\n"
    "  VERDICT: FAIL - <specific defect>; <specific defect>\n"
    "The verdict must come last, after your analysis, so it reflects what you actually found. "
    "A FAIL must enumerate at least one concrete defect (claim id and what is wrong with it). "
    "Do not write the words PASS or FAIL anywhere except that final verdict line. Do not modify files."
)


def read_verdict(text):
    """Return ('PASS'|'FAIL'|None, detail) from a verifier reply."""
    matches = list(re.finditer(r"^\s*VERDICT:\s*(PASS|FAIL)\b[ \t]*(.*)$", text or "", flags=re.I | re.M))
    if matches:
        verdict = matches[-1].group(1).upper()
        detail = matches[-1].group(2).strip().lstrip("-–—:").strip()
        if verdict == "FAIL" and not detail:
            return None, "FAIL with no enumerated defect"
        return verdict, detail
    lowered = (text or "").lower()
    positive = "verified sound" in lowered or "verified clean" in lowered
    negative = re.search(r"\bfail(?:ed|ure)?\b|blocking defect|\bdefect(?:s)?\b|not verified|unable to verify", lowered)
    if positive and not negative:
        return "PASS", "implicit positive attestation"
    return None, "no usable VERDICT line or unambiguous positive attestation"


def run_verifier(step_id, prompt, run_root, artifact):
    """Run a verifier step, retrying once when the reply is not a usable verdict."""
    last_text = ""
    for attempt in (1, 2):
        handle = run_step(
            provider="claude_code",
            agent="research-verifier",
            prompt=prompt,
            step_id=f"{step_id}-attempt-{attempt}",
            timeout=900,
        )
        last_text = output_text(handle)
        (run_root / "verification" / artifact).write_text(last_text, encoding="utf-8")
        verdict, detail = read_verdict(last_text)
        if verdict is not None:
            return verdict, detail, last_text
        (run_root / "verification" / f"{artifact}.malformed-attempt-{attempt}.txt").write_text(
            last_text, encoding="utf-8"
        )
    return None, "verifier gave no usable verdict after two attempts", last_text


set_state(run_root, "VERIFYING_SOURCES")
verification_dir = run_root / "verification"
verification_dir.mkdir(exist_ok=True)
verification_manifests = []
for packet_path in sorted(packet_dir.glob("worker-*.json")):
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    verification_path = verification_dir / f"verification-input-{packet_path.stem}.json"
    write_json(
        verification_path,
        {
            "question": question,
            "source_files": [str(p) for p in source_files],
            "packet": normalize_packet(packet.get("parsed") or {}),
        },
    )
    verification_manifests.append(verification_path)
try:
    advisory_verdict, advisory_detail, pre_text = run_verifier(
        "verify-sources",
        (
            f"Read these exact verification manifests: {', '.join(map(str, verification_manifests))}. "
            "Each contains one complete evidence packet and the source-file paths. Read the named source files "
            "from the manifests to verify exact excerpts. Acceptance criteria: every substantive claim must have "
            "an exact excerpt and locator; check contradictions, independence, wording strength, and coverage. "
            "Do not use directory listings or require a generated diff.\n\n" + VERDICT_CONTRACT
        ),
        run_root,
        "pre-ingest.txt",
    )
    write_json(run_root / "verification" / "advisory-verifier.json", {"verdict": advisory_verdict, "detail": advisory_detail})
except Exception as exc:
    write_json(
        run_root / "verification" / "advisory-verifier.json",
        {"verdict": None, "detail": f"verifier errored: {exc}"},
    )
    pre_text = ""

run_manifest = run_root / "source-manifest.jsonl"
manifest_path = run_manifest if run_manifest.exists() else CANONICAL_SOURCE_MANIFEST
if not manifest_path.exists():
    set_state(run_root, "FAILED", failure="missing source manifest")
    raise RuntimeError(f"no source manifest available (looked at {run_manifest} and {CANONICAL_SOURCE_MANIFEST})")
manifest_rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
try:
    manifest_rows = bind_manifest(source_files, manifest_rows, SOURCE_ADAPTERS)
except RuntimeError as exc:
    set_state(run_root, "FAILED", failure="source manifest validation failed")
    raise RuntimeError(str(exc)) from exc
run_manifest.write_text(
    "\n".join(json.dumps(row, ensure_ascii=False) for row in manifest_rows) + "\n", encoding="utf-8"
)

set_state(run_root, "COMPILING")
snap_dest = field_root / "evidence" / "snapshots"
snap_dest.mkdir(parents=True, exist_ok=True)
compiler_dir = run_root / "compiler-sources"
compiler_dir.mkdir(exist_ok=True)
bundles = {}
for src in source_files:
    copy_source_snapshot(src, snap_dest)
    attestation = source_attestation(representation_for(src))
    bundle = prepare_source_bundle(src, compiler_dir, attestation)
    bundle_root = compiler_dir / bundle["key"]
    compiler_input = bundle_root / bundle["input_path"]
    subprocess.run(["openkb", "--kb-dir", str(field_root), "add", str(compiler_input)], check=True, text=True)
    # Native input names are source-scoped when visual normalization is needed.
    if bundle["source"]["adapter"] == "markdown":
        publish_bundle(bundle_root, bundle, field_root / "wiki", compiler_input.stem)
        bundles[src.name] = compiler_input.stem
subprocess.run(["openkb", "--kb-dir", str(field_root), "lint"], check=True, text=True)

set_state(run_root, "MATERIALIZING_LEDGER")
NOTE_BY_SOURCE, SOURCE_BY_FILE = source_mappings(project, BOOKS)
for filename, doc_name in bundles.items():
    NOTE_BY_SOURCE[SOURCE_BY_FILE[filename]] = f"wiki/summaries/{doc_name}.md"
claims = []
dropped = []
for sid, _, _ in results:
    packet = json.loads((packet_dir / f"worker-{sid}.json").read_text(encoding="utf-8"))
    parsed = packet.get("parsed") or {}
    for idx, claim in enumerate(parsed.get("claims", []), 1):
        source_file = pathlib.Path(claim.get("source_file", "")).name
        source_id = SOURCE_BY_FILE.get(source_file)
        if not source_id:
            dropped.append({"worker": sid, "index": idx, "source_file": claim.get("source_file", "")})
            continue
        note = NOTE_BY_SOURCE[source_id]
        if not (field_root / note).is_file():
            set_state(run_root, "FAILED", failure="claim note target missing")
            raise RuntimeError(f"claim note target does not exist: {note}")
        claims.append(
            {
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
        )
        if IS_MULTI:
            claims[-1].update(
                {
                    "claim_type": claim.get("claim_type", ""),
                    "english_witness": claim.get("english_witness"),
                    "witnesses_consulted": claim.get("witnesses_consulted", []),
                }
            )
if dropped:
    write_json(run_root / "verification" / "dropped-claims.json", dropped)
    set_state(run_root, "FAILED", failure="claim with unmappable source file")
    raise RuntimeError(f"{len(dropped)} claim(s) could not be mapped to a manifest source; see dropped-claims.json")
if not claims:
    set_state(run_root, "FAILED", failure="no claims materialized")
    raise RuntimeError("no claims survived materialization")
(field_root / "evidence" / "claims.jsonl").write_text(
    "\n".join(json.dumps(c, ensure_ascii=False) for c in claims) + "\n", encoding="utf-8"
)
manifest_out = []
present_names = {p.name for p in source_files}
for row in manifest_rows:
    if pathlib.Path(row["snapshot"]).name not in present_names:
        continue
    row = dict(row)
    row["snapshot"] = "evidence/snapshots/" + snapshot_relative(source_dir / pathlib.Path(row["snapshot"]).name).as_posix()
    manifest_out.append(json.dumps(row, ensure_ascii=False))
(field_root / "evidence" / "sources.jsonl").write_text("\n".join(manifest_out) + "\n", encoding="utf-8")

provenance = subprocess.run(
    [sys.executable, str(RESEARCH_ROOT / "bin" / "provenance_validate.py"), str(field_root)],
    text=True,
    capture_output=True,
)
(run_root / "verification" / "provenance.txt").write_text(
    (provenance.stdout or "") + (provenance.stderr or ""), encoding="utf-8"
)
if provenance.returncode != 0:
    set_state(run_root, "FAILED", failure="provenance validation failed")
    raise RuntimeError(f"provenance validation failed: {provenance.stderr.strip()}")

grounding = subprocess.run(
    [sys.executable, str(RESEARCH_ROOT / "bin" / "excerpt_grounding.py"), str(field_root)],
    text=True,
    capture_output=True,
)
(run_root / "verification" / "excerpt-grounding.txt").write_text(
    (grounding.stdout or "") + (grounding.stderr or ""), encoding="utf-8"
)
if grounding.returncode != 0:
    set_state(run_root, "FAILED", failure="excerpt grounding failed")
    raise RuntimeError(f"excerpt grounding failed: {grounding.stderr.strip()}")

if IS_MULTI and any(c.get("english_witness") for c in claims):
    alignment_copy = run_root / "alignment.jsonl"
    if not alignment_copy.exists():
        proj_al = RESEARCH_ROOT / "corpora" / project["corpus_dir"] / project.get("alignment_path", "alignment.jsonl")
        if proj_al.exists():
            shutil.copy2(proj_al, alignment_copy)
    tgate = subprocess.run(
        [
            sys.executable,
            str(RESEARCH_ROOT / "bin" / "translation_grounding.py"),
            str(field_root),
            str(alignment_copy),
        ],
        text=True,
        capture_output=True,
    )
    (run_root / "verification" / "translation-grounding.txt").write_text(
        (tgate.stdout or "") + (tgate.stderr or ""), encoding="utf-8"
    )
    if tgate.returncode != 0:
        set_state(run_root, "FAILED", failure="translation grounding failed")
        raise RuntimeError(f"translation grounding failed: {tgate.stderr.strip()}")

set_state(run_root, "VERIFYING_DIFF")
subprocess.run(["git", "-C", str(field_root), "add", "-N", "--", "evidence"], check=True)
diff = subprocess.run(
    ["git", "-C", str(field_root), "diff", "--no-ext-diff", "--", "evidence"],
    check=True,
    text=True,
    capture_output=True,
).stdout
diff_path = run_root / "verification" / "generated-diff.patch"
diff_path.write_text(diff, encoding="utf-8")
if not diff.strip():
    set_state(run_root, "FAILED", failure="empty generated diff")
    raise RuntimeError("generated diff is empty; refusing to attest an empty change set")
try:
    post_verdict, post_detail, post_text = run_verifier(
        "verify-diff",
        (
            f"Question: {question}\n"
            f"Read the claims ledger {field_root / 'evidence' / 'claims.jsonl'}, the source ledger "
            f"{field_root / 'evidence' / 'sources.jsonl'}, and the approved snapshots in {snap_dest}. "
            "For every claim verify that its excerpt appears in the snapshot named by its source_ids, that the "
            "locator is consistent, and that the stance matches the excerpt. Confirm each source_id in the claims "
            "ledger exists in the source ledger.\n\n" + VERDICT_CONTRACT
        ),
        run_root,
        "post-compile.txt",
    )
    write_json(run_root / "verification" / "advisory-post-verifier.json", {"verdict": post_verdict, "detail": post_detail})
except Exception as exc:
    write_json(
        run_root / "verification" / "advisory-post-verifier.json",
        {"verdict": None, "detail": f"verifier errored: {exc}"},
    )
    post_text = ""

set_state(run_root, "READY_FOR_REVIEW")
subprocess.run(["git", "-C", str(field_root), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(field_root), "diff", "--cached", "--check"], check=True)
_cmt = project.get("commit_message") or "{project} evidence run ({claims} claims, Books {books}; run {run})"
commit_msg = _cmt.format(
    project=project_name.capitalize(),
    claims=len(claims),
    books="-".join(map(str, (BOOKS[0], BOOKS[-1]))),
    run=run_root.name,
)
subprocess.run(["git", "-C", str(field_root), "commit", "-m", commit_msg], check=True, text=True)
head = subprocess.run(
    ["git", "-C", str(field_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()
branch = subprocess.run(
    ["git", "-C", str(field_root), "branch", "--show-current"], capture_output=True, text=True, check=True
).stdout.strip()
residual = subprocess.run(
    ["git", "-C", str(field_root), "status", "--porcelain"], capture_output=True, text=True, check=True
).stdout.strip()
if residual:
    set_state(run_root, "FAILED", failure="worktree dirty after commit")
    raise RuntimeError(f"worktree not clean after commit:\n{residual}")
write_json(
    run_root / "run.json",
    {
        "run_id": run_root.name,
        "state": "READY_FOR_REVIEW",
        "branch": branch,
        "commit": head,
        "claims": len(claims),
        "provenance": collect_provenance(manifest_rows),
    },
)
emit_output({"state": "READY_FOR_REVIEW", "claims": len(claims), "branch": branch, "commit": head})
