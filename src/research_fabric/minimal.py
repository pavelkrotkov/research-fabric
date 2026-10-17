"""Opt-in source-to-wiki compilation. Never merges or modifies the original KB.

Run ``python -m research_fabric.minimal --help``. The full engine stays separate.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from ._minimal_transport import check_budget, subscription, write_json
from ._openkb045 import POLICY, CompilationError, _native_modules, native_compile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(kb, *args):
    return subprocess.run(["git", "-C", str(kb), *args], check=True, capture_output=True, text=True).stdout.strip()


def tree(root):
    if root.is_symlink():
        raise CompilationError("Candidate root must not be a symlink")
    files = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise CompilationError("Minimal KB must be self-contained: symlinks are unsupported")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = digest(path)
    if list((root / ".openkb/journal").glob("*.json")):
        raise CompilationError("Pending native journal: recover it in its original KB first")
    return files


def inputs(source_dir, manifest):
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    names, hashes = set(), set()
    for row in rows:
        name = row.get("snapshot", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:html|md|txt)", name):
            raise CompilationError("Manifest needs self-contained HTML/Markdown/text snapshot basenames")
        if Path(name).stem.casefold() in names or row["sha256"] in hashes:
            raise CompilationError("Duplicate source slug or content hash")
        names.add(Path(name).stem.casefold())
        hashes.add(row["sha256"])
        source = source_dir / name
        if source.is_symlink() or not source.is_file() or digest(source) != row["sha256"]:
            raise CompilationError(f"Source hash mismatch: {name}")
        if source.suffix == ".md" and re.search(r"!\[.*?\]\(", source.read_text()):
            raise CompilationError("Markdown assets require the full pipeline; minimal supports text only")
    if not rows:
        raise CompilationError("Manifest is empty")
    return rows


def validate_links(kb):
    import yaml

    wiki = kb / "wiki"

    def exists(base, name):
        target = (base / name).resolve()
        return target.is_relative_to(wiki.resolve()) and target.is_file()

    for page in sorted(wiki.rglob("*.md")):
        if page.relative_to(wiki).parts[0] in ("sources", "assets", "reports") or page.name == "AGENTS.md":
            continue
        text = page.read_text()
        for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
            name = raw.split("|", 1)[0].split("#", 1)[0].strip()
            name = name if name.endswith(".md") else name + ".md"
            if not exists(wiki, name):
                raise CompilationError(f"Broken wiki link in {page.relative_to(wiki)}: {name}")
        for target in re.findall(r"!?\[[^\]]*\]\(([^\s)]+)\)", text):
            parsed = urlsplit(target.strip("<>"))
            if (
                not parsed.scheme
                and not parsed.netloc
                and parsed.path
                and not exists(page.parent, unquote(parsed.path))
            ):
                raise CompilationError(f"Broken local link in {page.relative_to(wiki)}")
        if text.startswith("---\n"):
            metadata = yaml.safe_load(text.split("---", 2)[1]) or {}
            for name in [*metadata.get("sources", []), *([metadata["full_text"]] if metadata.get("full_text") else [])]:
                if not isinstance(name, str) or not exists(wiki, name):
                    raise CompilationError(f"Broken source metadata in {page.relative_to(wiki)}")


def commit(kb, source):
    before = tree(kb)
    public = {name: value for name, value in before.items() if name.split("/")[0] in ("wiki", "raw")}
    git(kb, "add", "-A", "--", "wiki", "raw")
    tracked = set(git(kb, "ls-files", "-z").split("\0"))
    if public.keys() - tracked:
        raise CompilationError("Ignored or untracked publication output")
    # Markdown may intentionally use trailing spaces for a hard line break.
    # Preserve compiler bytes while retaining the other Git diff checks.
    git(kb, "-c", "core.whitespace=-blank-at-eol", "diff", "--cached", "--check")
    git(kb, "commit", "-m", f"Minimal source checkpoint: {source}")
    head = git(kb, "rev-parse", "HEAD")
    if tree(kb) != before or git(kb, "status", "--porcelain", "--untracked-files=all"):
        raise CompilationError("Candidate changed during publication")
    for name, expected in public.items():
        blob = subprocess.check_output(["git", "-C", str(kb), "cat-file", "blob", f"{head}:{name}"])
        if hashlib.sha256(blob).hexdigest() != expected:
            raise CompilationError("Commit blob differs from accepted publication bytes")
    return head, before


def _restore(run, state):
    if not re.fullmatch(r"attempt-[0-9]{4,}", state["pending"]):
        raise CompilationError("Invalid rollback checkpoint")
    attempt = run / state["pending"]
    baseline = attempt / "baseline"
    if tree(baseline) != state["outputs"] or git(baseline, "rev-parse", "HEAD") != state["commit"]:
        raise CompilationError("Rollback baseline was altered")
    candidate = run / "candidate"
    if candidate.exists():
        quarantine = attempt / "failed-candidate"
        index = 0
        while quarantine.exists():
            index += 1
            quarantine = attempt / f"interrupted-restore-{index}"
        candidate.rename(quarantine)
    shutil.copytree(baseline, run / "candidate")
    state.pop("pending")
    state["state"] = "FAILED"
    write_json(run / "run.json", state)


def _verify_checkpoint(run, state):
    kb = run / "candidate"
    if tree(kb) != state["outputs"] or git(kb, "rev-parse", "HEAD") != state["commit"]:
        raise CompilationError("Candidate drift: do not resume an edited KB")
    for index, checkpoint in enumerate(state["completed"]):
        path = run / checkpoint["receipt"]
        if digest(path) != checkpoint["sha256"]:
            raise CompilationError("Source receipt was altered")
        receipt = json.loads(path.read_text())
        if receipt["source"] != state["identity"]["sources"][index] or receipt["policy"] != POLICY:
            raise CompilationError("Source receipt does not match ordered inputs")


def run(kb, source_dir, manifest, run_root, *, resume=False, settings=None, command=None):
    """One private candidate, one strict native transaction/checkpoint per source.

    ``command`` is an internal test seam; the CLI always uses the real worker.
    No model retry is automatic. Failed candidates and native state are retained.
    """
    kb, source_dir, manifest, run_root = (Path(p).resolve() for p in (kb, source_dir, manifest, run_root))
    if run_root.is_relative_to(kb) or run_root.is_relative_to(source_dir) or kb.is_relative_to(run_root):
        raise CompilationError("Run directory must be separate from original KB and sources")
    settings = dict(settings or {})
    if settings.get("model") != "gpt-6-luna" or settings.get("effort") != "xhigh":
        raise CompilationError("Minimal subscription route is qualified only for GPT-6 Luna xhigh")
    if any(
        type(settings.get(k)) is not int or settings[k] <= 0
        for k in ("max_requests", "max_tokens", "timeout", "output_tokens")
    ):
        raise CompilationError("Explicit positive request, token, time and output limits required")
    sources = inputs(source_dir, manifest)
    identity = {
        "source_dir": str(source_dir),
        "manifest": digest(manifest),
        "sources": sources,
        "settings": settings,
        "base_kb": str(kb),
        "code": {
            name: digest(Path(__file__).with_name(name))
            for name in ("minimal.py", "_minimal_transport.py", "_openkb045.py")
        },
    }
    if not resume:
        if git(kb, "rev-parse", "--show-toplevel") != str(kb) or git(
            kb, "status", "--porcelain", "--untracked-files=all"
        ):
            raise CompilationError("Original KB must be a clean repository root")
        baseline = tree(kb)
        if {name for name in baseline if name.split("/")[0] in ("wiki", "raw")} - set(
            git(kb, "ls-files", "-z").split("\0")
        ):
            raise CompilationError("Original KB has ignored publication outputs")
        run_root.mkdir(parents=True, exist_ok=False)
        run_root.chmod(0o700)
    elif not run_root.is_dir():
        raise CompilationError("Resume requires an existing run")
    with (run_root / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CompilationError("A parent or compiler child still owns this run") from None
        candidate = run_root / "candidate"
        if not resume:
            subprocess.run(
                ["git", "clone", "--no-hardlinks", "--no-checkout", "--", str(kb), str(candidate)],
                check=True,
                capture_output=True,
            )
            base = git(kb, "rev-parse", "HEAD")
            git(candidate, "checkout", "-B", "codex/minimal", base)
            shutil.copytree(kb, candidate, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
            if tree(candidate) != baseline:
                raise CompilationError("Original KB changed during isolation")
            # Local review only: no inherited push target or credentials.
            git(candidate, "remote", "remove", "origin")
            git(candidate, "config", "user.name", "Research Fabric")
            git(candidate, "config", "user.email", "research-fabric@example.invalid")
            state = {
                "state": "PREPARED",
                "mode": "minimal",
                "verified_claims": False,
                "identity": identity,
                "commit": base,
                "outputs": baseline,
                "completed": [],
                "attempts": 0,
            }
            write_json(run_root / "run.json", state)
            write_json(run_root / "budget.json", {"requests": 0, "tokens": 0, "observed_tokens": 0})
        else:
            state = json.loads((run_root / "run.json").read_text())
            if state["identity"] != identity:
                raise CompilationError("Sources, manifest, settings or code changed; cannot resume")
            if state.get("pending"):
                _restore(run_root, state)
        _verify_checkpoint(run_root, state)
        check_budget(run_root)
        for row in sources[len(state["completed"]) :]:
            _verify_checkpoint(run_root, state)
            if inputs(source_dir, manifest) != sources or digest(manifest) != identity["manifest"]:
                raise CompilationError("Immutable inputs changed")
            # A crash while copying the baseline may leave an orphan attempt
            # before run.json was advanced. Never overwrite that diagnostic.
            allocated = [int(p.name[8:]) for p in run_root.glob("attempt-*") if p.name[8:].isdigit()]
            state["attempts"] = max([state["attempts"], *allocated]) + 1
            attempt = run_root / f"attempt-{state['attempts']:04}"
            attempt.mkdir()
            shutil.copytree(candidate, attempt / "baseline")
            if tree(attempt / "baseline") != state["outputs"]:
                raise CompilationError("Candidate changed while preparing its rollback baseline")
            staged = attempt / row["snapshot"]
            shutil.copy2(source_dir / row["snapshot"], staged)
            if digest(staged) != row["sha256"]:
                raise CompilationError("Source changed during staging")
            state.update(state="COMPILING", pending=attempt.name)
            write_json(run_root / "run.json", state)
            try:
                env = {
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                    "TMPDIR": str(attempt),
                    "XDG_CACHE_HOME": str(run_root / "cache"),
                }
                argv = [
                    *(command or [sys.executable, "-m", "research_fabric.minimal"]),
                    "--worker",
                    str(run_root),
                    str(staged),
                    str(attempt / "result.json"),
                ]
                with (attempt / "child.log").open("w") as log:
                    result = subprocess.run(
                        argv,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=settings["timeout"],
                        pass_fds=(lock.fileno(),),
                    )
                report = json.loads((attempt / "result.json").read_text())
                if result.returncode or report.get("state") != "COMPLETE" or report.get("policy") != POLICY:
                    raise CompilationError("Native source compilation did not complete")
                if report.get("versions") != {"openkb": "0.4.5", "litellm": "1.87.2"} or report["outputs"] != tree(
                    candidate
                ):
                    raise CompilationError("Toolchain or output attestation mismatch")
                if len(report["sources"]) != 1 or report["sources"][0]["sha256"] != row["sha256"]:
                    raise CompilationError("Native receipt does not match input")
                outcome = report["sources"][0]
                if (
                    not re.fullmatch(r"[A-Za-z0-9_.-]+", outcome["summary"])
                    or not (candidate / "wiki/summaries" / (outcome["summary"] + ".md")).is_file()
                ):
                    raise CompilationError("Required summary absent")
                for folder, action, slug in outcome["required"]:
                    if folder not in ("concepts", "entities") or action not in ("create", "update", "related"):
                        raise CompilationError("Unknown required native operation")
                    target = candidate / "wiki" / folder / (slug + ".md")
                    if not target.resolve().is_relative_to(candidate / "wiki") or not target.is_file():
                        raise CompilationError("Required native output missing")
                check_budget(run_root)
                if inputs(source_dir, manifest) != sources or digest(manifest) != identity["manifest"]:
                    raise CompilationError("Source drift during compilation")
                validate_links(candidate)
                head, outputs = commit(candidate, row["snapshot"])
                if outputs != report["outputs"]:
                    raise CompilationError("Publication differs from native attested output")
                receipt = {
                    "source": row,
                    "policy": POLICY,
                    "parent_commit": state["commit"],
                    "commit": head,
                    "outputs": outputs,
                    "native": report,
                    "identity": identity,
                }
                write_json(attempt / "receipt.json", receipt)
                state["completed"].append(
                    {"receipt": f"{attempt.name}/receipt.json", "sha256": digest(attempt / "receipt.json")}
                )
                state.update(state="CHECKPOINTED", commit=head, outputs=outputs)
                state.pop("pending")
                write_json(run_root / "run.json", state)
            except BaseException as exc:
                # Retain even a partially applied commit; restore the private KB,
                # including ignored native state, from its verified prior checkpoint.
                state["failure"] = {"type": type(exc).__name__, "attempt": attempt.name}
                if state.get("pending"):
                    _restore(run_root, state)
                raise
        _verify_checkpoint(run_root, state)
        state.pop("failure", None)
        state["state"] = "READY_FOR_REVIEW"
        write_json(run_root / "run.json", state)
        return state


def worker(run_root, source, result):
    run_root, source, result = map(Path, (run_root, source, result))
    settings = json.loads((run_root / "run.json").read_text())["identity"]["settings"]
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), ExitStack() as stack:
            cli, compiler, _ = _native_modules()
            import openkb.config as config

            config.GLOBAL_CONFIG_DIR = run_root / "native-global"
            config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
            config.GLOBAL_CONFIG_LOCK_PATH = config.GLOBAL_CONFIG_DIR / "global.lock"
            stack.enter_context(subscription(run_root, settings))
            sync, asynchronous = compiler.litellm.completion, compiler.litellm.acompletion

            def arguments(kwargs):
                kwargs.update(
                    model="chatgpt/responses/" + settings["model"],
                    reasoning_effort=settings["effort"],
                    timeout=min(settings["timeout"], 300),
                    num_retries=0,
                    max_retries=0,
                    allowed_openai_params=["reasoning_effort"],
                )
                kwargs.pop("temperature", None)
                return kwargs

            async def call_async(*args, **kwargs):
                return await asynchronous(*args, **arguments(kwargs))

            stack.enter_context(
                patch.object(compiler.litellm, "completion", lambda *a, **kw: sync(*a, **arguments(kw)))
            )
            stack.enter_context(patch.object(compiler.litellm, "acompletion", call_async))
            stack.enter_context(patch.object(compiler, "_accepts_cache_control", lambda _: False))
            stack.enter_context(patch.object(cli, "_setup_llm_key", lambda _: None))
            original_config = cli.load_config
            stack.enter_context(
                patch.object(
                    cli,
                    "load_config",
                    lambda kb: {**original_config(kb), "model": "chatgpt/" + settings["model"], "concurrency": 1},
                )
            )
            report = native_compile(run_root / "candidate", [source])
        log = run_root / "candidate/wiki/log.md"
        if log.exists():
            log.write_text(log.read_text().rstrip("\n") + "\n")
        write_json(result, {**report, "state": "COMPLETE", "outputs": tree(run_root / "candidate")})
        return 0
    except Exception as exc:
        write_json(result, {"state": "FAILED", "reason": type(exc).__name__})
        return 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        return worker(*sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kb", "source-dir", "manifest", "run-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-requests", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=1800, help="Per-source child deadline in seconds")
    args = parser.parse_args()
    settings = {
        "model": "gpt-6-luna",
        "effort": "xhigh",
        "max_requests": args.max_requests,
        "max_tokens": args.max_tokens,
        "timeout": args.timeout,
        "output_tokens": 32000,
        "auth_file": str(Path.home() / ".codex/auth.json"),
    }
    try:
        result = run(args.kb, args.source_dir, args.manifest, args.run_root, resume=args.resume, settings=settings)
        print(
            json.dumps(
                {"state": result["state"], "commit": result["commit"], "candidate": str(args.run_root / "candidate")}
            )
        )
        return 0
    except (OSError, ValueError, KeyError, CompilationError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, CompilationError) else type(exc).__name__
        print(f"Minimal run refused/failed: {reason}; inspect private run diagnostics", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
