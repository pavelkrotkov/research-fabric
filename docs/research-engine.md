# Importable research engine

`research_fabric.engine` imports without CAO, OpenKB initialization, credentials,
filesystem writes or subprocesses. Constructing a `ResearchRun` is also inert;
`execute()` starts the run. The existing native OpenKB qualification, execution
journal, claim identity, source adapters, gates and terminal-state owner remain
authoritative.

`publication.materialize_evidence` projects accepted packets and bound source
rows into the existing ledgers and packet histories. It reads each packet once
and returns the projected claims. The engine still runs the gates and delegates
the proposed commit to `run_state.finalize_run`; materialization cannot declare
a run ready. The rehearsal does not yet share this operation (#18).

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
location in `run.json`. Preserve those artifacts. To resume, retain `run_root`,
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
