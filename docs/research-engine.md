# Importable research engine

`research_fabric.engine` imports without CAO, OpenKB initialization, credentials,
filesystem writes or subprocesses. Constructing a `ResearchRun` is also inert;
`execute()` starts the run. The existing native OpenKB qualification, execution
journal, claim identity, source adapters, gates and terminal-state owner remain
authoritative.

`publication.publish_candidate` is the single post-compilation publication
operation used by both the engine and `bin/dryrun_publication.py`. Its prepared
input is an accepted compile receipt, accepted evidence packets, frozen source
files plus manifest, and an isolated compiled candidate. It revalidates packet
and source revisions, materializes the ledgers/snapshots, runs the real
deterministic provenance/grounding gates, snapshots the complete generated diff,
records advisory availability, and creates/verifies the proposed Git commit.
Generated `wiki/`, `evidence/`, `raw/`, and `.gitattributes` outputs share
one staging policy; an empty diff, stale compiled output, ignored output, commit
rewrite/failure, or dirty post-commit tree cannot succeed.

The publisher does **not** compile, plan, call models, merge main, or set terminal
run state. Production may supply an advisory callback, but its result never
overrides a deterministic gate. Offline rehearsal supplies no advisory callback,
so the review record says `unavailable` rather than inventing success. The
rehearsal copies both the prepared run and candidate into a disposable directory;
the input run and KB are read-only. After a validated publication result returns,
only `ResearchRun` calls `run_state.record_ready`.

Parity is exercised in the native workflow fixture: the same prepared candidate
and accepted run are published once by `ResearchRun` and once by the rehearsal,
then normalized ledgers, gate outcomes, changed/tracked publication files and
output bytes are compared. Portable publication tests cover source-manifest and
claim mapping failures, stale evidence/candidate revisions, deterministic gate
failure, commit/dirty-tree failures, missing compiled outputs and the shared
empty-diff rejection.

## Portable offline rehearsal

From a checkout with dependencies installed (`uv sync --dev`):

```sh
uv run python bin/dryrun_publication.py /path/to/prepared-run /path/to/candidate-kb \
  --projects-dir /path/to/project-specs --project odyssey --base main
```

The run must already contain `sources/`, `source-manifest.jsonl`, accepted
`evidence/worker-*.json`, and exactly one `verification/compile/*/completed.json`;
reading projects also require their frozen `reading-plan.json`. The candidate's
`wiki/` and `raw/` are copied as prepared, including uncommitted compiled output.
This command does not compile or contact any model/provider.

Exactly one branch option is required: `--base NAME` clones an existing local
branch (including `main`) and creates a disposable `rehearsal/dryrun-pub-*`
proposal, or `--branch NAME` selects an existing proposal branch. There is no
implicit base; `--branch main` and `--branch master` are rejected. Input refs,
branch, worktree and prepared artifacts are never changed; no merge occurs.

Gate scripts default to the checkout containing this script; `--engine-root`
overrides that root. Project lookup uses `--projects-dir` if provided, otherwise
`<engine-root>/projects`, loading `<project>.yaml` (`odyssey` by default).
All relative paths are relative to the invoking working directory. No host path
or environment-variable override takes precedence over these arguments.
Root aliases, including macOS's default temporary directory and symlinked
`TMPDIR`, are resolved consistently; no canonical-TMPDIR workaround is needed.
Nested symlinks within the prepared run or candidate are rejected rather than
followed, preventing copies/publication writes from escaping into input data.
On failure after cloning, the command prints the preserved disposable diagnostics
directory; successful copies are removed. Invalid project, branch and prepared
input configuration is checked before cloning where possible.

```python
from pathlib import Path
from research_fabric.engine import ResearchRun, RunConfig

config = RunConfig(
    engine_root=Path('/checkout/research-fabric'),
    project_path=Path('/checkout/research-fabric/projects/odyssey.yaml'),
    field_root=Path('/worktrees/research-proposal'),
    run_root=Path('/runs/research-proposal'),
    source_dir=Path('/snapshots/odyssey'),
    question='What motivates the voyage?',
)

def advisory(*, agent, prompt, step_id, timeout):
    # Call your supervisor/verifier transport and return its response text.
    return transport(agent=agent, prompt=prompt, step_id=step_id, timeout=timeout)

result = ResearchRun(config, advisory).execute()
```

The callback supplies planning and advisory text only. Workers still use the
existing execution profile, and compilation still uses qualified native OpenKB.
`execution` passes overrides to the existing resolver; it is not another model
policy. Optional worker/compiler/OpenKB command arguments provide concrete
process boundaries for controlled environments and offline integration tests.
Sources use the shared adapter registry and its existing contract. `result` includes the proposed
commit and evidence location only after all gates and commit verification pass.
The engine never merges the proposal into the KB's main branch.

Unsafe configuration (run artifacts inside the KB) is rejected before an attempt
starts and before any writes, preserving the existing worktree and prior artifacts.
A started run that fails raises and records `FAILED`, its failing stage, reason and artifact
location in `run.json`. If that state file is damaged or cannot be written, the
recording failure is logged and the original exception is preserved; damaged
bytes are not silently replaced with an invented state. Preserve those artifacts. To resume, retain `run_root`,
set `reuse_evidence_dir=run_root / 'evidence'`, and supply any explicit execution
overrides. Compatible completed packets are reused; missing or incompatible
packets are collected. The journal retains earlier attempts and cumulative budget
usage across configuration revisions. A model change does not reset the budget.
If a failed attempt left the KB dirty, supply a fresh isolated KB worktree; the
engine does not discard the failed worktree's changes to make it appear clean.

## CAO adapter

`workflows/research.py` only maps launch inputs, supplies CAO advisory calls and
emits the engine result. Set its optional `engine_root` input to the repository
checkout for CAO resume: the runner may relocate the workflow script into its
journal snapshot. This path is resolved before importing the engine. Existing
`RESEARCH_FABRIC_*` environment overrides remain supported when the launcher
forwards them.

The pinned upstream CAO 2.4.1 contract discovers the literal `INPUTS` through
[AST parsing](https://github.com/awslabs/cli-agent-orchestrator/blob/v2.4.1/src/cli_agent_orchestrator/services/workflow_spec_service.py)
and launches both fresh and resumed scripts as
[`sys.executable, script_path`](https://github.com/awslabs/cli-agent-orchestrator/blob/v2.4.1/src/cli_agent_orchestrator/services/script_runner.py).
The subprocess contract test relocates the adapter and uses that runner's filtered
environment. Separate mapping tests check `get_inputs`, `run_step` and
`emit_output`. These tests do not claim a live CAO service or the deployment's
additional lifecycle patches were exercised.
