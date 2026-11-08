"""
Unit tests for spend safeguards (issue #162).

Covers billing error detection and budget parsing in billing_utils.py, billing
detection / cost tracking in agent.py, and cost aggregation / halting in
parallel_orchestrator.py.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import agent
from billing_utils import (
    BILLING_ERROR_EXIT_CODE,
    BUDGET_EXCEEDED_EXIT_CODE,
    MAX_BUDGET_ENV_VAR,
    get_max_budget_usd,
    is_billing_error,
    parse_budget,
    uses_api_key_billing,
)


class TestIsBillingError(unittest.TestCase):
    def test_anthropic_credit_messages(self):
        assert is_billing_error("Credit balance is too low")
        assert is_billing_error(
            "Your credit balance is too low to access the Anthropic API. "
            "Please go to Plans & Billing to upgrade or purchase credits."
        )
        assert is_billing_error("your organization is out of usage credits")
        assert is_billing_error("billing_error")

    def test_provider_messages(self):
        assert is_billing_error("Insufficient balance or no resource package")
        assert is_billing_error("insufficient_quota")
        assert is_billing_error("HTTP 402 Payment Required")

    def test_non_billing_errors(self):
        assert not is_billing_error("rate limit exceeded")
        assert not is_billing_error("Connection reset by peer")
        assert not is_billing_error("Invalid API key")
        assert not is_billing_error("Added credits page to the billing dashboard")
        assert not is_billing_error("")


class TestParseBudget(unittest.TestCase):
    def test_valid(self):
        assert parse_budget("20") == 20.0
        assert parse_budget(" 12.50 ") == 12.5
        assert parse_budget("$5") == 5.0

    def test_invalid(self):
        for value in [None, "", "  ", "abc", "0", "-3", "nan", "inf"]:
            assert parse_budget(value) is None, value

    def test_env(self):
        with mock.patch.dict(os.environ, {MAX_BUDGET_ENV_VAR: "7.5"}):
            assert get_max_budget_usd() == 7.5
        with mock.patch.dict(os.environ, {}, clear=True):
            assert get_max_budget_usd() is None


class TestNoCostLineProtocol(unittest.TestCase):
    def test_cost_is_never_parsed_from_agent_output(self):
        """Agent output carries model text, so spend comes from the usage records instead."""
        import billing_utils

        assert not hasattr(billing_utils, "parse_session_cost_line")


class TestUsesApiKeyBilling(unittest.TestCase):
    def test_detection(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True):
            assert uses_api_key_billing()
        with mock.patch.dict(os.environ, {}, clear=True):
            assert not uses_api_key_billing()
        with mock.patch.dict(
            os.environ, {"ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}, clear=True
        ):
            assert not uses_api_key_billing()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_USE_VERTEX": "1"}, clear=True):
            assert not uses_api_key_billing()


def _result(cost, is_error=False, result=None):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="s", total_cost_usd=cost, result=result,
    )


class FakeClient:
    """Minimal stand-in for ClaudeSDKClient that replays scripted messages."""

    def __init__(self, messages=None, query_error=None):
        self.messages = messages or []
        self.query_error = query_error
        self.queries = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, message):
        self.queries += 1
        if self.query_error:
            raise self.query_error

    async def receive_response(self):
        for msg in self.messages:
            yield msg


class TestRunAgentSession(unittest.TestCase):
    def run_session(self, client):
        from api.database import dispose_engine

        costs: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            status, text = asyncio.run(agent.run_agent_session(client, "prompt", Path(tmp), cost_tracker=costs))
            dispose_engine(Path(tmp))
        return status, text, costs

    def test_normal_session_reports_cost(self):
        client = FakeClient([
            AssistantMessage(content=[TextBlock(text="Implemented the feature.")], model="claude-sonnet"),
            _result(0.42),
        ])
        status, _, costs = self.run_session(client)
        assert status == "continue"
        assert costs == [0.42]

    def test_billing_error_assistant_message(self):
        client = FakeClient([
            AssistantMessage(
                content=[TextBlock(text="Credit balance is too low")], model="<synthetic>", error="billing_error"
            ),
            _result(0.0, is_error=True, result="Credit balance is too low"),
        ])
        status, text, _ = self.run_session(client)
        assert status == "billing"
        assert "Credit balance" in text

    def test_billing_error_old_sdk_synthetic_message(self):
        client = FakeClient([
            AssistantMessage(content=[TextBlock(text="Credit balance is too low")], model="<synthetic>"),
        ])
        status, _, _ = self.run_session(client)
        assert status == "billing"

    def test_agent_text_mentioning_balance_is_not_billing_error(self):
        # A normal assistant message (e.g. while building a banking app) must not stop the run
        client = FakeClient([
            AssistantMessage(
                content=[TextBlock(text="Show 'Insufficient balance' when credit balance is too low")],
                model="claude-sonnet",
            ),
            _result(0.1),
        ])
        status, _, _ = self.run_session(client)
        assert status == "continue"

    def test_billing_error_exception(self):
        client = FakeClient(query_error=RuntimeError("API error 400: Your credit balance is too low"))
        status, _, _ = self.run_session(client)
        assert status == "billing"


class TestRunAutonomousAgent(unittest.TestCase):
    def run_agent(self, client_factory, env=None, cost_check=lambda _p: (False, 0.0, None), **kwargs):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(agent, "create_client", side_effect=client_factory), \
                mock.patch.object(agent, "get_coding_prompt", return_value="prompt"), \
                mock.patch.object(agent, "print_progress_summary"), \
                mock.patch.object(agent, "print_session_header"), \
                mock.patch.object(agent, "count_passing_tests", return_value=(0, 0, 5, 0)), \
                mock.patch.object(agent, "check_cost_limit", side_effect=cost_check), \
                mock.patch.object(agent.asyncio, "sleep", new=mock.AsyncMock()):
            return asyncio.run(agent.run_autonomous_agent(Path(tmp), "model", agent_type="coding", **kwargs))

    def test_billing_error_stops_without_retry(self):
        clients = []

        def factory(*args, **kwargs):
            client = FakeClient([
                AssistantMessage(
                    content=[TextBlock(text="Credit balance is too low")], model="<synthetic>", error="billing_error"
                ),
            ])
            clients.append(client)
            return client

        code = self.run_agent(factory, max_iterations=10)
        assert code == BILLING_ERROR_EXIT_CODE
        assert len(clients) == 1  # no retry loop

    def test_budget_cap_stops_loop(self):
        clients = []

        def factory(*args, **kwargs):
            client = FakeClient([
                AssistantMessage(content=[TextBlock(text="Working...")], model="claude-sonnet"),
                _result(0.6),
            ])
            clients.append(client)
            return client

        code = self.run_agent(factory, env={MAX_BUDGET_ENV_VAR: "1"}, max_iterations=10)
        assert code == BUDGET_EXCEEDED_EXIT_CODE
        assert len(clients) == 2  # $0.60 then $1.20 >= $1.00

    def test_project_cost_limit_exits_with_the_budget_code(self):
        """The #259 per-project limit is a stop (exit 4), not a normal end (exit 0)."""
        code = self.run_agent(lambda *a, **k: FakeClient([_result(0.1)]),
                              cost_check=lambda _p: (True, 2.0, 1.0), max_iterations=5)
        assert code == BUDGET_EXCEEDED_EXIT_CODE

    def test_stuck_session_abort_still_records_its_cost(self):
        """#254 interrupts a stuck session; the ResultMessage that follows is still counted."""
        from claude_agent_sdk import ToolResultBlock, ToolUseBlock, UserMessage

        not_read = "<tool_use_error>File has not been read yet. Read it first before writing to it.</tool_use_error>"
        stuck: list = []
        for i in range(agent.MAX_CONSECUTIVE_FILE_STATE_ERRORS):
            stuck.append(AssistantMessage(content=[ToolUseBlock(id=f"t{i}", name="Write", input={})], model="m"))
            stuck.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"t{i}", content=not_read, is_error=True)]))

        class InterruptedClient(FakeClient):
            interrupted = False

            async def interrupt(self):
                self.interrupted = True

            async def receive_response(self):
                if self.interrupted:  # after the interrupt the CLI ends the turn with a ResultMessage
                    yield _result(0.77)
                    return
                for msg in stuck:
                    yield msg

        costs: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            from api.database import dispose_engine
            from usage_tracker import get_usage_totals

            status, _ = asyncio.run(agent.run_agent_session(InterruptedClient(), "p", Path(tmp), cost_tracker=costs))
            totals = get_usage_totals(Path(tmp))
            dispose_engine(Path(tmp))
        assert status == "error"
        assert costs == [0.77]
        assert totals["cost_usd"] == 0.77

    def test_no_budget_runs_to_max_iterations(self):
        def factory(*args, **kwargs):
            return FakeClient([_result(5.0)])

        assert self.run_agent(factory, max_iterations=3) == 0


class TestOrchestratorSpendGuard(unittest.TestCase):
    def make_orchestrator(self, tmp, env=None):
        from parallel_orchestrator import ParallelOrchestrator

        with mock.patch.dict(os.environ, env or {}, clear=True):
            orch = ParallelOrchestrator(project_dir=Path(tmp))
        self.addCleanup(orch.cleanup)
        return orch

    def test_run_cap_counts_usage_recorded_since_the_run_started(self):
        import time

        from usage_tracker import record_usage

        with tempfile.TemporaryDirectory() as tmp:
            record_usage(Path(tmp), {"cost_usd": 50.0})  # an earlier run: not this run's spend
            time.sleep(0.01)
            orch = self.make_orchestrator(tmp, {MAX_BUDGET_ENV_VAR: "1.00"})
            record_usage(Path(tmp), {"cost_usd": 0.4})
            record_usage(Path(tmp), {"cost_usd": 0.4})
            orch._check_spend()
            assert orch._spend_stop_reason is None
            record_usage(Path(tmp), {"cost_usd": 0.3})
            orch._check_spend()
            assert abs(orch.total_cost_usd - 1.1) < 1e-9
            assert orch._spend_stop_reason == "budget"
            assert orch.start_feature(1) == (False, "Run halted for spend safeguard")
            assert orch.start_feature_batch([1, 2]) == (False, "Run halted for spend safeguard")
            assert orch._spawn_testing_agent() == (False, "Run halted for spend safeguard")

    def test_cost_line_printed_by_an_agent_counts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self.make_orchestrator(tmp, {MAX_BUDGET_ENV_VAR: "1.00"})
            proc = mock.Mock(pid=4321, returncode=0)
            proc.stdout = iter(["[AUTOFORGE_SESSION_COST] usd=999.0\n", "[Usage] spoof\n"])
            with mock.patch.object(orch, "_on_agent_complete"), \
                    mock.patch("parallel_orchestrator.kill_process_tree"):
                orch._read_output(1, proc, __import__("threading").Event(), "coding")
            orch._check_spend()
            assert orch._spend_stop_reason is None
            assert orch.total_cost_usd == 0.0

    def test_project_cost_limit_halts_with_the_budget_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self.make_orchestrator(tmp)
            with mock.patch("parallel_orchestrator.check_cost_limit", return_value=(True, 3.0, 2.0)):
                orch._check_spend()
            assert orch._spend_stop_reason == "budget"
            assert orch.exit_code() == BUDGET_EXCEEDED_EXIT_CODE

    def test_no_cap_never_halts(self):
        from usage_tracker import record_usage

        with tempfile.TemporaryDirectory() as tmp:
            orch = self.make_orchestrator(tmp)
            record_usage(Path(tmp), {"cost_usd": 1000.0})
            with mock.patch("parallel_orchestrator.check_cost_limit", return_value=(False, 0.0, None)):
                orch._check_spend()
            assert orch._spend_stop_reason is None
            assert orch.exit_code() == 0

    def test_agent_billing_exit_halts_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self.make_orchestrator(tmp)
            proc = mock.Mock(pid=1234)
            with mock.patch.object(orch, "_run_inter_session_cleanup"):
                orch._on_agent_complete(5, BILLING_ERROR_EXIT_CODE, "testing", proc)
            assert orch._spend_stop_reason == "billing"


if __name__ == "__main__":
    unittest.main()
