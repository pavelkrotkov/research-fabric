"""Frozen reading-plan loading, assignment binding, and publication projection.

The plan is immutable run input: workers, repair, materialization, and gates all consume
its exact source identities and spans. Loading validates current snapshots before use;
publication reprojects quote coordinates from current accepted excerpts rather than
persisting stale offsets.

This module does not discover files, compile a wiki, or mutate evidence. It is the
read-only authority that proves a worker, persisted claim, or published citation
still refers to the same frozen assignment.

Keeping those checks here prevents worker, repair, provenance, and grounding paths
from growing separate interpretations of reading identity. The class intentionally
returns deterministic errors instead of attempting migration or recovery.
"""

from __future__ import annotations

import json
import pathlib
from ._reading_span import reading_quote, span_range, span_text, structure
from ._reading_validation import policy, validate_plan


def _load_sources(data, source_paths):
    from .sources import _bind_representation, representation_for, source_attestation

    reps, boundaries = {}, {}
    for name, source in source_paths.items():
        rep = representation_for(pathlib.Path(source))
        expected = {**source_attestation(rep), "source_file": name}
        if any(data["sources"][name].get(key) != value for key, value in expected.items()):
            raise ValueError(f"frozen reading plan source drift: {name}")
        _bind_representation(data["sources"][name], rep, rep.path)
        reps[name] = rep
        _, _, boundaries[name] = structure(name, rep)
    return reps, boundaries


def _profile_identity(data, profile):
    works = {name: row.get("work_id") for name, row in data.get("sources", {}).items()}
    return data.get("policy"), data.get("works"), works


def _packet_bindings(plan, packet, reading_id):
    from .core import packet_defects

    if packet.get("worker") != reading_id:
        raise ValueError("reading packet worker differs from dispatched assignment")
    defects = packet_defects(packet.get("parsed"))
    if defects:
        raise ValueError("; ".join(defects))
    claims = packet["parsed"]["claims"]
    bindings = [plan.claim(reading_id, claim)[0] for claim in claims]
    if any(claim.get("reading") != binding for claim, binding in zip(claims, bindings)):
        raise ValueError("accepted reading assignment differs from frozen plan")
    return bindings


def _published_claim_error(plan, claim, quotes):
    if plan is None:
        raise ValueError("reading claim has no verified frozen plan")
    expected = plan.project_claim(claim, quotes=quotes)
    if any(claim.get(key) != value for key, value in expected.items()):
        raise ValueError("reading claim projection differs from frozen assignment/current excerpt")


class ReadingPlan:
    """One frozen assignment shared by dispatch, publication, repair, and resume.

    The object carries validated representations only; it never discovers sources or
    changes a frozen assignment after generation has begun.
    """

    def __init__(self, data, representations, boundaries):
        self.data, self.representations, self.boundaries = data, representations, boundaries

    @classmethod
    def load(cls, path, source_paths, profile=None):
        """Load only when source bytes and optional project policy still match.

        Rehashed or retargeted plans therefore fail before a worker or repair model runs.
        """
        data = json.loads(pathlib.Path(path).read_text())
        expected = (policy(profile), profile["works"], profile["sources"]) if profile is not None else None
        if expected is not None and _profile_identity(data, profile) != expected:
            raise ValueError("frozen reading plan project policy or work assignment drift")
        if not isinstance(data.get("sources"), dict):
            raise ValueError("frozen reading plan sources missing or invalid")
        if set(source_paths) != set(data["sources"]):
            raise ValueError("frozen reading plan source set drift")
        reps, boundaries = _load_sources(data, source_paths)
        plan = cls(data, reps, boundaries)
        plan.validate()
        return plan

    def reading(self, reading_id):
        matches = [row for row in self.data["readings"] if row["id"] == reading_id]
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous reading assignment: {reading_id}")
        return matches[0]

    def source_names(self, reading_id):
        reading = self.reading(reading_id)
        names = {self.data["sections"][key]["source_file"] for role in ("primary", "context") for key in reading[role]}
        return sorted(names)

    def input(self, reading_id):
        """Render assigned source text with labels that are instructions, not evidence.

        Claims are later accepted only when their exact excerpt lies inside the bound span.
        """
        parts = []
        for role in ("primary", "context"):
            for key in self.reading(reading_id)[role]:
                span = self.data["sections"][key]
                source = span["source_file"]
                start, end = span["lines"]
                label = f"\n[{role.upper()} SOURCE {source} L{start}-{end - 1}]\n"
                parts.extend((label, span_text(self.representations, span)))
        return "".join(parts)

    def _assignments(self, reading_id, source_file):
        for role in ("primary", "context"):
            for key in self.reading(reading_id)[role]:
                span = self.data["sections"][key]
                if span["source_file"] == source_file:
                    yield (
                        {
                            "plan_sha256": self.data["sha256"],
                            "reading_id": reading_id,
                            "work_id": self.data["sources"][source_file]["work_id"],
                            "role": role,
                            "lines": span["lines"],
                        },
                        span,
                    )

    def claim(self, reading_id, claim):
        """Bind one exact excerpt to exactly one assigned primary or context span.

        Missing and ambiguous occurrences fail closed instead of guessing a locator.
        """
        matches = [
            binding
            for binding, span in self._assignments(reading_id, claim.get("source_file"))
            if claim.get("excerpt") in span_text(self.representations, span)
        ]
        if len(matches) != 1:
            raise ValueError("reading quote is missing, ambiguous, or outside its assigned source spans")
        binding = matches[0]
        return binding, reading_quote(self.representations[claim["source_file"]], {**claim, "reading": binding})

    def validate_claim(self, claim):
        """Prove a persisted claim still names the frozen source and assignment.

        This is the shared identity check used by publication and post-repair validation.
        """
        source = self.data["sources"][claim["source_file"]]
        if "source_ids" in claim and claim["source_ids"] != [source["source_id"]]:
            raise ValueError("reading claim source IDs differ from frozen source identity")
        binding = claim["reading"]
        valid = any(
            binding == expected for expected, _ in self._assignments(binding["reading_id"], claim["source_file"])
        )
        if not valid:
            raise ValueError("accepted reading assignment differs from frozen plan")

    def project_claim(self, claim, *, quotes=True):
        """Project immutable reading identity and current exact quote coordinates.

        Context deliberately carries no independence group and cannot count as support.
        """
        self.validate_claim(claim)
        binding = claim["reading"]
        result = {
            "reading": binding,
            "independence_group": binding["work_id"] if binding["role"] == "primary" else None,
        }
        if quotes:
            result["quote_span"] = reading_quote(self.representations[claim["source_file"]], claim)
        return result

    def section(self, section_id):
        from ._reading_citations import section

        return section(self, section_id)

    def citation_policy(self):
        from ._reading_citations import citation_policy

        return citation_policy(self)

    def validate_packet(self, packet, reading_id, acceptance):
        """Require every worker claim to belong to its dispatched frozen assignment.

        Reading-specific claim bounds apply after ordinary evidence-packet validation.
        """
        bindings = _packet_bindings(self, packet, reading_id)
        minimum = acceptance.get("min_claims_per_reading", 1)
        if sum(binding["role"] == "primary" for binding in bindings) < minimum:
            raise ValueError("reading packet has too few primary evidence claims")
        limit = acceptance.get("max_claims_per_reading")
        if limit is not None and len(bindings) > limit:
            raise ValueError("reading packet exceeds maximum evidence claims")

    def validate(self):
        validate_plan(self.data, self.representations, self.boundaries)

    @staticmethod
    def published_claim_errors(field_root, source_rows, claims, *, quotes=True):
        """Yield deterministic publication defects instead of silently repairing drift.

        Both provenance and grounding gates call this projection to share one policy.
        """
        plan = published_reading_plan(field_root, source_rows)
        for claim in claims:
            if plan is None and "reading" not in claim:
                continue
            try:
                _published_claim_error(plan, claim, quotes)
            except (ValueError, KeyError, TypeError) as exc:
                yield claim.get("claim_id"), str(exc)


def published_reading_plan(field_root, source_rows):
    """Load publication mappings by logical source path, never snapshot basename.

    Duplicate logical names fail before claim projection can attach to the wrong work.
    """
    from ._source_assets import safe_path

    root = pathlib.Path(field_root)
    path = root / "evidence/reading-plan.json"
    if not path.exists():
        return None
    paths = {row["source_file"]: safe_path(root, row["snapshot"]) for row in source_rows}
    if len(paths) != len(source_rows):
        raise ValueError("ambiguous published reading source identity")
    return ReadingPlan.load(path, paths)
