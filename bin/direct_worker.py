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
    run, source_root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    if sys.argv[3] == "--reading":
        reading_id = sys.argv[4]
        project_path = pathlib.Path(sys.argv[5])
        from research_fabric._source_assets import safe_path
        from research_fabric.core import load_project
        from research_fabric.sources import ReadingPlan, source_provenance

        project = load_project(project_path.parent, project_path.stem)
        plan = ReadingPlan.load(
            run / "reading-plan.json",
            {name: safe_path(source_root, name) for name in project["reading"]["sources"]},
            project["reading"],
        )
        paths = [source_root / name for name in plan.source_names(reading_id)]
        prompt = (
            "Extract claim-level evidence for this research question: " + plan.data["question"] + "\n"
            "Read only the assigned primary/context passages. Return ONLY JSON with claims, conflicts, coverage_notes. "
            "Claims require claim, source_file, locator, excerpt, stance (supports/qualifies/contradicts), confidence. "
            "Copy source_file exactly, including spaces. Excerpts must occur exactly once within one assigned section; "
            "preserve newlines. Generated labels are not evidence. Context qualifies interpretation but does not "
            "count as independent primary support. Do not invent authorship.\n" + plan.input(reading_id)
        )
        worker = reading_id
        provenance = source_provenance(paths, source_root=source_root)

        def validate(text):
            parsed = validated_packet(text)
            for claim in parsed["claims"]:
                claim["reading"], _ = plan.claim(reading_id, claim)
            return parsed

    else:
        BOOK = int(sys.argv[3])
        SOURCE_FILE = sys.argv[4] if len(sys.argv) > 4 else f"odyssey-book-{BOOK}.html"
        THEME = sys.argv[5] if len(sys.argv) > 5 else None
        worker = f"book-{BOOK}"
        paths = [source_root / SOURCE_FILE]
        source = source_root / SOURCE_FILE
        representation = representation_for(source)
        body = representation.text
        focus = THEME or "key events, characters, divine actions, and decisions"
        prompt = (
            "You are a research evidence collector. Below is the full text of source '"
            f"{SOURCE_FILE}' (the Odyssey, A.T. Murray translation). Extract 10-16 evidence items about the "
            f"book's {focus}. Return ONLY a JSON object, "
            "no prose, matching exactly: "
            '{"claims":[{"claim":"...","source_file":"' + SOURCE_FILE + '","locator":"...",'
            '"excerpt":"...","stance":"supports","confidence":0.9,"uncertainty":"..."}],'
            '"conflicts":[],"coverage_notes":[]}\n'
            "RULES: excerpt must be a verbatim, exact substring of the provided text (no ellipses inside "
            "excerpts, no rewording). locator = book + line/section reference. stance = the claim's stance "
            "toward the epic's themes (supports/qualifies/contradicts). Do not use code fences.\n\nBOOK TEXT:\n" + body
        )
        provenance = [source_attestation(representation)]
        validate = validated_packet
    from research_fabric.claims import atomic_write_json

    session = worker_session(run, "extraction", worker, paths)
    parsed = session.call([{"role": "user", "content": prompt}], validate=validate)
    atomic_write_json(
        run / "evidence" / f"worker-{worker}.json",
        {
            "worker": worker,
            "execution": session.records(),
            "source_provenance": provenance,
            "parsed": parsed,
        },
    )
    print(f"OK {worker}: {len(parsed['claims'])} claims")


if __name__ == "__main__":
    main()
