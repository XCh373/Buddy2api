import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for the offline Vue component mock")
def test_settings_component_load_save_reset_and_preview():
    repository = Path(__file__).parents[1]
    harness = Path(__file__).with_name("_settings_component_test.js")
    result = subprocess.run(
        [shutil.which("node"), str(harness)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
