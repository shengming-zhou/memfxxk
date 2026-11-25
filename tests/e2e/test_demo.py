from pathlib import Path
from runpy import run_path

import pytest

from memfxxk.client import MemfxxkClient

pytestmark = pytest.mark.integration


def test_demo_script_through_sdk(sdk: MemfxxkClient, capsys: pytest.CaptureFixture[str]) -> None:
    script = Path(__file__).resolve().parents[2] / "examples" / "demo_dev_agent.py"
    run = run_path(str(script))
    bundle = run["run_demo"](sdk)
    output = capsys.readouterr().out
    assert "Restored plan" in output
    assert "linked evidence" in output
    assert "Release policy v2 loaded" in output
    assert "smoke test" in bundle.rendered
