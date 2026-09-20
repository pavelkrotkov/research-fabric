"""Native child executable, replacing only external model I/O for offline tests."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from openkb.agent import compiler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from research_fabric.compilation import main

# First process fails after one page; next process must really recompile both.
counter = Path(sys.argv.pop(1))
calls = json.loads(counter.read_text()) if counter.exists() else {"attempts": 0, "pages": 0}
calls["attempts"] += 1
counter.write_text(json.dumps(calls))


def response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=5),
    )


def citations(messages):
    system = messages[0]["content"]
    if "## Frozen research source citations" not in system:
        return ""
    records = [json.loads(line) for line in system.splitlines() if line.startswith('{"section":')]
    assert records and all(
        row["target"] in system and row["original_bytes"][1] > row["original_bytes"][0] for row in records
    )
    calls["citation_mappings"] = records
    counter.write_text(json.dumps(calls))
    return "\n".join(f"[Original section](../{row['target']})" for row in records)


def completion(model, messages, **kwargs):
    mapped = citations(messages)
    prompt = messages[-1]["content"]
    if isinstance(prompt, list):
        prompt = prompt[0]["text"]
    if "decide how to update" in prompt:
        return response(json.dumps({"update": [{"name": "one"}, {"name": "two"}]}))
    if "Rewrite the summary" in prompt:
        raise TimeoutError("Optional summary rewrite")
    return response(json.dumps({"description": "Synthetic source", "content": "Initial summary\n" + mapped}))


async def acompletion(model, messages, **kwargs):
    await asyncio.sleep(0.001)
    mapped = citations(messages)
    calls["pages"] += 1
    counter.write_text(json.dumps(calls))
    if calls["attempts"] == 1 and "for: two" in messages[-1]["content"]:
        raise TimeoutError("Required page timed out")
    return response(json.dumps({"description": "Concept", "content": "Native generated body\n" + mapped}))


compiler.litellm.completion = completion
compiler.litellm.acompletion = acompletion
raise SystemExit(main())
