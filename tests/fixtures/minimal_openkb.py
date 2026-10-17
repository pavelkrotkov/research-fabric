"""Real minimal child/native compiler; synthetic HTTP only, network prohibited."""

import json
import socket
import sys
import time
from pathlib import Path

import httpx
from litellm.llms.chatgpt.authenticator import Authenticator
from openkb.agent import compiler

from research_fabric.minimal import worker

run, source, result = map(Path, sys.argv[-3:])
fault = (run / "fault").read_text().strip() if (run / "fault").exists() else ""
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Network prohibited"))
Authenticator._get_expires_at = lambda self, token: time.time() + 3600

if fault == "timeout":
    time.sleep(20)
if fault in ("missing-write", "summary-write"):
    original = compiler.atomic_write_text

    def write(path, text, *args, **kwargs):
        if fault == "missing-write" and Path(path).name == "two.md":
            return
        if fault == "summary-write" and Path(path).parent.name == "summaries" and "[[concepts/one]]" in text:
            return
        original(path, text, *args, **kwargs)

    compiler.atomic_write_text = write


def reply(request):
    body = json.loads(request.content)
    prompt = json.dumps(body["input"], ensure_ascii=False)
    if "decide how to update" in prompt:
        text = "malformed" if fault == "malformed" else json.dumps({"update": [{"name": "one"}, {"name": "two"}]})
    elif "Rewrite the summary" in prompt:
        text = "Complete synthetic summary"
    elif "for: one" in prompt or "for: two" in prompt:
        content = "Native generated body" + ("\n[Missing](../nonexistent.md)\n" if fault == "broken-link" else "")
        text = json.dumps({"description": "Synthetic concept", "content": content})
    else:
        text = json.dumps({"description": "Synthetic document", "content": "Initial synthetic summary"})
    if fault == "quota":
        return httpx.Response(429, json={"error": "PRIVATE-SECRET"}, request=request)
    payload = {
        "id": "offline",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": [
            {
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    }
    return httpx.Response(
        200,
        text="data: " + json.dumps({"type": "response.completed", "response": payload}) + "\n\n",
        headers={"content-type": "text/event-stream"},
        request=request,
    )


httpx.Client.send = lambda self, request, **kwargs: reply(request)


async def asynchronous(self, request, **kwargs):
    return reply(request)


httpx.AsyncClient.send = asynchronous
raise SystemExit(worker(run, source, result))
