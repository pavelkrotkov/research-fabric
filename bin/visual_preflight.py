#!/usr/bin/env python3
"""Opt-in native OAuth visual inspection; no image/token payloads are logged."""

import argparse
import json
import os
from pathlib import Path

os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"

from research_fabric.visual_preflight import preflight  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import litellm

    litellm.set_verbose = False
    litellm.suppress_debug_info = True
    litellm.turn_off_message_logging = True
    result = preflight(args.source_root, json.loads(args.spec.read_text()), args.output)
    print(json.dumps({"record": str(args.output), "identity": result["identity"], "unresolved": result["unresolved"]}))
