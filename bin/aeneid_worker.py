#!/usr/bin/env python3
"""Multi-witness Aeneid evidence worker (OpenRouter direct API).

Reads ONE canonical Latin book plus its translation witnesses and produces an
extended-schema claim packet. STAGED design: the model is a reasoner that
exhausts its output budget when given the whole corpus (~170K chars) in one
call, so the work is split into small calls (each piped to a reasoning model
that returns clean JSON):

  Stage 1 (Latin only, ~34K chars)
      Establish the claims + verbatim LATIN excerpt + canonical locator
      (Aen.<b>.<line>). This is the authoritative grounding evidence, set
      FIRST, per project requirement #9.
  Stage 2 (per witness, ~40K chars each)
      For EACH witness (Conington, Mackail, Kline), given the Latin excerpts,
      quote a verbatim English rendering for every claim. Only ONE witness
      text per call, so reasoning fits the output budget.
  Stage 3 (small selection call)
      Given each claim's per-witness verbatim candidates, select the single
      best English witness (semantic fidelity -> exact span -> clarity ->
      literary quality). This SELECTION is recorded as reviewable judgment;
      the deterministic translation-grounding gate later proves the quoted
      English is verbatim in a valid witness overlapping the aligned Latin.

Verbatim is enforced mechanically at each stage (excerpt must be an exact
normalized substring of its source); a claim that cannot be grounded in a real
witness is dropped rather than published with synthesized English.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.execution import ExecutionError, InvalidOutput, packet_policy_defects, worker_session
from research_fabric.sources import representation_for, source_attestation  # noqa: E402

RUN = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
BOOK = int(sys.argv[3])
CANONICAL_FILE = sys.argv[4]
THEME = sys.argv[5] if len(sys.argv) > 5 else None
WITNESSES = []
i = 6
while i < len(sys.argv):
    if sys.argv[i] == "--witness":
        label, src_id, fname = sys.argv[i + 1].split(":", 2)
        WITNESSES.append({"label": label, "source_id": src_id, "file": fname})
        i += 2
    else:
        i += 1

SESSION = worker_session(
    RUN,
    "extraction",
    f"book-{BOOK}",
    [SRC / CANONICAL_FILE, *(SRC / w["file"] for w in WITNESSES)],
    project={"project": "aeneid"},
)
CLAIM_TYPES = [
    "textual",
    "linguistic",
    "characterization",
    "thematic",
    "intertextual",
    "historical",
    "scholarly",
    "reception",
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip().lower()


def call_model(messages, key="selections"):
    def validate(text):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise InvalidOutput()
        rows = parsed.get(key)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise InvalidOutput()
        if key == "claims" and not rows:
            raise InvalidOutput()
        return rows

    return SESSION.call(messages, validate=validate)


def build_latin_prompt(latin_body, canon_file):
    focus = THEME or "the central events and their moral and theological significance"
    return (
        "You are a research evidence collector for Virgil's Aeneid. You are given the full "
        f"LATIN text of Aeneid Book {BOOK} (canonical, authoritative).\n"
        f"Focus: {focus}\n\n"
        "Produce 10-16 claim-level evidence items. GROUNDING RULE: each claim's `excerpt` must be a "
        "VERBATIM substring of the Latin text you were given, and its `locator` must be "
        f"Aen.{BOOK}.<line> or Aen.{BOOK}.<start>-<end>. The Latin is the authoritative evidence. "
        "Do NOT yet choose an English translation.\n\n"
        "Claim schema (JSON only, no prose, no code fences):\n"
        '{"claims":[{"claim":"<English claim sentence>","claim_type":"<one of: '
        f'{", ".join(CLAIM_TYPES)}>","source_file":"{canon_file}",'
        f'"locator":"Aen.{BOOK}.<lines>","excerpt":"<verbatim LATIN>",'
        '"stance":"supports|qualifies|contradicts","confidence":0.0-1.0}],'
        '"conflicts":[],"coverage_notes":[]}\n\n'
        "=== LATIN (canonical) ===\n" + latin_body
    )


def build_witness_prompt(items, witness_label, witness_sid, wtext):
    """Per-witness: given the Latin excerpts + claim numbers, quote verbatim English."""
    head = "\n".join(f"[{it['i']}] {it['locator']} :: {it['latin']}" for it in items)
    return (
        f"You are selecting verbatim English renderings. Below are numbered Latin passages. "
        f"Quote, for EACH numbered item, the single VERBATIM sentence/fragment from the following "
        f"translation ({witness_label}) that renders it. "
        "The English excerpt MUST be an exact substring of the translation text provided. "
        "If no span clearly renders that Latin passage, emit [NONE] for that item.\n\n"
        "=== NUMBERED LATIN PASSAGES ===\n" + head + "\n\n"
        f"[{witness_label}] <{witness_sid}>: {wtext}\n\n"
        f'Output exactly JSON: {{"selections":[{{"i":<number>,"translator":"{witness_label}",'
        f'"source_id":"{witness_sid}","excerpt":"<verbatim English>"}}]}}\n'
        "No prose, no code fences. Include only items with a verbatim match."
    )


def build_select_prompt(items):
    """Given each claim's per-witness candidates, pick a single best English witness."""
    blocks = []
    for it in items:
        cands = "\n        ".join(f"[{c['translator']}] {c['excerpt']}" for c in it.get("candidates", []))
        blocks.append(f"[{it['i']}] {it['locator']} LATIN: {it['latin']}\n    Candidates:\n        {cands}")
    return (
        "For each numbered item choose the SINGLE English rendering that best captures the meaning "
        "relevant to that Latin passage. Selection priority: semantic fidelity -> exact span "
        "correspondence -> clarity -> literary quality. Select ONLY from the given candidates; "
        "do not invent or rephrase. If no candidate is adequate, emit null.\n\n"
        "=== ITEMS ===\n" + "\n\n".join(blocks) + "\n\n"
        'Output exactly JSON: {"selections":[{"i":<number>,"translator":"<Kline|Conington|Mackail>",'
        '"excerpt":"<the EXACT chosen candidate text>"}]}\n'
        "No prose, no code fences."
    )


def verbatim(excerpt, text):
    """Empty or non-text excerpts are not evidence, even if containment succeeds."""
    return isinstance(excerpt, str) and bool(norm(excerpt)) and norm(excerpt) in norm(text)


def indexed(selections):
    """Model item numbers are local selection indices, never persistent claim IDs."""
    result = {}
    for row in selections:
        try:
            result[int(row.get("i"))] = row
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def collect_latin(text):
    claims = call_model([{"role": "user", "content": build_latin_prompt(text, CANONICAL_FILE)}], key="claims")
    kept = [claim for claim in claims if verbatim(claim.get("excerpt"), text)]
    if not kept:
        raise RuntimeError("stage 1 produced no verbatim Latin excerpts")
    return [
        {"i": i, "locator": claim["locator"], "latin": claim["excerpt"], "claim": claim, "candidates": []}
        for i, claim in enumerate(kept, 1)
    ]


def collect_witness(items, witness):
    text = witness["representation"].text
    rows = call_model(
        [{"role": "user", "content": build_witness_prompt(items, witness["label"], witness["source_id"], text)}]
    )
    selections = indexed(rows)
    for item in items:
        excerpt = selections.get(item["i"], {}).get("excerpt")
        if verbatim(excerpt, text):
            item["candidates"].append(
                {"translator": witness["label"], "source_id": witness["source_id"], "excerpt": excerpt}
            )


def chosen_candidate(item, selection):
    """Preserve the existing advisory-choice fallback to a grounded candidate.

    The model can select a witnessed span, never introduce one during selection.
    Missing/invalid choices use the first already-grounded witness in input order.
    """
    for candidate in item["candidates"]:
        if candidate["translator"] != selection.get("translator"):
            continue
        excerpt = selection.get("excerpt")
        if isinstance(excerpt, str) and norm(excerpt) == norm(candidate["excerpt"]):
            return candidate
    return item["candidates"][0]


def assemble_claim(item, selection, witnesses):
    candidate = chosen_candidate(item, selection)
    locator = item["locator"]
    witness = {
        **candidate,
        "translator": candidate["translator"].capitalize(),
        "locator": f"{BOOK}.{locator.split('.', 1)[-1]}" if "." in locator else locator,
    }
    consulted = [w["source_id"] for w in witnesses if w["source_id"] != candidate["source_id"]]
    return {**item["claim"], "english_witness": witness, "witnesses_consulted": [*consulted, candidate["source_id"]]}


def collect_claims(canonical, witnesses):
    items = collect_latin(canonical.text)
    for witness in witnesses:
        collect_witness(items, witness)
    selectable = [item for item in items if item["candidates"]]
    if not selectable:
        raise RuntimeError("no claims with a verbatim English witness survived all stages")
    rows = call_model([{"role": "user", "content": build_select_prompt(selectable)}])
    selections = indexed(rows)
    return [assemble_claim(item, selections.get(item["i"], {}), witnesses) for item in selectable]


def main():
    canonical = representation_for(SRC / CANONICAL_FILE)
    witnesses = [{**w, "representation": representation_for(SRC / w["file"])} for w in WITNESSES]
    claims = collect_claims(canonical, witnesses)
    packet = {
        "worker": f"book-{BOOK}",
        "attempts": SESSION.records(),
        "execution": SESSION.records(),
        "source_provenance": sorted(
            [source_attestation(canonical)] + [source_attestation(w["representation"]) for w in witnesses],
            key=lambda row: row["source_file"],
        ),
        "parsed": {"claims": claims, "conflicts": [], "coverage_notes": []},
    }
    if packet_policy_defects(packet, {"project": "aeneid"}):
        raise ExecutionError("artifact_model_policy")
    path = RUN / "evidence" / f"worker-book-{BOOK}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    print(f"OK aeneid-book-{BOOK}: {len(claims)} claims")


if __name__ == "__main__":
    main()
