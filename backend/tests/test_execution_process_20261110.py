import os
import subprocess
import sys
from pathlib import Path


def test_real_process_deadline_cancel_graceful_stop_crash_and_restart(tmp_path):
    env = {**os.environ, "INSPECTOR_DATABASE_URL": f"sqlite:///{tmp_path / 'execution.db'}",
           "INSPECTOR_ENV": "test", "INSPECTOR_DEMO_SEED": "false", "INSPECTOR_REDIS_URL": "",
           "INSPECTOR_HCS_MOCK_URL": "", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("execution_process_probe.py"))],
                            env=env, capture_output=True, text=True, timeout=100)
    (tmp_path / "process-probe.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-16000:]
    assert "PROCESS_PROBE=" in result.stdout
