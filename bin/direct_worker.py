"""Direct-API evidence worker (OpenRouter z-ai/glm-5.3-flash) — openai SDK client.

Reads one source through the shared adapter, makes a chat completion with
robust 429/5xx backoff + parse retry, and writes a validated claim packet. No
tmux, no screen scraping, no Codex quota.

Run with the hermes venv python (has the openai SDK):
  <hermes-venv>/bin/python direct_worker.py <run_root> <source_dir> \
     <book> <source_filename> [theme]
Writes <run_root>/evidence/worker-book-<N>.json
"""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.execution import InvalidOutput, worker_session
from research_fabric.sources import representation_for, source_attestation  # noqa: E402

BOOK = int(sys.argv[3])
RUN = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
SOURCE_FILE = sys.argv[4] if len(sys.argv) > 4 else f"odyssey-book-{BOOK}.html"
THEME = sys.argv[5] if len(sys.argv) > 5 else None
SESSION = worker_session(RUN, "extraction", f"book-{BOOK}", [SRC / SOURCE_FILE])


def extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    # z-ai/glm-5.3-flash may add prose around the object; locate the claims opener.
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
            if not isinstance(v, str) or not v.strip() or v.strip() in ("...", "…", "TBD"):
                out.append(f"claim {i} field {f} placeholder/missing")
    return out


def validated_packet(text):
    parsed = extract_json(text)
    if defects(parsed):
        raise InvalidOutput()
    return parsed


def main():
    source = SRC / SOURCE_FILE
    representation = representation_for(source)
    body = representation.text
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
    parsed = SESSION.call([{"role": "user", "content": prompt}], validate=validated_packet)
    pkt_dir = RUN / "evidence"
    pkt_dir.mkdir(parents=True, exist_ok=True)
    (pkt_dir / f"worker-book-{BOOK}.json").write_text(
        json.dumps(
            {
                "worker": f"book-{BOOK}",
                "attempts": SESSION.records(),
                "execution": SESSION.records(),
                "source_provenance": [source_attestation(representation)],
                "parsed": parsed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"OK book-{BOOK}: {len(parsed['claims'])} claims")


if __name__ == "__main__":
    main()
