import os
import subprocess
import sys


def test_cli_emits_chinese_help_on_a_western_windows_locale():
    result = subprocess.run(
        [sys.executable, "-m", "tumo_engine.cli", "--help"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "图模一致" in result.stdout.decode("utf-8")
