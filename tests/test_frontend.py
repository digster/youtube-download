"""Include the dependency-free browser polling regressions in the main suite."""

import shutil
import subprocess
from pathlib import Path


def test_frontend_polling():
    node = shutil.which("node")
    assert node, "Install Node.js to run the frontend polling tests."
    test_file = Path(__file__).parent / "frontend" / "polling.test.cjs"
    result = subprocess.run(
        [node, "--test", str(test_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
