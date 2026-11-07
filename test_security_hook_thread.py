"""
The bash security hook validates in a worker thread (#38), with the same verdicts.

Run with: python -m pytest test_security_hook_thread.py
"""

import asyncio
import threading

import security
from security import bash_security_hook, validate_bash_tool_use


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_hook_runs_validation_off_the_event_loop(monkeypatch, tmp_path):
    seen: list[str] = []
    real = security.validate_bash_tool_use

    def recording(input_data, context=None):
        seen.append(threading.current_thread().name)
        return real(input_data, context)

    monkeypatch.setattr(security, "validate_bash_tool_use", recording)

    async def call():
        return await bash_security_hook(_bash("ls"), context={"project_dir": str(tmp_path)}), \
            threading.current_thread().name

    result, loop_thread = asyncio.run(call())

    assert result == {}
    assert seen and seen[0] != loop_thread


def test_hook_and_sync_core_agree(tmp_path):
    context = {"project_dir": str(tmp_path)}
    for command in ("npm run build", "pkill node && pkill -9 sshd", 'bash -c "sudo reboot"'):
        assert asyncio.run(bash_security_hook(_bash(command), context=context)) == \
            validate_bash_tool_use(_bash(command), context)
    assert validate_bash_tool_use(_bash("pkill node && pkill -9 sshd"), context)["decision"] == "block"
