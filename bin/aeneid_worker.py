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
from html.parser import HTMLParser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.execution import ExecutionError, InvalidOutput, packet_policy_defects, worker_session

RUN = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
BOOK = int(sys.argv[3])
CANONICAL_FILE = sys.argv[4]
THEME = sys.argv[5] if len(sys.argv) > 5 else None
WITNESSES = []  # [{label, source_id, file}]
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


class _T(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(raw: str) -> str:
    p = _T()
    p.feed(raw)
    return " ".join("".join(p.out).split())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip().lower()


def call_model(messages, key="selections"):
    def validate(text):
        parsed = extract_json(text)
        rows = (parsed or {}).get(key)
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            raise InvalidOutput()
        return parsed

    return SESSION.call(messages, validate=validate)


def extract_json(text):
    t = (text or "").strip()
    dec = json.JSONDecoder()
    # Prefer the exact top-level keys we asked for. raw_decode works from a
    # given offset and handles nested braces correctly, so we do NOT slice to a
    # boundary; just try each candidate offset and require a dict with the
    # expected key (a stray leading array must not win).
    for idx in [k for k, ch in enumerate(t) if ch in "{"][:100]:
        try:
            obj, _ = dec.raw_decode(re.sub(r"\n[ \t]+", " ", t[idx:]))
        except Exception:
            continue
        if isinstance(obj, dict) and ("claims" in obj or "selections" in obj):
            return obj
    return None


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


def main():
    raw_latin = (SRC / CANONICAL_FILE).read_text(encoding="utf-8", errors="replace")
    latin_body = html_to_text(raw_latin)
    latin_hay = norm(raw_latin)
    witness_texts = []
    for w in WITNESSES:
        raw_w = (SRC / w["file"]).read_text(encoding="utf-8", errors="replace")
        witness_texts.append({**w, "text": html_to_text(raw_w), "hay": norm(raw_w)})

    # ---- Stage 1: Latin-only claims ----
    packet = call_model([{"role": "user", "content": build_latin_prompt(latin_body, CANONICAL_FILE)}], key="claims")
    claims = packet["claims"]

    # Drop claims whose Latin excerpt is NOT verbatim in the Latin snapshot.
    kept = []
    for c in claims:
        if norm(c.get("excerpt")) in latin_hay:
            kept.append(c)
    claims = kept
    if not claims:
        raise RuntimeError("stage 1 produced no verbatim Latin excerpts")

    items = [
        {"i": idx, "locator": c["locator"], "latin": c["excerpt"], "claim": c, "candidates": []}
        for idx, c in enumerate(claims, 1)
    ]

    # ---- Stage 2: per-witness verbatim English ----
    for w in witness_texts:
        text = call_model(
            [{"role": "user", "content": build_witness_prompt(items, w["label"], w["source_id"], w["text"])}]
        )
        sel = text
        sel_list = (sel or {}).get("selections") or []
        by_i = {}
        for s in sel_list:
            try:
                by_i[int(s.get("i"))] = s
            except (TypeError, ValueError):
                continue
        for it in items:
            s = by_i.get(it["i"])
            if not s or not isinstance(s, dict):
                continue
            ex = s.get("excerpt")
            if not isinstance(ex, str) or not ex.strip():
                continue
            if norm(ex) not in w["hay"]:  # verbatim check against THIS witness
                continue
            it["candidates"].append(
                {
                    "translator": w["label"],
                    "source_id": w["source_id"],
                    "excerpt": ex,
                }
            )

    # ---- Stage 3: select best English witness per claim ----
    selectable = [it for it in items if it["candidates"]]
    selection_map = {}
    if selectable:
        text = call_model([{"role": "user", "content": build_select_prompt(selectable)}])
        sel = text
        for s in (sel or {}).get("selections") or []:
            try:
                selection_map[int(s.get("i"))] = s
            except (TypeError, ValueError):
                continue

    # ---- Assemble final packet; drop claims with no verbatim selected English ----
    final = []
    for it in items:
        chosen = selection_map.get(it["i"])
        ew = None
        if chosen and isinstance(chosen, dict):
            translator = chosen.get("translator")
            cand = next((c for c in it["candidates"] if c["translator"] == translator), None)
            if cand and norm(chosen.get("excerpt", "")) == norm(cand["excerpt"]):
                # rely on the actual verbatim candidate
                ew = {
                    "translator": cand["translator"].capitalize(),
                    "source_id": cand["source_id"],
                    "locator": f"{BOOK}.{it['locator'].split('.', 1)[-1]}" if "." in it["locator"] else it["locator"],
                    "excerpt": cand["excerpt"],
                }
            else:
                # reviewer judgement picked a candidate but the transcript didn't match verbatim:
                # fall back to the first verbatim candidate for this claim
                if it["candidates"]:
                    c0 = it["candidates"][0]
                    ew = {
                        "translator": c0["translator"].capitalize(),
                        "source_id": c0["source_id"],
                        "locator": f"{BOOK}.{it['locator'].split('.', 1)[-1]}"
                        if "." in it["locator"]
                        else it["locator"],
                        "excerpt": c0["excerpt"],
                    }
        else:
            if it["candidates"]:
                c0 = it["candidates"][0]
                ew = {
                    "translator": c0["translator"].capitalize(),
                    "source_id": c0["source_id"],
                    "locator": f"{BOOK}.{it['locator'].split('.', 1)[-1]}" if "." in it["locator"] else it["locator"],
                    "excerpt": c0["excerpt"],
                }
        if ew is None:
            continue
        final.append(
            {
                **it["claim"],
                "english_witness": ew,
                "witnesses_consulted": [w["source_id"] for w in witness_texts if w["label"] != ew["translator"]]
                + [ew["source_id"]],
            }
        )

    if not final:
        raise RuntimeError("no claims with a verbatim English witness survived all stages")

    pkt = RUN / "evidence" / f"worker-book-{BOOK}.json"
    pkt.parent.mkdir(parents=True, exist_ok=True)
    packet = {
        "worker": f"book-{BOOK}",
        "attempts": SESSION.records(),
        "execution": SESSION.records(),
        "parsed": {"claims": final, "conflicts": [], "coverage_notes": []},
    }
    if packet_policy_defects(packet, {"project": "aeneid"}):
        raise ExecutionError("artifact_model_policy")
    pkt.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    print(f"OK aeneid-book-{BOOK}: {len(final)} claims")


if __name__ == "__main__":
    main()
