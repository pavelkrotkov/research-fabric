"""Importing the production engine must not start a run or require host services."""

import os
import subprocess
import sys
from pathlib import Path


def test_import_without_cao_credentials_or_host_state(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    script = r"""
import os
import sys

class NoHostModules:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"cao_workflow", "openkb"}:
            raise AssertionError("engine import requested host service: " + fullname)

sys.meta_path.insert(0, NoHostModules())
sys.argv = ["import-only"]

def audit(event, args):
    if event in {"subprocess.Popen", "os.system", "socket.connect", "os.mkdir"}:
        raise AssertionError("engine import performed run work: " + event)
    if event == "open":
        path, mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise AssertionError("engine import wrote a file: " + str(path))
        if isinstance(path, str) and (path.endswith(".env") or "/.hermes/" in path):
            raise AssertionError("engine import read credentials: " + path)

sys.addaudithook(audit)
import research_fabric.engine
assert "cao_workflow" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(source)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
