"""Qualified OpenKB 0.4.5 instrumentation; run only in an isolated child.

This adapter observes native actions, not prompts or provider request counts.
The native planner filters malformed entries and nonexistent related pages;
only its final local lists describe what this invocation must apply. Both
short and long document compilation converge on the same completion boundary.

A coroutine can suspend many times without completing. Observing its final
index call avoids Python tracing's ambiguous yield/return events entirely.
Failure raises while native run_add_mutation still owns its rollback journal.
The outer process discards a failed candidate even if native recovery fails.

No compiler implementation, summary fallback, normalization, page formatting,
backlink algorithm or registry naming policy is reproduced here. Upgrade this
adapter only after running its real-native tests against the proposed release.
"""

import asyncio
import hashlib
import importlib.metadata
import inspect
from collections import Counter
from contextlib import contextmanager, nullcontext
from pathlib import Path

from research_fabric.execution import ExecutionError, Session, _validated, response

POLICY = "native-045-complete-v1"
COMPILER_SHA = "9d697d323100916017e25c0826efb3922092f56fe8971b13d725ce80b898d1da"


class CompilationError(RuntimeError):
    """Native compilation lacks definitive applied-operation evidence."""


def _native_modules():
    versions = {name: importlib.metadata.version(name) for name in ("openkb", "litellm")}
    if versions != {"openkb": "0.4.5", "litellm": "1.87.2"}:
        raise CompilationError(f"Unsupported toolchain: {versions}; requalify the native adapter before upgrading")
    from openkb import cli
    from openkb.agent import compiler

    if hashlib.sha256(Path(compiler.__file__).read_bytes()).hexdigest() != COMPILER_SHA:
        raise CompilationError("OpenKB compiler source differs from qualified 0.4.5; requalify adapter")
    return cli, compiler, versions


def _required_plan(local, compiler):
    """Capture the required normalized operations, including code-only links.

    Missing locals mean native execution took an unparseable-plan return, or
    its completion seam changed. Neither is evidence of an accepted empty plan.
    A genuine normalized empty plan does have all six lists and is permitted.
    """
    groups = (
        ("concepts", "create_items", "update_items", "related_items"),
        ("entities", "entity_create", "entity_update", "entity_related"),
    )
    required = []
    for folder, create, update, related in groups:
        if any(key not in local for key in (create, update, related)):
            raise CompilationError("Native compile has no final normalized plan")
        for action, key in (("create", create), ("update", update)):
            required.extend((folder, action, compiler._sanitize_concept_name(item["name"])) for item in local[key])
        required.extend((folder, "related", compiler._sanitize_concept_name(name)) for name in local[related])
    return required


def _verify_plan(local, compiler, writes, applied):
    """Read native normalization; never reimplement planning or page writing."""
    required = _required_plan(local, compiler)
    expected = Counter((folder, slug) for folder, action, slug in required if action != "related")
    if expected != Counter(applied):
        raise CompilationError("Required native page operations have missing outcomes")
    wiki = local["wiki_dir"]
    _verify_summary(local, writes)
    for folder, action, slug in required:
        _verify_page(wiki, folder, action, slug, writes, local["doc_name"])
    return required


def _verify_summary(local, writes):
    wiki = local["wiki_dir"]
    summary = f"summaries/{local['doc_name']}.md"
    if not (wiki / summary).is_file():
        raise CompilationError("Native summary output is missing")
    if local["rewrite_summary"] and (wiki / summary).resolve() not in writes:
        raise CompilationError("Native short-document summary was not applied")


def _summary_outcome(local):
    if not local["rewrite_summary"]:
        return "not-requested"
    return "rewritten" if local.get("candidate") else "native fallback"


def _verify_page(wiki, folder, action, slug, writes, doc_name):
    path = wiki / folder / f"{slug}.md"
    if not path.is_file():
        raise CompilationError(f"Required native output missing: {folder}/{slug}")
    if action != "related" and path.resolve() not in writes:
        raise CompilationError(f"Required operation was not applied: {action} {folder}/{slug}")
    summary = wiki / "summaries" / f"{doc_name}.md"
    if f"[[{folder}/{slug}]]" not in summary.read_text():
        raise CompilationError(f"Required summary backlink missing: {folder}/{slug}")
    if f"[[summaries/{doc_name}]]" not in path.read_text():
        raise CompilationError(f"Required source backlink missing: {folder}/{slug}")


@contextmanager
def strict_native(compiler, cli):
    """Process-scoped adapter. Exceptions reach native run_add_mutation.

    _update_index is the final native action, including empty-plan paths.
    Native retries occur *inside* its mutation; disable that retry so our
    bounded retry always starts with a fresh baseline, not partial pages.
    """
    original_compile = compiler._compile_concepts
    original_index = compiler._update_index
    original_write = compiler.atomic_write_text
    original_retry = cli._run_compile_with_retry
    original_concept = compiler._write_concept
    original_entity = compiler._write_entity
    reports = []
    active = None

    def checked_write(path, text, *args, **kwargs):
        original_write(path, text, *args, **kwargs)
        if Path(path).read_text(encoding="utf-8") != text:
            raise CompilationError(f"Native write did not apply: {Path(path).name}")
        if active is not None:
            active["writes"].add(Path(path).resolve())

    def page_writer(original, folder):
        def checked(wiki_dir, name, content, *args, **kwargs):
            original(wiki_dir, name, content, *args, **kwargs)
            slug = compiler._sanitize_concept_name(name)
            path = (wiki_dir / folder / f"{slug}.md").resolve()
            if active is None or path not in active["writes"]:
                raise CompilationError(f"Native page writer did not apply {folder}/{slug}")
            active["applied"].append((folder, slug))

        return checked

    def checked_index(*args, **kwargs):
        frame = inspect.currentframe().f_back
        try:
            if frame.f_code is not original_compile.__code__ or active is None:
                raise CompilationError("Unknown native compilation completion boundary")
            required = _verify_plan(frame.f_locals, compiler, active["writes"], active["applied"])
            original_index(*args, **kwargs)
            active["report"] = {
                "required": required,
                "summary": frame.f_locals["doc_name"],
                "optional_summary": _summary_outcome(frame.f_locals),
            }
        finally:
            del frame

    async def checked_compile(*args, **kwargs):
        nonlocal active
        if active is not None:
            raise CompilationError("Concurrent native compilation is unsupported in this child")
        active = {"writes": set(), "applied": [], "report": None}
        try:
            await original_compile(*args, **kwargs)
            if active["report"] is None:
                raise CompilationError("Native compilation returned without operation outcomes")
            reports.append(active["report"])
        finally:
            active = None

    def one_attempt(factory, label):
        asyncio.run(factory())

    compiler._compile_concepts = checked_compile
    compiler._update_index = checked_index
    compiler.atomic_write_text = checked_write
    compiler._write_concept = page_writer(original_concept, "concepts")
    compiler._write_entity = page_writer(original_entity, "entities")
    cli._run_compile_with_retry = one_attempt
    try:
        yield reports
    finally:
        compiler._compile_concepts = original_compile
        compiler._update_index = original_index
        compiler.atomic_write_text = original_write
        compiler._write_concept = original_concept
        compiler._write_entity = original_entity
        cli._run_compile_with_retry = original_retry


def native_compile(kb, sources, execution_root=None, profile_index=0):
    """Aggregate single-file native results; directory CLI ignores failures.

    Each file must add exactly one completed native compile report. A native
    dedup hit is insufficient evidence: even historical partial compiles can
    own a hash. Conservatively reprocess all inputs instead of maintaining a
    second source registry. This costs model calls but cannot trust stale data.
    """
    cli, compiler, versions = _native_modules()
    from openkb.state import HashRegistry

    execution = (
        native_execution(execution_root, sources, compiler, cli, profile_index) if execution_root else nullcontext()
    )
    outcomes = []
    with execution, strict_native(compiler, cli) as reports:
        for source in sources:
            # No native registry entry is an acceptance record. The outer seam
            # alone can skip a byte-identical, previously accepted candidate.
            HashRegistry(kb / ".openkb" / "hashes.json").remove_by_hash(hashlib.sha256(source.read_bytes()).hexdigest())
            before = len(reports)
            status = cli.add_single_file(source, kb)
            if status != "added" or len(reports) != before + 1:
                raise CompilationError(f"Native add did not complete: {source.name} ({status})")
            outcomes.append(
                {"source": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), **reports[-1]}
            )
    return {"policy": POLICY, "versions": versions, "sources": outcomes}


@contextmanager
def native_execution(root, sources, compiler, cli, index):
    session = Session(root, "compile", "native-compile", sources)
    profiles = session.profiles()
    profile = profiles[min(index, len(profiles) - 1)]
    model = f"{profile['provider']}/{profile['model']}"
    original_sync, original_async = compiler.litellm.completion, compiler.litellm.acompletion
    original_config = cli.load_config
    original_cache = compiler._accepts_cache_control
    original_key = cli._setup_llm_key
    if profile["provider"] == "chatgpt" and any(p.suffix.lower() not in {".md", ".txt"} for p in sources):
        raise ExecutionError("oauth_compile_requires_text_sources")
    stopped = []

    def config(path):
        value = dict(original_config(path))
        value.update(model=model, timeout=session.config["budget"]["attempt_seconds"])
        return value

    def prepare(kwargs):
        if stopped:
            raise ExecutionError(stopped[0])
        try:
            attempt, timeout = session.begin(profile)
        except ExecutionError as exc:
            stopped.append(exc.reason)
            raise
        kwargs.update(session.arguments(profile, timeout, kwargs.get("max_tokens")))
        kwargs.update(model=model, num_retries=0, max_retries=0)
        return attempt, timeout

    @contextmanager
    def attempt(kwargs):
        try:
            with session.attempt(*prepare(kwargs)) as call:
                yield call
        except ExecutionError as exc:
            if exc.reason not in {"transient", "invalid_output"}:
                stopped.append(exc.reason)
            raise

    def sync(**kwargs):
        with attempt(kwargs) as call:
            call.response = (
                response(profile, kwargs["messages"], kwargs)
                if profile["provider"] == "chatgpt"
                else original_sync(**kwargs)
            )
            _validated(call.response, lambda text: text)
            return call.response

    async def asynchronous(**kwargs):
        with attempt(kwargs) as call:
            call.response = (
                await asyncio.to_thread(response, profile, kwargs["messages"], kwargs)
                if profile["provider"] == "chatgpt"
                else await original_async(**kwargs)
            )
            _validated(call.response, lambda text: text)
            return call.response

    cli.load_config = config
    if profile["provider"] == "chatgpt":
        # Native provider detection otherwise refreshes/logs in outside the journal.
        compiler._accepts_cache_control = lambda model: False
        cli._setup_llm_key = lambda kb: None
    compiler.litellm.completion, compiler.litellm.acompletion = sync, asynchronous
    try:
        yield session
        if stopped:
            raise ExecutionError(stopped[0])
        for row in session.records():
            if row["outcome"] not in {"accepted", "transient", "invalid_output"}:
                raise ExecutionError(row["outcome"])
    finally:
        cli.load_config = original_config
        compiler._accepts_cache_control = original_cache
        cli._setup_llm_key = original_key
        compiler.litellm.completion, compiler.litellm.acompletion = original_sync, original_async
