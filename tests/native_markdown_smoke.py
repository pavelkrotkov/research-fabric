"""Optional smoke: run with Python + OpenKB 0.4.5 installed.

Uses a disposable KB and scripted model responses; native conversion, compiler,
summary/index writing and registry updates remain real. This does not exercise
CAO orchestration, publication gates or READY_FOR_REVIEW.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import litellm
import openkb.config as config
from click.testing import CliRunner
from openkb.cli import add_single_file, cli

with tempfile.TemporaryDirectory(prefix="markdown-native-") as directory:
    root = Path(directory)
    os.chdir(root)
    config.GLOBAL_CONFIG_DIR = root / "global"
    config.GLOBAL_CONFIG_PATH = root / "global/global.yaml"
    config.GLOBAL_CONFIG_LOCK_PATH = root / "global/global.lock"
    result = CliRunner().invoke(cli, ["init", "--model", "openai/test", "--language", "en"], input="\n")
    assert result.exit_code == 0, (result.output, repr(result.exception))
    source = root / "sample.md"
    source.write_text("# Measurement\n\nThe sample temperature is $T = 21$ degrees.\n")
    replies = iter(
        [
            json.dumps(
                {
                    "description": "Synthetic measurement",
                    "content": "# Measurement\n\nThe sample temperature is $T = 21$ degrees.",
                }
            ),
            json.dumps(
                {
                    "concepts": {"create": [], "update": [], "related": []},
                    "entities": {"create": [], "update": [], "related": []},
                }
            ),
        ]
    )

    def completion(**kwargs):
        assert "The sample temperature is $T = 21$ degrees." in str(kwargs["messages"])
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": next(replies)}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    with (
        patch.dict(os.environ, {"OPENAI_API_KEY": "scripted-no-network"}),
        patch.object(litellm, "completion", completion),
    ):
        assert add_single_file(source, root) == "added"
    assert (root / "wiki/sources/sample.md").is_file()
    summary = (root / "wiki/summaries/sample.md").read_text()
    assert "$T = 21$" in summary
    registry = json.loads((root / ".openkb/hashes.json").read_text())
    assert registry
    assert "sample" in (root / "wiki/index.md").read_text()
    assert next(replies, None) is None
    print("PASS: native Markdown conversion, compile_short_doc, summary/index and hash registry; scripted model only")
