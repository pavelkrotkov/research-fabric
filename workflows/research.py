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

# Project specs live in projects/<name>.yaml and describe how a corpus is read
# into claims + notes (snapshot regex, themes, source-id/note templates,
# manifest path, acceptance rules). fields.yaml (field root registry) answers a
# different question — which broad KB a question belongs to — and is NOT this.
RESEARCH_ROOT = pathlib.Path("/home/pavel/research-fabric")
PROJECTS_DIR = RESEARCH_ROOT / "projects"
DEFAULT_PROJECT = "odyssey"
# The direct-API worker's model/provider (mirrors bin/direct_worker.py). Kept in
# sync so run.json records the exact model that produced the claims.
MODEL_REF = "stealth/ox-alpha"
PROVIDER_REF = "openrouter"

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


def collect_provenance() -> dict:
    """Record the exact toolchain + inputs that produced this run, so a KB
    commit is reproducible (and auditable) without the original session."""

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
# Immutable provenance root of trust. Runs that do not carry their own
# source-manifest.jsonl fall back to the canonical copy derived from the
# project spec; digests are re-verified against the run's actual source bytes
# before anything is published.
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

source_files = sorted(source_dir.glob("*.html"))
if not source_files:
    raise RuntimeError("no source snapshots found in source_dir")
# Every snapshot must match the pattern. Books are enumerated from the
# canonical variant only (so 12 books, not 4×12); witness files ride along as
# non-authoritative interpretive sources.
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
DIRECT_WORKER_PY = os.environ.get(
    "RESEARCH_FABRIC_WORKER_PYTHON", "/home/pavel/.hermes/hermes-agent/venv/bin/python")


def collect(spec):
    """Collect one book's evidence packet via the direct-API worker (ox-alpha).

    Replaces the CAO tmux worker path: the worker reads the book's HTML, calls
    stealth/ox-alpha directly through OpenRouter, and writes a structurally
    validated packet to packet_dir. No terminal UI, no scrollback scraping, no
    provider quota. The packet is re-validated here (fail-closed) before use.
    """
    sid, task = spec
    b = int(sid.split("-")[1])
    theme_match = re.search(r"Extract claim-level evidence about (.+?)\\. Use", task)
    theme = theme_match.group(1) if theme_match else None
    # Resolve the snapshot filename for this book from the project's label
    # template so SOURCE_BY_FILE can map the worker's emitted source_file back.
    if IS_MULTI:
        book_label = project["book_label_template"].format(n=b, w=CANONICAL)
    else:
        book_label = project.get("book_label_template", "{n}.html").format(n=b)
    if not (source_dir / book_label).is_file():
        book_label = next(
            (p.name for p in source_files if (m := BOOK_RE.search(p.name)) and int(m.group(1)) == b), book_label
        )
    validator = VALIDATOR
    attempts = []
    for attempt in range(1, 3):
        try:
            if IS_MULTI:
                # Canonical Latin label + witness labels/source-ids → aeneid worker.
                canon_label = book_label
                witness_args = []
                for w in WITNESSES:
                    wfile = project["book_label_template"].format(n=b, w=w)
                    wid = project["source_id_template"].format(n=b, w=w)
                    witness_args += ["--witness", f"{w}:{wid}:{wfile}"]
                cmd = [DIRECT_WORKER_PY, AENEID_WORKER, str(run_root), str(source_dir),
                       str(b), canon_label, theme or ""] + witness_args
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2400)
            else:
                proc = subprocess.run(
                    [DIRECT_WORKER_PY, DIRECT_WORKER, str(run_root), str(source_dir), str(b), book_label, theme or ""],
                    capture_output=True, text=True, timeout=1500,
                )
            packet = packet_dir / f"worker-{sid}.json"
            if proc.returncode != 0 or not packet.exists():
                raise RuntimeError(
                    f"worker failed (rc={proc.returncode}): {proc.stderr.strip()[-400:] or proc.stdout.strip()[-400:]}"
                )
            parsed = json.loads(packet.read_text(encoding="utf-8")).get("parsed")
            defects = VALIDATOR(parsed, ACCEPTANCE)
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


# The host has two very old cores; concurrent Codex tmux shell creation
# reliably exceeds CAO's 60s shell-init gate. Keep both independent tasks,
# but serialize launch for reliability. A completed evidence directory may be
# reused after an interrupted verifier/publication phase; packets are still
# structurally rechecked below before any downstream mutation.
if reuse_evidence_dir:
    # Partial reuse: only packets that pass structural validation are carried
    # forward; missing or invalid packets are re-collected below.
    reused_sids = []
    for sid, _ in worker_specs:
        src_packet = reuse_evidence_dir / f"worker-{sid}.json"
        if not src_packet.exists():
            continue
        try:
            src_data = json.loads(src_packet.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if VALIDATOR(src_data.get("parsed"), ACCEPTANCE):
            continue  # invalid packet — re-collect instead of propagating it
        dst = packet_dir / src_packet.name
        shutil.copy2(src_packet, dst) if src_packet.resolve() != dst.resolve() else None
        reused_sids.append(sid)
    # Only the non-reused workers are collected now.
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
        defects = VALIDATOR(packet.get("parsed"), ACCEPTANCE)
        if defects:
            set_state(run_root, "FAILED", failure=f"reused packet invalid: {sid}")
            raise RuntimeError(f"reused packet {sid} failed structural validation: {'; '.join(defects)}")

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
    """Return ('PASS'|'FAIL'|None, detail) from a verifier reply.

    The verdict is taken from the LAST ``VERDICT:`` line, so an agent that
    thinks aloud before deciding is judged on its conclusion rather than on a
    token emitted mid-analysis. A FAIL that enumerates no defect is treated as
    malformed (None) rather than as a real failure: publication6's verifier
    printed a bare FAIL and then stated "Verified clean ... all 37 locators
    resolve" with an empty defect list, which is an unusable verdict in either
    direction and must be retried, not believed.
    """
    matches = list(re.finditer(r"^\s*VERDICT:\s*(PASS|FAIL)\b[ \t]*(.*)$", text or "", flags=re.I | re.M))
    if matches:
        verdict = matches[-1].group(1).upper()
        detail = matches[-1].group(2).strip().lstrip("-–—:").strip()
        if verdict == "FAIL" and not detail:
            return None, "FAIL with no enumerated defect"
        return verdict, detail

    # Claude occasionally omits the requested final marker and emits a short
    # positive attestation instead. Accept only an unambiguous positive
    # attestation with no negative/defect language anywhere. This deliberately
    # does not accept "Verified clean" when a FAIL token is also present (the
    # publication6 contradiction that motivated the strict contract).
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
    manifest_path = verification_dir / f"verification-input-{packet_path.stem}.json"
    write_json(
        manifest_path,
        {
            "question": question,
            "source_files": [str(p) for p in source_files],
            "packet": normalize_packet(packet.get("parsed") or {}),
        },
    )
    verification_manifests.append(manifest_path)
# The LLM verifier is ADVISORY from here on. Its verdict is recorded but does
# not gate: across repeated runs it emitted a bare FAIL followed by "Verified
# clean", then "Verified sound", then bare "Findings" -- prose that cannot be
# parsed into a trustworthy machine verdict without loosening the contract to
# the point of rubber-stamping. The GATING pre-ingest check is the deterministic
# excerpt-grounding + provenance + manifest trio below, which mechanically
# proves every excerpt occurs in the cited snapshot. The verifier's full output
# is still captured for human review.
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
    write_json(
        run_root / "verification" / "advisory-verifier.json",
        {
            "verdict": advisory_verdict,
            "detail": advisory_detail,
        },
    )
except Exception as exc:  # advisory only: record and continue to deterministic gates
    write_json(
        run_root / "verification" / "advisory-verifier.json",
        {
            "verdict": None,
            "detail": f"verifier errored: {exc}",
        },
    )
    pre_text = ""

# Resolve the immutable source manifest and re-verify it against the actual
# bytes in this run. The manifest is the provenance root of trust, so a run
# that cannot produce a byte-exact manifest must fail closed rather than
# publish claims whose sources cannot be attested.
run_manifest = run_root / "source-manifest.jsonl"
manifest_path = run_manifest if run_manifest.exists() else CANONICAL_SOURCE_MANIFEST
if not manifest_path.exists():
    set_state(run_root, "FAILED", failure="missing source manifest")
    raise RuntimeError(f"no source manifest available (looked at {run_manifest} and {CANONICAL_SOURCE_MANIFEST})")
manifest_rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
by_name = {pathlib.Path(row["snapshot"]).name: row for row in manifest_rows}
for src in source_files:
    row = by_name.get(src.name)
    if row is None:
        set_state(run_root, "FAILED", failure="source manifest incomplete")
        raise RuntimeError(f"source manifest has no entry for {src.name}")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    if digest != row.get("sha256"):
        set_state(run_root, "FAILED", failure="source manifest sha256 mismatch")
        raise RuntimeError(f"sha256 mismatch for {src.name}: manifest={row.get('sha256')} actual={digest}")
if run_manifest != manifest_path:
    shutil.copy2(manifest_path, run_manifest)

# Copy immutable snapshots into the branch, then compile through the trusted host command.
set_state(run_root, "COMPILING")
snap_dest = field_root / "evidence" / "snapshots"
snap_dest.mkdir(parents=True, exist_ok=True)
for src in source_files:
    dest = snap_dest / src.name
    if dest.exists():
        dest.chmod(0o644)
    shutil.copy2(src, dest)
    dest.chmod(0o444)
subprocess.run(["openkb", "--kb-dir", str(field_root), "add", str(source_dir)], check=True, text=True)
subprocess.run(["openkb", "--kb-dir", str(field_root), "lint"], check=True, text=True)

# Deterministically materialize accepted claims BEFORE the post-compile
# verifier runs, so the verifier reviews the complete change set including the
# claims ledger it is meant to attest. Materializing afterwards would leave the
# ledger permanently unverified.
set_state(run_root, "MATERIALIZING_LEDGER")
# Dynamic mapping: every manifest source id maps to its summary note; every
# snapshot filename maps back to its source id. Templates come from the
# project spec ({n} = captured book number). The note must already exist in
# the field root (created by the ingest step).
NOTE_BY_SOURCE, SOURCE_BY_FILE = source_mappings(project, BOOKS)
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
            # Extend backwards-compatible with the multi-witness fields. The
            # Latin excerpt/locator above stay authoritative (source_ids is
            # exactly the canonical source). Witness fields are supplementary.
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
    # Only emit manifest rows for sources actually present in this run. With
    # the canonical fallback manifest (which may cover more snapshots than a
    # partial run's source_dir), emitting every row leaves provenance rows for
    # snapshots that were never copied, and provenance_validate fails closed.
    if pathlib.Path(row["snapshot"]).name not in present_names:
        continue
    row = dict(row)
    row["snapshot"] = "evidence/snapshots/" + pathlib.Path(row["snapshot"]).name
    manifest_out.append(json.dumps(row, ensure_ascii=False))
(field_root / "evidence" / "sources.jsonl").write_text("\n".join(manifest_out) + "\n", encoding="utf-8")

# Deterministic provenance gate: run before any agent judgement so a structural
# defect fails closed without spending a verifier step.
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

# Deterministic excerpt-grounding gate: mechanically prove every claim's
# excerpt occurs in the snapshot it cites, tolerating only HTML-entity,
# punctuation-folding and whitespace-reflow artifacts introduced by reading the
# sources through a terminal UI. This runs before the agent verifier so a
# fabricated or drifted quotation fails closed regardless of agent judgement.
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

# Deterministic translation-grounding gate (multi-witness projects): prove each
# claim's selected English witness is a real translation source whose quoted
# English occurs verbatim and overlaps the aligned Latin passage. Runs only for
# witness projects; a single-witness (Odyssey) run has no english_witness.
if IS_MULTI and any(c.get("english_witness") for c in claims):
    alignment_copy = run_root / "alignment.jsonl"
    if not alignment_copy.exists():
        proj_al = RESEARCH_ROOT / "corpora" / project["corpus_dir"] / project.get("alignment_path", "alignment.jsonl")
        if proj_al.exists():
            shutil.copy2(proj_al, alignment_copy)
    tgate = subprocess.run(
        [sys.executable, str(RESEARCH_ROOT / "bin" / "translation_grounding.py"),
         str(field_root), str(alignment_copy)],
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
# ``git add -N`` registers new files with the index without staging content, so
# ``git diff`` renders them as full additions. Without this the generated diff
# silently omits every newly created evidence file and the verifier would
# attest an empty change set.
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
# Post-compile LLM verifier is also advisory: the claims ledger it would review
# has already passed the deterministic excerpt-grounding, provenance and
# manifest-sha256 gates, which cover exactly the checks requested here (excerpt
# occurs in cited snapshot; source_ids exist in the source ledger). Record its
# output for human review without letting unparseable prose block publication.
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
    write_json(
        run_root / "verification" / "advisory-post-verifier.json",
        {
            "verdict": post_verdict,
            "detail": post_detail,
        },
    )
except Exception as exc:
    write_json(
        run_root / "verification" / "advisory-post-verifier.json",
        {
            "verdict": None,
            "detail": f"verifier errored: {exc}",
        },
    )
    post_text = ""

set_state(run_root, "READY_FOR_REVIEW")
# ``openkb add``/``lint`` also touch tracked KB files (wiki/log.md) and emit a
# lint report. Those are legitimate products of the compile step, so commit the
# whole working tree rather than evidence/ alone -- otherwise the run ends with
# a dirty worktree and the next run starts from unexplained local changes.
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
        "provenance": collect_provenance(),
    },
)
emit_output({"state": "READY_FOR_REVIEW", "claims": len(claims), "branch": branch, "commit": head})
