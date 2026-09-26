"""Explicit research runs; CAO supplies only planning/advisory calls and reporting."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from research_fabric._source_assets import publish_bundle
from research_fabric.claims import ClaimIdentityError, accept_packet, atomic_write_json
from research_fabric.compilation import assert_run_branch, compile_with_recovery, normalize_generated_log, write_json
from research_fabric.compilation import digest as sha256_of
from research_fabric.core import (
    book_task_from_project,
    extract_json,
    multisource_packet_defects,
    normalize_packet,
    packet_defects,
    read_verdict,
    source_mappings,
)
from research_fabric.core import (
    load_project as load_project_spec,
)
from research_fabric.execution import configure, configured, history, packet_policy_defects, resolve
from research_fabric.publication import publish_candidate
from research_fabric.review import advisory_record
from research_fabric.run_state import record_ready, run_lifecycle, set_state
from research_fabric.sources import (
    ADAPTERS,
    bind_manifest,
    discover_sources,
    packet_source_defects,
    prepare_source_bundle,
    representation_for,
    source_attestation,
    source_provenance,
)


@dataclass(frozen=True)
class RunConfig:
    engine_root: pathlib.Path
    project_path: pathlib.Path
    field_root: pathlib.Path
    run_root: pathlib.Path
    source_dir: pathlib.Path
    question: str
    reuse_evidence_dir: pathlib.Path | None = None
    execution: dict = field(default_factory=dict)
    visual_preflight_spec: pathlib.Path | None = None
    visual_python: str = sys.executable
    worker_python: str = sys.executable
    compiler_command: tuple[str, ...] | None = None
    openkb_command: tuple[str, ...] = ("openkb",)


def _command_output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        return result.splitlines()[0] if result else None
    except (OSError, subprocess.SubprocessError):
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


def _reuse_evidence_packets(
    reuse_dir,
    destination_dir,
    specs,
    worker_provenance,
    validator,
    source_dir,
    adapters=ADAPTERS,
    policy=lambda packet, sid: [],
):
    reused_sids = []
    for sid, _ in specs:
        src_packet = reuse_dir / f"worker-{sid}.json"
        if not src_packet.exists():
            continue
        try:
            src_data = json.loads(src_packet.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        defects = packet_source_defects(src_data, worker_provenance[sid], validator) + policy(src_data, sid)
        if defects:
            continue
        dst = destination_dir / src_packet.name
        try:
            accepted = accept_packet(src_data, sid, source_dir=source_dir, adapters=adapters)
        except ClaimIdentityError as exc:
            raise RuntimeError(f"reused packet {sid} failed claim identity validation: {exc}") from exc
        atomic_write_json(dst, accepted)
        reused_sids.append(sid)
    return reused_sids


VERDICT_CONTRACT = (
    "Work through the evidence first and write your findings. Then, as the very LAST line of your "
    "reply, emit the verdict in exactly one of these forms:\n"
    "  VERDICT: PASS\n"
    "  VERDICT: FAIL - <specific defect>; <specific defect>\n"
    "The verdict must come last, after your analysis, so it reflects what you actually found. "
    "A FAIL must enumerate at least one concrete defect (claim id and what is wrong with it). "
    "Do not write the words PASS or FAIL anywhere except that final verdict line. Do not modify files."
)


class ResearchRun:
    """One explicit invocation, with existing source, execution and publication owners."""

    def __init__(self, config, agent_step, *, agent_provider=None):
        self.config, self.agent_step = config, agent_step
        self.agent_provider = agent_provider
        self.reading_plan = None
        self.derived_contexts = None
        self.packet_dir = config.run_root / "evidence"
        self.compiler_dir = config.run_root / "compiler-sources"

    def execute(self):
        if self.config.run_root.resolve().is_relative_to(self.config.field_root.resolve()):
            raise RuntimeError("run artifacts must live outside the KB worktree")
        with run_lifecycle(self.config.run_root):
            self._prepare()
            self._sources()
            self.plan()
            set_state(self.config.run_root, "RESEARCHING")
            self._collect()
            set_state(self.config.run_root, "VERIFYING_SOURCES")
            self._verify_sources()
            set_state(self.config.run_root, "COMPILING")
            self._compile()
            set_state(self.config.run_root, "MATERIALIZING_LEDGER")
            return self._publish()

    def _prepare(self):
        assert_run_branch(self.config.field_root)
        if subprocess.run(
            ["git", "-C", str(self.config.field_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip():
            raise RuntimeError("run worktree must start clean; preserve user changes and use a fresh isolated branch")
        self.packet_dir.mkdir(parents=True, exist_ok=True)
        self.project = load_project_spec(self.config.project_path.parent, self.config.project_path.stem)
        self.acceptance = self.project.get("acceptance") or {}
        from openkb.config import load_config

        native_config = load_config(self.config.field_root / ".openkb/config.yaml")
        previous_execution = (
            configured(self.config.run_root)[1] if (self.config.run_root / "execution.sqlite3").exists() else None
        )
        execution = resolve(
            self.project, self.config.execution, native_model=native_config["model"], previous=previous_execution
        )
        configure(self.config.run_root, execution)
        self.canonical_manifest = (
            self.config.engine_root / "corpora" / self.project["corpus_dir"] / self.project["manifest_path"]
        ).resolve()
        self.book_re = re.compile(self.project.get("snapshot_pattern", ""))
        self.witnesses = list((self.project.get("witnesses") or {}).keys())
        self.canonical = self.project.get("canonical_variant", "latin")
        self.is_multi = bool(self.witnesses)

    def _validator(self, parsed, acceptance=None):
        if self.is_multi:
            return multisource_packet_defects(parsed, self.project, acceptance)
        return packet_defects(parsed, acceptance)

    def _reading_sources(self):
        from .reading import prepare_reading_plan

        self._bind_manifest(reading=True, bind=False)
        self.reading_plan, self.bundles = prepare_reading_plan(
            self.config.source_dir,
            self.project,
            self.manifest_rows,
            self.config.run_root / "reading-plan.json",
            self.compiler_dir,
            question=self.config.question,
        )
        data = self.reading_plan.data
        from .compile_units import prepare_units

        self.compile_plan = prepare_units(
            self.reading_plan, self.bundles, self.project, self.config.run_root, self.config.field_root
        )
        self.manifest_rows = list(data["sources"].values())
        self.source_files = [self.config.source_dir / name for name in data["sources"]]
        self.worker_specs = [(row["id"], self.config.question) for row in data["readings"]]
        self.worker_sources = {
            row["id"]: [self.config.source_dir / name for name in self.reading_plan.source_names(row["id"])]
            for row in data["readings"]
        }
        self.worker_provenance = {
            sid: source_provenance(paths, source_root=self.config.source_dir)
            for sid, paths in self.worker_sources.items()
        }

    def _sources(self):
        if "reading" in self.project:
            self._reading_sources()
            return
        self.source_files = discover_sources(self.config.source_dir, ADAPTERS)
        if not self.source_files:
            raise RuntimeError("no supported source snapshots found in source_dir")
        matched = [(path, self.book_re.search(path.name)) for path in self.source_files]
        if any(match is None for _, match in matched):
            raise RuntimeError(f"all snapshots must match {self.book_re.pattern}")
        if self.is_multi:
            self.books = sorted(int(match.group(1)) for _, match in matched if match.groups()[1:2] == (self.canonical,))
            self.worker_sources = {f"book-{book}": self._witness_sources(book) for book in self.books}
        else:
            self.worker_sources = {f"book-{int(match.group(1))}": [path] for path, match in matched}
            if len(self.worker_sources) != len(self.source_files):
                raise RuntimeError(f"all snapshots must match {self.book_re.pattern}")
            self.books = sorted(int(match.group(1)) for _, match in matched)
        self.worker_specs = [
            (f"book-{b}", book_task_from_project(b, self.project, self.config.project_path.stem)) for b in self.books
        ]
        self.worker_provenance = {sid: source_provenance(paths, ADAPTERS) for sid, paths in self.worker_sources.items()}

    def _witness_sources(self, book):
        paths = [
            self.config.source_dir / self.project["book_label_template"].format(n=book, w=variant)
            for variant in [self.canonical, *self.witnesses]
        ]
        missing = [path.name for path in paths if not path.is_file()]
        if missing:
            raise RuntimeError(f"worker source input missing: {missing}")
        return paths

    def _packet_defects(self, packet: dict, sid: str) -> list[str]:
        return packet_source_defects(
            packet, self.worker_provenance[sid], lambda parsed: self._validator(parsed, self.acceptance)
        ) + self._packet_policy(packet, sid)

    def _packet_policy(self, packet, sid):
        if self.reading_plan:
            self.reading_plan.validate_packet(packet, sid, self.acceptance)
        if self.derived_contexts is not None:
            from .derived_context import packet_defects

            defects = packet_defects(packet, self.derived_contexts[sid])
            if defects:
                return defects
        return packet_policy_defects(packet, self.project)

    def _reading_visual_context(self):
        from .derived_context import prepare_contexts

        spec = json.loads(self.config.visual_preflight_spec.read_text()) if self.config.visual_preflight_spec else None

        def inspect(single, key):
            directory = self.config.run_root / "verification" / "visual"
            spec_path, record_path = directory / f"{key}-spec.json", directory / f"{key}.json"
            if not spec_path.exists():
                atomic_write_json(spec_path, single)
            elif json.loads(spec_path.read_text()) != single:
                raise ValueError("immutable visual specification drift")
            subprocess.run(
                [
                    self.config.visual_python,
                    str(self.config.engine_root / "bin/visual_preflight.py"),
                    "--source-root",
                    str(self.config.source_dir),
                    "--spec",
                    str(spec_path),
                    "--output",
                    str(record_path),
                ],
                check=True,
                text=True,
            )
            return json.loads(record_path.read_text())

        self.derived_contexts = prepare_contexts(
            self.reading_plan,
            self.bundles,
            self.config.run_root,
            spec,
            inspect,
            self.project["reading"].get("visual_context"),
        )

    def plan(self):
        if self.reading_plan:
            self._reading_visual_context()
            return  # Source assignment stays frozen; derived context is separate.
        visual_preparation = ""
        if self.config.visual_preflight_spec:
            from research_fabric.visual_preflight import preparation_note

            visual_record = self.config.run_root / "verification" / "visual-inspection.json"
            subprocess.run(
                [
                    self.config.visual_python,
                    str(self.config.engine_root / "bin" / "visual_preflight.py"),
                    "--source-root",
                    str(self.config.source_dir),
                    "--spec",
                    str(self.config.visual_preflight_spec),
                    "--output",
                    str(visual_record),
                ],
                check=True,
                text=True,
            )
            visual_preparation = preparation_note(json.loads(visual_record.read_text()))

        if self.config.reuse_evidence_dir:
            write_json(
                self.config.run_root / "plan.json", {"reused": True, "source": str(self.config.reuse_evidence_dir)}
            )
        else:
            plan = self.agent_step(
                agent="research-supervisor",
                prompt=(
                    f"{visual_preparation}\nQuestion: {self.config.question}\n"
                    f"Source snapshots: {', '.join(map(str, self.source_files))}\n"
                    "Return JSON only with keys research_questions, worker_assignments, allowed_source_types, "
                    "source_budget, acceptance_criteria, ambiguities, stop_conditions. Keep assignments bounded "
                    "and non-overlapping. Do not modify files."
                ),
                step_id="plan",
                timeout=300,
            )
            write_json(self.config.run_root / "plan.json", {"raw": plan, "parsed": extract_json(plan)})

    def _worker_command(self, sid, task):
        if self.reading_plan:
            return [
                self.config.worker_python,
                str(self.config.engine_root / "bin/direct_worker.py"),
                str(self.config.run_root),
                str(self.config.source_dir),
                "--reading",
                sid,
                str(self.config.project_path),
                "--derived-context",
                str(self.config.run_root / "derived-context" / f"{sid}.json"),
            ]
        b = int(sid.split("-")[1])
        theme_match = re.search("Extract claim-level evidence about (.+?)\\\\. Use", task)
        theme = theme_match.group(1) if theme_match else None
        book_label = self.worker_sources[sid][0].name

        worker = "aeneid_worker.py" if self.is_multi else "direct_worker.py"
        cmd = [
            self.config.worker_python,
            str(self.config.engine_root / "bin" / worker),
            str(self.config.run_root),
            str(self.config.source_dir),
            str(b),
            book_label,
            theme or "",
        ]
        for w in self.witnesses:
            wfile = self.project["book_label_template"].format(n=b, w=w)
            wid = self.project["source_id_template"].format(n=b, w=w)
            cmd += ["--witness", f"{w}:{wid}:{wfile}"]
        return cmd

    def _collect_one(self, spec):
        """Run a worker and persist either accepted evidence or failure diagnostics."""
        sid, task = spec
        try:
            proc = subprocess.run(
                self._worker_command(sid, task),
                capture_output=True,
                text=True,
                timeout=2400 if self.is_multi else 1500,
            )
            packet = self.packet_dir / f"worker-{sid}.json"
            if proc.returncode != 0 or not packet.exists():
                raise RuntimeError(
                    f"worker failed (rc={proc.returncode}): {proc.stderr.strip()[-400:] or proc.stdout.strip()[-400:]}"
                )
            self._accept_worker_packet(packet, sid)
            return (sid, proc.stdout.strip(), None)
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            result = {"error": str(exc)[:400]}
        except Exception as exc:
            result = {"error": f"unexpected: {exc}"}
        write_json(self.packet_dir / f"worker-{sid}.json", {"worker": sid, "attempts": [result], "parsed": None})
        reason = result["error"]
        return (sid, "", f"direct worker returned no valid evidence packet ({reason})")

    def _accept_worker_packet(self, packet, sid):
        packet_data = json.loads(packet.read_text(encoding="utf-8"))
        defects = self._packet_defects(packet_data, sid)
        if defects:
            raise RuntimeError("; ".join(defects))
        accepted = accept_packet(
            packet_data,
            sid,
            source_dir=self.config.source_dir,
            attempt_id=f"{sid}:worker",
            adapters=ADAPTERS,
        )
        atomic_write_json(packet, accepted)

    def _collect(self):
        if self.config.reuse_evidence_dir:
            reused_sids = _reuse_evidence_packets(
                self.config.reuse_evidence_dir,
                self.packet_dir,
                self.worker_specs,
                self.worker_provenance,
                lambda parsed: self._validator(parsed, self.acceptance),
                self.config.source_dir,
                ADAPTERS,
                self._packet_policy,
            )
            pending_specs = [(sid, task) for sid, task in self.worker_specs if sid not in reused_sids]
            self.results = [(sid, "reused", None) for sid in reused_sids]
        else:
            pending_specs = self.worker_specs
            self.results = []
        self.results.extend(map(self._collect_one, pending_specs))
        if any((err for _, _, err in self.results)):
            raise RuntimeError("one or more evidence workers failed")

    def run_verifier(self, step_id, prompt, artifact):
        """Run a verifier step, retrying once when the reply is not a usable verdict."""
        last_text = ""
        for attempt in (1, 2):
            handle = self.agent_step(
                agent="research-verifier", prompt=prompt, step_id=f"{step_id}-attempt-{attempt}", timeout=900
            )
            last_text = handle
            (self.config.run_root / "verification" / artifact).write_text(last_text, encoding="utf-8")
            verdict, detail = read_verdict(last_text)
            if verdict is not None:
                return (verdict, detail, last_text)
            (self.config.run_root / "verification" / f"{artifact}.malformed-attempt-{attempt}.txt").write_text(
                last_text, encoding="utf-8"
            )
        return (None, "verifier gave no usable verdict after two attempts", last_text)

    def _advisory(self, step_id, prompt, artifact, report):
        try:
            verdict, detail, _ = self.run_verifier(step_id, prompt, artifact)
        except Exception as exc:
            verdict, detail = None, f"verifier errored: {exc}"
        write_json(self.config.run_root / "verification" / report, {"verdict": verdict, "detail": detail})

    def _verification_manifests(self):
        verification_dir = self.config.run_root / "verification"
        verification_dir.mkdir(exist_ok=True)
        verification_manifests = []
        for packet_path in sorted(self.packet_dir.glob("worker-*.json")):
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            verification_path = verification_dir / f"verification-input-{packet_path.stem}.json"
            write_json(
                verification_path,
                {
                    "question": self.config.question,
                    "source_files": [str(p) for p in self.source_files],
                    "derived_context": packet.get("derived_context"),
                    "packet": packet.get("parsed")
                    if self.reading_plan
                    else normalize_packet(packet.get("parsed") or {}),
                },
            )
            verification_manifests.append(verification_path)
        return verification_manifests

    def _verify_sources(self):
        verification_manifests = self._verification_manifests()
        self._advisory(
            "verify-sources",
            f"Read these exact verification manifests: {', '.join(map(str, verification_manifests))}. "
            "Each contains one complete evidence packet and the source-file paths. Read the named source files "
            "from the manifests to verify exact excerpts. Acceptance criteria: every substantive claim must have "
            "an exact excerpt and locator; check contradictions, independence, wording strength, and coverage. "
            "Do not use directory listings or require a generated diff.\n\n" + VERDICT_CONTRACT,
            "pre-ingest.txt",
            "advisory-verifier.json",
        )
        self._bind_manifest(reading=bool(self.reading_plan))

    def _bind_manifest(self, reading=False, bind=True):
        run_manifest = self.config.run_root / "source-manifest.jsonl"
        self.manifest_path = run_manifest if run_manifest.exists() else self.canonical_manifest
        if not self.manifest_path.exists():
            raise RuntimeError(f"no source manifest available (looked at {run_manifest} and {self.canonical_manifest})")
        self.manifest_rows = [
            json.loads(line) for line in self.manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if not bind:
            return
        if reading:
            from ._source_assets import safe_path

            paths = [safe_path(self.config.source_dir, name) for name in self.project["reading"]["sources"]]
            self.manifest_rows = bind_manifest(paths, self.manifest_rows, source_root=self.config.source_dir)
        else:
            self.manifest_rows = bind_manifest(self.source_files, self.manifest_rows, ADAPTERS)
        run_manifest.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in self.manifest_rows) + "\n", encoding="utf-8"
        )

    def _compile(self):
        self.compiler_dir.mkdir(exist_ok=True)
        if self.reading_plan:
            from .compile_units import compile_units

            self.compile_report = compile_units(
                self.config.field_root, self.config.run_root, self.compile_plan, command=self.config.compiler_command
            )
            self._lint_compiled()
            return
        self.bundles = {}
        compiler_inputs = []
        for src in self.source_files:
            rep = representation_for(src)
            attestation = source_attestation(rep)
            bundle = prepare_source_bundle(src, self.compiler_dir, attestation)
            self.bundles[attestation["source_file"]] = bundle
            compiler_inputs.append(self.compiler_dir / bundle["key"] / bundle["input_path"])
        self.compile_report = compile_with_recovery(
            self.config.field_root,
            compiler_inputs,
            self.config.run_root / "verification" / "compile",
            execution_root=self.config.run_root,
            command=self.config.compiler_command,
        )
        for bundle in self.bundles.values():
            if bundle["source"]["adapter"] == "markdown":
                publish_bundle(
                    self.compiler_dir / bundle["key"],
                    bundle,
                    self.config.field_root / "wiki",
                    pathlib.Path(bundle["input_path"]).stem,
                )
        self._lint_compiled()

    def _lint_compiled(self):
        subprocess.run(
            [*self.config.openkb_command, "--kb-dir", str(self.config.field_root), "lint"], check=True, text=True
        )
        normalize_generated_log(self.config.field_root)

    def _publication_context(self):
        if self.reading_plan:
            return {}, {name: row["source_id"] for name, row in self.reading_plan.data["sources"].items()}
        return source_mappings(self.project, self.books)

    def _alignment(self):
        if not self.is_multi:
            return None
        path = self.config.run_root / "alignment.jsonl"
        original = (
            self.config.engine_root
            / "corpora"
            / self.project["corpus_dir"]
            / self.project.get("alignment_path", "alignment.jsonl")
        )
        if not path.exists() and original.exists():
            shutil.copy2(original, path)
        return path if path.is_file() else None

    def _advisory_review(self, review):
        verification = self.config.run_root / "verification"
        prompt = (
            f"Question: {self.config.question}\n"
            f"Read the exact candidate diff {verification / 'generated-diff.patch'} and review record "
            f"{verification / 'compiled-wiki-review.json'}. Follow changed wiki links and the source-linked "
            "original-section citations. If present, read evidence/coverage.json and evidence/unit-outcomes.json "
            f"under {self.config.field_root} for original Work/unit/operation mappings and per-unit retention diffs. "
            "Compare retained before-state from the git diff with current pages and "
            "source evidence. Look for unsupported interpretations, missing nuance, semantic omissions, duplicate "
            "concepts, and lost qualifications; a detail moved to a linked page is not automatically lost. Start "
            "with 'Scope:'; use 'Before:', 'After:' and 'Source:' when relevant; include 'Uncertainty:'. "
            "Do not modify files. This opinion is advisory and cannot override mechanical failures.\n\n"
            + VERDICT_CONTRACT
        )
        try:
            verdict, detail, reply = self.run_verifier("verify-diff", prompt, "post-compile.txt")
            error = detail if verdict is None else None
        except Exception as exc:
            verdict, detail, reply, error = None, str(exc), "", f"verifier errored: {exc}"
        record = advisory_record(
            reply,
            {"candidate_sha256": review["candidate_sha256"], "changed": review["changed"]},
            error=error,
        )
        record.update(verdict=verdict, detail=detail)
        return record

    def _commit_message(self, claims):
        default = (
            "{project} evidence run ({claims} claims; run {run})"
            if self.reading_plan
            else "{project} evidence run ({claims} claims, Books {books}; run {run})"
        )
        return (self.project.get("commit_message") or default).format(
            project=self.config.project_path.stem.capitalize(),
            claims=claims,
            books="" if self.reading_plan else "-".join(map(str, (self.books[0], self.books[-1]))),
            run=self.config.run_root.name,
        )

    def _publish(self):
        notes, sources = self._publication_context()
        provenance = self.collect_provenance()
        outcome = publish_candidate(
            field_root=self.config.field_root,
            run_root=self.config.run_root,
            engine_root=self.config.engine_root,
            source_dir=self.config.source_dir,
            source_files=self.source_files,
            manifest_rows=self.manifest_rows,
            worker_ids=[sid for sid, _, _ in self.results],
            notes=notes,
            sources=sources,
            compiled=self.compile_report,
            message=self._commit_message,
            multi=self.is_multi,
            reading_plan=self.reading_plan,
            acceptance=self.acceptance,
            alignment=self._alignment(),
            advisory=self._advisory_review,
            before_review=lambda: set_state(self.config.run_root, "VERIFYING_DIFF"),
        )
        self.claims, self.review = outcome["claims"], outcome["review"]
        self.review["commit"] = outcome["commit"]
        write_json(self.config.run_root / "verification" / "compiled-wiki-review.json", self.review)
        return record_ready(self.config.run_root, len(self.claims), provenance, outcome)

    def collect_provenance(self) -> dict:
        """Record the exact toolchain + inputs that produced this run."""

        proj_path = self.config.project_path
        corpus_manifest = self.manifest_path
        return {
            "execution": history(self.config.run_root),
            "advisory_execution": {
                "provider": self.agent_provider,
                "actual_model": None,
                "usage": None,
                "reason": "External advisory calls do not expose effective model or usage at this seam",
            },
            "native_lint_execution": {"actual_model": None, "usage": None, "budgeted": False},
            "engine_sha": _command_output(["git", "-C", str(self.config.engine_root), "rev-parse", "HEAD"]),
            "engine_tag": _command_output(
                ["git", "-C", str(self.config.engine_root), "describe", "--tags", "--exact-match"]
            ),
            "project": self.config.project_path.stem,
            "project_spec_sha": sha256_of(proj_path) if proj_path.exists() else None,
            "corpus_manifest_sha": sha256_of(corpus_manifest) if corpus_manifest.exists() else None,
            "corpus_sources_sha": _dir_sha(self.config.source_dir),
            "source_adapters": sorted({f"{row['adapter']}@{row['adapter_version']}" for row in self.manifest_rows}),
            "openkb": _command_output([*self.config.openkb_command, "--version"]),
            "toolchain": {
                "python": _command_output([sys.executable, "--version"]),
            },
        }
