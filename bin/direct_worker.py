"""Direct-API evidence worker (OpenRouter stealth/ox-alpha) — openai SDK client.

Replaces the CAO tmux worker: reads a book's HTML, strips it to text, makes a
chat completion with robust 429/5xx backoff + parse retry, and writes a
validated claim packet. No tmux, no screen scraping, no Codex quota.

Run with the hermes venv python (has the openai SDK):
  <hermes-venv>/bin/python direct_worker.py <run_root> <source_dir> \
     <book> <source_filename> [theme]
Writes <run_root>/evidence/worker-book-<N>.json
"""

import html
import json
import os
import pathlib
import re
import sys
import time
from html.parser import HTMLParser

from openai import OpenAI

BOOK = int(sys.argv[3])
RUN = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
SOURCE_FILE = sys.argv[4] if len(sys.argv) > 4 else f"odyssey-book-{BOOK}.html"
THEME = sys.argv[5] if len(sys.argv) > 5 else None
KEY = os.environ.get("OPENROUTER_API_KEY")
if not KEY:
    # Load from the main Hermes home's .env (never printed or persisted).
    for line in pathlib.Path.home().joinpath(".hermes", ".env").read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
if not KEY:
    raise RuntimeError("OPENROUTER_API_KEY not available")
MODEL = "stealth/ox-alpha"
BASE = "https://openrouter.ai/api/v1"
CLIENT = OpenAI(base_url=BASE, api_key=KEY)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, t, a):
        if t in ("script", "style"):
            self.skip += 1
        if t in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, t):
        if t in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, d):
        if not self.skip:
            self.parts.append(d)


def html_to_text(raw):
    tp = Text()
    tp.feed(raw)
    text = html.unescape("".join(tp.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    idx = text.find("Homer")
    if idx > 0:
        text = text[idx:]
    return text


def extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    # ox-alpha may add prose around the object; locate the claims opener.
    i = t.find('{"claims"')
    if i > 0:
        t = t[i:]
    return json.loads(t)


def defects(parsed):
    out = []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("claims"), list):
        return ["packet has no claims list"]
    if not parsed["claims"]:
        return ["claims list empty"]
    for i, c in enumerate(parsed["claims"], 1):
        if not isinstance(c, dict):
            out.append(f"claim {i} not object")
            continue
        for f in ("claim", "source_file", "locator", "excerpt"):
            v = c.get(f)
            if not isinstance(v, str) or not v.strip() or v.strip() in ("...", "\u2026", "TBD"):
                out.append(f"claim {i} field {f} placeholder/missing")
    return out


def call_model(prompt):
    for a in range(1, 16):
        try:
            r = CLIENT.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=20000
            )
            return r.choices[0].message.content
        except Exception as e:
            s = str(e)
            transient = (
                "429" in s or "500" in s or "502" in s or "503" in s or "504" in s or "Provider returned error" in s
            )
            if not transient:
                raise
            time.sleep(min(60, 6 * a))
    raise RuntimeError("model call failed after 15 attempts")


def main():
    raw = (SRC / SOURCE_FILE).read_text(encoding="utf-8", errors="replace")
    body = html_to_text(raw)
    focus = THEME or "key events, characters, divine actions, and decisions"
    prompt = (
        "You are a research evidence collector. Below is the full text of source '"
        f"{SOURCE_FILE}' (the Odyssey, A.T. Murray translation). Extract 10-16 claim-level evidence items about the "
        f"book's {focus}. Return ONLY a JSON object, "
        "no prose, matching exactly: "
        '{"claims":[{"claim":"...","source_file":"' + SOURCE_FILE + '","locator":"...",'
        '"excerpt":"...","stance":"supports","confidence":0.9,"uncertainty":"..."}],'
        '"conflicts":[],"coverage_notes":[]}\n'
        "RULES: excerpt must be a verbatim, exact substring of the provided text (no ellipses inside "
        "excerpts, no rewording). locator = book + line/section reference. stance = the claim's stance "
        "toward the epic's themes (supports/qualifies/contradicts). Do not use code fences.\n\nBOOK TEXT:\n" + body
    )
    parsed = None
    last_err = None
    for attempt in range(1, 4):
        text = call_model(
            prompt
            + (
                f"\n\nNOTE: previous reply was invalid ({last_err}). Return only the JSON object."
                if attempt > 1
                else ""
            )
        )
        try:
            parsed = extract_json(text)
            d = defects(parsed)
            if not d:
                break
            last_err = "; ".join(d)
        except Exception as e:
            last_err = f"unparseable: {str(e)[:80]}"
    if parsed is None or defects(parsed):
        raise RuntimeError(f"no valid packet after 3 model attempts: {last_err}")
    pkt_dir = RUN / "evidence"
    pkt_dir.mkdir(parents=True, exist_ok=True)
    (pkt_dir / f"worker-book-{BOOK}.json").write_text(
        json.dumps({"worker": f"book-{BOOK}", "attempts": [{"attempt": 1, "ok": True}], "parsed": parsed}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"OK book-{BOOK}: {len(parsed['claims'])} claims")


if __name__ == "__main__":
    main()
