"""Opt-in ONE live subscription request; not collected by pytest or run by CI."""

import argparse
import json
from pathlib import Path

from research_fabric import execution as ex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly spend subscription quota")
    parser.add_argument("--run-root", type=Path, required=True, help="new private directory outside any KB")
    parser.add_argument("--model", choices=["gpt-5", "gpt-6-astra"], required=True)
    parser.add_argument("--effort", choices=["low", "medium", "high"], default="low")
    args = parser.parse_args()
    if not args.live:
        parser.error("no request made: --live is required")
    if args.run_root.exists():
        parser.error("use a new run-root; never reset an existing journal")
    profile = {"provider": "chatgpt", "auth": "oauth", "model": args.model, "effort": args.effort}
    config = ex.resolve(
        override={
            "subscription_only": True,
            "roles": {"extraction": profile, "repair": profile},
            "budget": {"requests": 1, "attempts": 1, "output_tokens": 1000},
        },
        environ={},
    )
    ex.configure(args.run_root, config)
    try:
        result = ex.Session(args.run_root, "extraction", "subscription-smoke", []).call(
            [{"role": "user", "content": 'Reply with only this JSON: {"ok":true}'}], validate=json.loads
        )
        if result != {"ok": True}:
            raise RuntimeError("unexpected smoke response")
    finally:
        print(json.dumps(ex.history(args.run_root), indent=2))


if __name__ == "__main__":
    main()
