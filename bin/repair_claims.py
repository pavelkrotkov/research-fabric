"""Claim-level excerpt repair: re-ground only the claims the grounding gate rejected.

For each failed claim, show ox-alpha its claim + excerpt + the book text and ask for
the exact verbatim span. Writes repaired packets in-place (attempt-2 recorded).
Usage: repair_claims.py <run_root> <source_dir> <grounding_report.txt>
"""
import sys, os, json, re, time, pathlib
from openai import OpenAI

RUN = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
REPORT = pathlib.Path(sys.argv[3])

_key = os.environ.get("OPENROUTER_API_KEY")
if not _key:
    for line in pathlib.Path.home().joinpath(".hermes", ".env").read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            _key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
CLIENT = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=_key)


def call_model(prompt):
    for a in range(1, 16):
        try:
            r = CLIENT.chat.completions.create(model="stealth/ox-alpha",
                messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=20000)
            return r.choices[0].message.content
        except Exception as e:
            s = str(e)
            if "429" in s or "Provider returned error" in s or any(f"5{x}" in s[:4] for x in "01234"):
                time.sleep(min(60, 6 * a))
            else:
                raise
    raise RuntimeError("model call failed after 15 attempts")


def extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t); t = t.rsplit("```", 1)[0]
    i = t.find('{"')
    if i > 0: t = t[i:]
    return json.loads(t.strip())

# 1. Parse the grounding report for failed claim ids
failed_ids = []
for line in REPORT.read_text().splitlines():
    m = re.match(r"(c-book-\d+-\d+): excerpt not found", line.strip())
    if m:
        failed_ids.append(m.group(1))
print(f"failed claims: {len(failed_ids)}")

# 2. Group by book; load each book's packet and text once
by_book = {}
for cid in failed_ids:
    b = int(re.search(r"c-book-(\d+)-", cid).group(1))
    by_book.setdefault(b, []).append(cid)

def html_to_text_local(raw):
    from html.parser import HTMLParser
    class T(HTMLParser):
        def __init__(s):
            super().__init__(); s.parts=[]; s.skip=0
        def handle_starttag(s,t,a):
            if t in ('script','style'): s.skip+=1
            if t in ('br','p','div','li'): s.parts.append('\n')
        def handle_endtag(s,t):
            if t in ('script','style'): s.skip=max(0,s.skip-1)
        def handle_data(s,d):
            if not s.skip: s.parts.append(d)
    tp=T(); tp.feed(raw)
    import html as h
    text=h.unescape(''.join(tp.parts))
    text=re.sub(r'[ \t]+',' ',text); text=re.sub(r'\n\s*\n+','\n\n',text)
    i=text.find('Homer')
    return text[i:] if i>0 else text

repaired = 0
dropped = 0
for b, cids in sorted(by_book.items()):
    pkt_path = RUN / "evidence" / f"worker-book-{b}.json"
    pkt = json.loads(pkt_path.read_text())
    claims = pkt["parsed"]["claims"]
    body = html_to_text_local((SRC / f"odyssey-book-{b}.html").read_text(encoding="utf-8", errors="replace"))
    for cid in cids:
        idx = int(cid.rsplit("-", 1)[1]) - 1
        if idx >= len(claims): continue
        c = claims[idx]
        prompt = (
            f"You are repairing one evidence claim about Odyssey Book {b}. The claim's "
            f"\"excerpt\" was rejected because it is not a verbatim substring of the book text.\n\n"
            f'CLAIM: "{c["claim"]}"\n'
            f'REJECTED EXCERPT: "{c["excerpt"]}"\n\n'
            "Find the passage in BOOK TEXT that this excerpt attempted to quote. Reply with ONLY "
            'a JSON object: {"excerpt": "<exact verbatim substring of BOOK TEXT>", "found": true}\n'
            "The excerpt must be copied character-for-character from BOOK TEXT (you may choose a "
            'shorter span). If the passage does not exist in BOOK TEXT, reply {"found": false}.\n\n'
            f"BOOK TEXT:\n{body}"
        )
        try:
            out = extract_json(call_model(prompt))
        except Exception as e:
            print(f"{cid}: model error {e}")
            continue
        if not out.get("found"):
            # Drop the ungroundable claim
            claims.remove(c)
            dropped += 1
            print(f"{cid}: dropped (passage not found)")
            continue
        new_ex = out.get("excerpt", "")
        if new_ex and new_ex in body:
            c["excerpt"] = new_ex
            repaired += 1
            print(f"{cid}: repaired ({len(new_ex)} chars)")
        else:
            claims.remove(c); dropped += 1
            print(f"{cid}: dropped (model excerpt still not verbatim)")
    pkt["attempts"].append({"attempt": "excerpt-repair", "repaired": True})
    pkt_path.write_text(json.dumps(pkt, indent=2) + "\n", encoding="utf-8")

print(f"\ndone: repaired={repaired}, dropped={dropped}")
