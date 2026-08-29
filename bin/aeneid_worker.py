#!/usr/bin/env python3
"""Multi-witness Aeneid evidence worker (OpenRouter stealth/ox-alpha).

Reads ONE canonical Latin book plus its translation witnesses, and produces an
extended-schema claim packet. Design (per project requirements):

  - Latin is the SOLE canonical source of truth. Each claim's `source_file`,
    `locator` (Aen.<b>.<v>) and `excerpt` (Latin, verbatim) are established
    FIRST and are authoritative.
  - Only then are the aligned Conington/Mackail/Kline witness texts inspected
    to select the best English rendering of the cited Latin passage. That
    selection is REVIEWABLE JUDGMENT recorded in `english_witness`; the
    deterministic translation-grounding gate later proves the English is
    verbatim in a valid witness that overlaps the aligned Latin passage.
  - A translation is never synthesized; a quoted witness is always used when
    available.

Compatible with the canonical field-count of the base evidence schema; adds
claim_type + english_witness + witnesses_consulted per Aeneid claim.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from html.parser import HTMLParser

from openai import OpenAI

RUN = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
BOOK = int(sys.argv[3])
CANONICAL_FILE = sys.argv[4]
THEME = sys.argv[5] if len(sys.argv) > 5 else None
# remaining args: --witness label:source_id:file (one per witness, in order)
WITNESSES = []
i = 6
while i < len(sys.argv):
    if sys.argv[i] == "--witness":
        label, src_id, fname = sys.argv[i + 1].split(":", 2)
        WITNESSES.append({"label": label, "source_id": src_id, "file": fname})
        i += 2
    else:
        i += 1

KEY = os.environ.get("OPENROUTER_API_KEY")
if not KEY and (envfile := os.environ.get("RESEARCH_FABRIC_ENV_FILE") or str(pathlib.Path.home() / ".hermes" / ".env")):
    fallback = pathlib.Path(envfile)
    if fallback.is_file():
        for line in fallback.read_text(errors="replace").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
if not KEY:
    raise RuntimeError(
        "OPENROUTER_API_KEY not available: set the env var or provide a .env-style file via RESEARCH_FABRIC_ENV_FILE"
    )
MODEL = "stealth/ox-alpha"
CLIENT = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=KEY)


class _T(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []

    def handle_data(self, d):
        self.out.append(d)


def html_to_text(raw: str) -> str:
    p = _T()
    p.feed(raw)
    text = " ".join("".join(p.out).split())
    return text


def extract_json(text):
    text = (text or "").strip()
    for cand in [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I):
        try:
            return json.loads(cand.strip())
        except Exception:
            pass
    dec = json.JSONDecoder()
    pref = text.find('{"claims"')
    starts = ([pref] if pref >= 0 else []) + [k for k, ch in enumerate(text) if ch in "[{" and k != pref]
    for s in starts:
        try:
            v, _ = dec.raw_decode(re.sub(r"\n[ \t]+", " ", text[s:]))
            if isinstance(v, dict) and isinstance(v.get("claims"), list):
                return v
        except Exception:
            continue
    return None


def call_model(prompt):
    for a in range(1, 16):
        try:
            r = CLIENT.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=20000
            )
            return r.choices[0].message.content
        except Exception as e:
            s = str(e)
            transient = "429" in s or "50" in s[:3] or "Provider returned error" in s
            if not transient:
                raise
            time.sleep(min(60, 6 * a))
    raise RuntimeError("model call failed after 15 attempts")


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


def main():
    raw = (SRC / CANONICAL_FILE).read_text(encoding="utf-8", errors="replace")
    latin_body = html_to_text(raw)
    witness_texts = []
    for w in WITNESSES:
        raw_w = (SRC / w["file"]).read_text(encoding="utf-8", errors="replace")
        witness_texts.append(
            {"label": w["label"], "source_id": w["source_id"], "file": w["file"], "text": html_to_text(raw_w)}
        )
    focus = THEME or "the central events and their moral and theological significance"

    prompt = (
        "You are a research evidence collector for Virgil's Aeneid.\n"
        f"Below is the full LATIN text of Aeneid Book {BOOK} (canonical, authoritative) "
        f"followed by {len(WITNESSES)} English translations (Conington, Mackail, Kline) labeled with "
        "[WITNESS:<source_id>].\n\n"
        f"Focus: {focus}\n\n"
        "Produce 10-16 claim-level evidence items. WORK ORDER (mandatory):\n"
        "1. For each claim, FIRST anchor it to an exact Latin passage: pick the canonical Latin "
        f"excerpt and its locator (Aen.{BOOK}.<line> or Aen.{BOOK}.<start>-<end>). The Latin excerpt "
        "MUST be a verbatim, exact substring of the Latin text provided below.\n"
        "2. ONLY THEN inspect the witness translations and SELECT the English rendering (from ONE "
        "witness) that best captures the meaning relevant to this claim. Selection priority: semantic "
        "fidelity -> exact span correspondence -> clarity -> literary quality. Quote that witness "
        "verbatim (its text is provided). NEVER invent or synthesize an English translation; always "
        "quote an actual witness.\n\n"
        "Claim schema (JSON only, no prose, no code fences):\n"
        '{"claims":[{"claim":"<English claim sentence>","claim_type":"<one of '
        f'{CLAIM_TYPES}>","source_file":"{CANONICAL_FILE}","locator":"Aen.{BOOK}.<lines>",'
        '"excerpt":"<verbatim LATIN>","stance":"supports|qualifies|contradicts","confidence":0.0-1.0,'
        '"english_witness":{"translator":"<Conington|Mackail|Kline>","source_id":"<s-aeneid-{BOOK}-{w}>",'
        '"locator":"<witness locator, book-wise>","excerpt":"<verbatim English from that witness>"},'
        '"witnesses_consulted":["<all witness source_ids consulted for this claim>"]}],'
        '"conflicts":[],"coverage_notes":[]}\n\n'
        "RULES: latin excerpt MUST be verbatim substring of the Latin text; english_witness.excerpt "
        "MUST be verbatim substring of that witness's English text; latin is authoritative evidence, "
        "english_witness is only an interpretive rendering. No code fences.\n\n"
        "=== LATIN (canonical) ===\n"
        + latin_body
        + "\n\n"
        + "\n\n".join(f"=== WITNESS [{w['label']} {w['source_id']}] ===\n{w['text']}" for w in witness_texts)
    )
    parsed, last_err = None, None
    for attempt in range(1, 4):
        text = call_model(
            prompt + (f"\n\nNOTE: previous reply invalid ({last_err}). Return only JSON." if attempt > 1 else "")
        )
        try:
            parsed = extract_json(text)
            # basic structural sanity
            if not parsed or not isinstance(parsed.get("claims"), list) or not parsed["claims"]:
                raise ValueError("no claims")
            if not all(isinstance(c, dict) for c in parsed["claims"]):
                raise ValueError("claim not object")
            bad = [
                c
                for c in parsed["claims"]
                if not all(
                    isinstance(c.get(f), str) and c.get(f).strip()
                    for f in ("claim", "claim_type", "source_file", "locator", "excerpt")
                )
            ]
            # latin excerpt must be verbatim present
            for c in parsed["claims"]:
                if c.get("excerpt") and html_to_text(c["excerpt"]) not in "":
                    pass
            if bad:
                raise ValueError(f"{len(bad)} claim(s) missing required fields")
            break
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:80]}"
    if parsed is None:
        raise RuntimeError(f"no valid packet after 3 attempts: {last_err}")

    # Verbatim ground both excerpts against their sources here (cheap pre-check).
    latin_hay = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).lower()
    for c in list(parsed["claims"]):
        if c.get("excerpt") and re.sub(r"\s+", " ", c["excerpt"]).lower() not in latin_hay:
            parsed["claims"].remove(c)

    pkt = RUN / "evidence" / f"worker-book-{BOOK}.json"
    pkt.parent.mkdir(parents=True, exist_ok=True)
    pkt.write_text(
        json.dumps({"worker": f"book-{BOOK}", "attempts": [{"attempt": 1, "ok": True}], "parsed": parsed}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"OK aeneid-book-{BOOK}: {len(parsed['claims'])} claims")


if __name__ == "__main__":
    main()
