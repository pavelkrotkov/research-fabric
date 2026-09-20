# Synthetic compiled-wiki review cases

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

Current tests show that provenance/excerpt gates alone accept all five wiki
variants, while the compiled-wiki audit rejects broken page/asset links and
accepts semantic-only qualifier/count/relocation cases for human review. The
audit snapshots publication hashes, includes modified and untracked wiki files
in its diff, and is invalidated by later candidate-byte changes. #11's published
reading-plan validator supplies source/span/disposition identity; no fixture
grants semantic accuracy, completeness, READY status or merge authority.
