# Synthetic compiled-wiki review cases (#13 preparation only)

`before/sensor.md` is one retained prior page, not a complete prior KB.
`after/` is a small candidate with a modified entity, new concept, two source
summaries, immutable synthetic snapshots and the existing evidence-ledger format.
The snapshots and all advisory responses are authored fixtures, not empirical
model results. Apply one `variants/*.patch` to a fresh `after/` copy with
`git apply --unidiff-zero`.

| Variant | Deliberate difference | Human-review question |
| --- | --- | --- |
| qualifier | Drops “only in dry indoor conditions” | Did the result become overgeneralized? |
| count | Changes Study A's 24 to Study B's 80 | Was enrollment copied across sources? |
| relocation | Moves the qualified result to linked `drift.md` | Is the supported detail preserved in the linked scope? |
| broken-page | Links to absent `missing-sensor` | Pending mechanical wiki-link rejection |
| broken-asset | References absent `missing-plot.png` | Pending mechanical target/asset binding |

`advisory.json` contains plain scripted replies with explicit scope, before/after
and source evidence, and uncertainty. It also includes a malformed reply and an
unavailable-review case. There is deliberately no new response protocol/parser
or assertion that a live model will detect these changes. Future #13 handling
must record adverse/unavailable opinions without making them semantic gates.

Current tests call real evidence gates and demonstrate that all five wiki
variants leave valid verbatim evidence: those gates do **not** audit wiki prose
or links. They also exercise the existing publication inventory (including
wiki-only/untracked additions) and compiled-page hash-drift rejection. The byte
snapshot is fixture input to that existing check, not proof of native compilation
or an implemented review certificate. Existing #12 asset-bundle tests separately
cover manifest closure; the broken wiki asset here is deliberately unmanifested.

Still blocked on #11: generic source/span/disposition mappings and the final
mechanical review interface. Review snapshot/commit binding, citation and link
rejection, advisory handling, shared workflow/rehearsal integration, and the
README's compiler/ledger data-flow correction remain #13 implementation work.
No fixture grants semantic accuracy, completeness, READY status or merge authority.
