"""
Agent Session Logic
===================

Core agent interaction functions for running autonomous coding sessions.
"""

import asyncio
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import (
    ClaudeSDKClient,
    MirrorErrorMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
)

# Fix Windows console encoding for Unicode characters (emoji, etc.)
# Without this, print() crashes when Claude outputs emoji like ✅
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from billing_utils import (
    BILLING_ERROR_EXIT_CODE,
    BILLING_ERROR_HELP,
    BUDGET_EXCEEDED_EXIT_CODE,
    MAX_BUDGET_ENV_VAR,
    get_max_budget_usd,
    is_billing_error,
    uses_api_key_billing,
)
from client import create_client
from progress import (
    count_passing_tests,
    has_features,
    print_progress_summary,
    print_session_header,
)
from prompts import (
    copy_spec_to_project,
    get_auto_improve_prompt,
    get_batch_feature_prompt,
    get_coding_prompt,
    get_initializer_prompt,
    get_single_feature_prompt,
    get_testing_prompt,
    with_file_editing_rules,
    with_project_knowledge,
    with_project_skills,
)
from rate_limit_utils import (
    calculate_error_backoff,
    calculate_rate_limit_backoff,
    clamp_retry_delay,
    format_rate_limit_marker,
    is_rate_limit_error,
    parse_limit_reset,
    parse_retry_after,
)
from usage_tracker import (
    check_cost_limit,
    extract_usage,
    format_cost_limit_message,
    record_usage,
)

# Configuration
AUTO_CONTINUE_DELAY_SECONDS = 3
# How long to wait for the ResultMessage (cost) of a session interrupted by the stuck guard
RESULT_AFTER_INTERRUPT_TIMEOUT_SECONDS = 30

# Abort a session after this many consecutive Write/Edit "file state" errors
# (stale or missing Read) without a single successful write in between.
# Prevents an agent from burning tokens all night retrying the same edit (#30).
MAX_CONSECUTIVE_FILE_STATE_ERRORS = 6

FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

_FILE_STATE_ERROR_PATTERNS = (
    "file has not been read yet",
    "file has been unexpectedly modified",
    "file has been modified since read",
)


def is_file_state_error(error_text: str) -> bool:
    """Return True if a tool error is Claude Code's stale/missing-read guard."""
    lowered = error_text.lower()
    return any(pattern in lowered for pattern in _FILE_STATE_ERROR_PATTERNS)


def _print_system_message(msg: SystemMessage) -> None:
    """Log the system messages that matter for debugging; ignore the rest."""
    if isinstance(msg, TaskNotificationMessage):
        if msg.status != "completed":
            print(f"\n[Subagent {msg.status}] {msg.summary}", flush=True)
    elif isinstance(msg, MirrorErrorMessage):
        print(f"\n[Session mirror error] {msg.error}", flush=True)
    elif msg.subtype == "init":
        # The init message reports MCP server status. A server that failed to
        # start (e.g. feature DB init error) otherwise only shows up later as
        # confusing "tool not found" errors.
        for server in msg.data.get("mcp_servers") or []:
            status = server.get("status")
            if status != "connected":
                print(f"[MCP] Server '{server.get('name')}' is not connected (status: {status})", flush=True)


def _print_result_message(msg: ResultMessage) -> str:
    """
    Print a summary of a ResultMessage.

    Returns:
        Error text if the result is an error, otherwise an empty string.
    """
    summary = f"{msg.num_turns} turns, {msg.duration_ms / 1000:.1f}s"
    if msg.total_cost_usd is not None:
        summary += f", ${msg.total_cost_usd:.4f}"

    if not msg.is_error:
        print(f"\n[Result] {summary}", flush=True)
        return ""

    details = "; ".join(msg.errors or []) or msg.result or ""
    if msg.api_error_status:
        details = f"API status {msg.api_error_status}: {details}"
    error_text = f"[Result error: {msg.subtype}] {details}".rstrip()
    print(f"\n{error_text} ({summary})", flush=True)
    return error_text


def _record_session_usage(
    project_dir: Path,
    result_msg: ResultMessage,
    usage_context: Optional[dict[str, Any]],
    cost_tracker: Optional[list[float]] = None,
) -> None:
    """Store the session's token usage and cost (the cost is already in the [Result] line).

    The cost comes from the SDK's ResultMessage, never from text the model wrote.
    """
    usage = extract_usage(result_msg)
    record_usage(project_dir, usage, **(usage_context or {}))
    if cost_tracker is not None and result_msg.total_cost_usd is not None:
        cost_tracker.append(float(result_msg.total_cost_usd))
    print(
        f"[Usage] {usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens "
        f"(cache: {usage['cache_read_tokens']:,} read, {usage['cache_creation_tokens']:,} written)",
        flush=True,
    )


async def _receive_result_after_interrupt(
    client: ClaudeSDKClient, timeout: float = RESULT_AFTER_INTERRUPT_TIMEOUT_SECONDS
) -> Optional[ResultMessage]:
    """Read what the CLI sends after an interrupt until the turn's ResultMessage (or a timeout)."""

    async def read() -> Optional[ResultMessage]:
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                return msg
        return None

    try:
        return await asyncio.wait_for(read(), timeout)
    except asyncio.TimeoutError:
        print("   (no result after the interrupt; this session's cost is not recorded)", flush=True)
        return None


def _print_limit_warning(response: str, delay_seconds: int, reset_at: Optional[datetime]) -> None:
    """Print a prominent warning that Claude's rate/usage limit was reached."""
    # Show the line Claude actually sent (e.g. "Limit reached · resets 6pm (...)")
    limit_lines = [ln.strip() for ln in response.splitlines() if ln.strip() and is_rate_limit_error(ln)]
    detail = limit_lines[-1] if limit_lines else response.strip()[-300:]

    if reset_at is not None:
        when = reset_at.astimezone().strftime("%B %d, %Y at %I:%M %p %Z")
    else:
        when = (datetime.now().astimezone() + timedelta(seconds=delay_seconds)).strftime("%I:%M %p %Z")

    print("\n" + "!" * 70, flush=True)
    print("  WARNING: CLAUDE USAGE/RATE LIMIT REACHED", flush=True)
    print("!" * 70, flush=True)
    if detail:
        print(f"Claude said: {detail}", flush=True)
    print(f"Pausing new sessions for {delay_seconds}s (until ~{when}).", flush=True)
    print("!" * 70, flush=True)


async def run_agent_session(
    client: ClaudeSDKClient,
    message: str,
    project_dir: Path,
    usage_context: Optional[dict[str, Any]] = None,
    cost_tracker: Optional[list[float]] = None,
) -> tuple[str, str]:
    """
    Run a single agent session using Claude Agent SDK.

    Args:
        client: Claude SDK client
        message: The prompt to send
        project_dir: Project directory path
        usage_context: Extra fields (agent_type, feature_ids, model) stored with
            the session's token/cost usage record
        cost_tracker: Optional list; the session's reported cost (USD) is
            appended to it when the SDK provides one.

    Returns:
        (status, response_text) where status is:
        - "continue" if agent should continue working
        - "rate_limit" if a rate limit was hit (response_text is retry seconds or "unknown")
        - "billing" if the API reported exhausted credits (response_text is the error)
        - "error" if an error occurred
    """
    print("Sending prompt to Claude Agent SDK...\n")

    billing_error: Optional[str] = None

    try:
        # Send the query
        await client.query(message)

        # Collect response text and show tool use
        # Retry receive_response() on MessageParseError — the SDK raises this for
        # unknown CLI message types (e.g. "rate_limit_event") which kills the async
        # generator.  The subprocess is still alive so we restart to read remaining
        # messages from the buffered channel.
        response_text = ""
        result_msg: Any = None
        max_parse_retries = 50
        parse_retries = 0
        # Track Write/Edit calls so repeated stale-read errors can be detected
        write_tool_ids: set[str] = set()
        consecutive_file_state_errors = 0
        stuck_on_file_state = False
        while True:
            try:
                async for msg in client.receive_response():
                    msg_type = type(msg).__name__

                    # Handle AssistantMessage (text and tool use)
                    if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                        # API errors (e.g. "Credit balance is too low") arrive as
                        # synthetic assistant messages. Newer SDKs tag them with
                        # an error type; older ones only mark the model.
                        msg_error = getattr(msg, "error", None)
                        is_api_error = msg_error is not None or getattr(msg, "model", "") == "<synthetic>"
                        if msg_error == "billing_error":
                            billing_error = "billing_error"
                        for block in msg.content:
                            block_type = type(block).__name__

                            if block_type == "TextBlock" and hasattr(block, "text"):
                                response_text += block.text
                                print(block.text, end="", flush=True)
                                if is_api_error and is_billing_error(block.text):
                                    billing_error = block.text
                            elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                                print(f"\n[Tool: {block.name}]", flush=True)
                                tool_use_id = getattr(block, "id", None)
                                if block.name in FILE_WRITE_TOOLS and tool_use_id:
                                    write_tool_ids.add(tool_use_id)
                                if hasattr(block, "input"):
                                    input_str = str(block.input)
                                    if len(input_str) > 200:
                                        print(f"   Input: {input_str[:200]}...", flush=True)
                                    else:
                                        print(f"   Input: {input_str}", flush=True)

                    # Handle UserMessage (tool results)
                    elif msg_type == "UserMessage" and hasattr(msg, "content"):
                        for block in msg.content:
                            block_type = type(block).__name__

                            if block_type == "ToolResultBlock":
                                result_content = getattr(block, "content", "")
                                is_error = getattr(block, "is_error", False)
                                is_write_result = getattr(block, "tool_use_id", None) in write_tool_ids

                                # Check if command was blocked by security hook
                                if "blocked" in str(result_content).lower():
                                    print(f"   [BLOCKED] {result_content}", flush=True)
                                elif is_error:
                                    # Show errors (truncated)
                                    error_str = str(result_content)[:500]
                                    print(f"   [Error] {error_str}", flush=True)
                                    if is_file_state_error(str(result_content)):
                                        consecutive_file_state_errors += 1
                                else:
                                    # Tool succeeded - just show brief confirmation
                                    print("   [Done]", flush=True)
                                    if is_write_result:
                                        consecutive_file_state_errors = 0

                        if consecutive_file_state_errors >= MAX_CONSECUTIVE_FILE_STATE_ERRORS:
                            stuck_on_file_state = True
                            break

                    elif isinstance(msg, SystemMessage):
                        _print_system_message(msg)

                    # Handle ResultMessage (end of turn: errors, turn count, cost, token usage)
                    elif isinstance(msg, ResultMessage):
                        result_msg = msg
                        error_text = _print_result_message(msg)
                        if error_text:
                            # Surface errors in the response so rate-limit
                            # detection in the caller can see them
                            response_text += f"\n{error_text}"
                            if is_billing_error(error_text):
                                billing_error = error_text

                break  # Normal completion
            except Exception as inner_exc:
                if type(inner_exc).__name__ == "MessageParseError":
                    parse_retries += 1
                    if parse_retries > max_parse_retries:
                        print(f"Too many unrecognized CLI messages ({parse_retries}), stopping")
                        break
                    print(f"Ignoring unrecognized message from Claude CLI: {inner_exc}")
                    continue
                raise  # Re-raise to outer except

        if stuck_on_file_state:
            message_text = (
                f"Aborting session: {consecutive_file_state_errors} consecutive Write/Edit errors "
                "('File has not been read yet' / 'File has been unexpectedly modified') with no "
                "successful write in between. A dev server, formatter or another process may be "
                "rewriting the file. Starting a fresh session instead of burning tokens."
            )
            print(f"\n[STUCK] {message_text}", flush=True)
            try:
                await client.interrupt()
                # The interrupted turn still ends with a ResultMessage: count its cost
                result_msg = await _receive_result_after_interrupt(client)
            except Exception as interrupt_exc:
                print(f"   (interrupt failed: {interrupt_exc})", flush=True)
            if result_msg is not None:
                _record_session_usage(project_dir, result_msg, usage_context, cost_tracker)
            print("\n" + "-" * 70 + "\n")
            return "error", message_text

        if result_msg is not None:
            _record_session_usage(project_dir, result_msg, usage_context, cost_tracker)

        print("\n" + "-" * 70 + "\n")
        if billing_error is not None:
            return "billing", billing_error
        return "continue", response_text

    except Exception as e:
        error_str = str(e)
        print(f"Error during agent session: {error_str}")

        # Billing errors are not retryable: stop instead of burning more credits
        if billing_error is not None or is_billing_error(error_str):
            return "billing", billing_error or error_str

        # Detect rate limit errors from exception message
        if is_rate_limit_error(error_str):
            # Try to extract retry-after time from error
            retry_seconds = parse_retry_after(error_str)
            if retry_seconds is not None:
                return "rate_limit", str(retry_seconds)
            else:
                return "rate_limit", "unknown"

        return "error", error_str


async def run_autonomous_agent(
    project_dir: Path,
    model: str,
    max_iterations: Optional[int] = None,
    yolo_mode: bool = False,
    feature_id: Optional[int] = None,
    feature_ids: Optional[list[int]] = None,
    agent_type: Optional[str] = None,
    testing_feature_id: Optional[int] = None,
    testing_feature_ids: Optional[list[int]] = None,
    auto_improve: bool = False,
) -> int:
    """
    Run the autonomous agent loop.

    Args:
        project_dir: Directory for the project
        model: Claude model to use
        max_iterations: Maximum number of iterations (None for unlimited)
        yolo_mode: If True, skip browser testing in coding agent prompts
        feature_id: If set, work only on this specific feature (used by orchestrator for coding agents)
        feature_ids: If set, work on these features in batch (used by orchestrator for batch mode)
        agent_type: Type of agent: "initializer", "coding", "testing", or None (auto-detect)
        testing_feature_id: For testing agents, the pre-claimed feature ID to test (legacy single mode)
        testing_feature_ids: For testing agents, list of feature IDs to batch test
        auto_improve: If True, run in auto-improve mode (agent creates one
            improvement feature, implements it, commits, and exits). Takes
            precedence over other prompt selection branches.

    Returns:
        Process exit code: 0 normally, BILLING_ERROR_EXIT_CODE when stopped
        because credits ran out, BUDGET_EXCEEDED_EXIT_CODE when stopped
        because AUTOFORGE_MAX_BUDGET_USD was reached.
    """
    print("\n" + "=" * 70)
    print("  AUTONOMOUS CODING AGENT")
    print("=" * 70)
    print(f"\nProject directory: {project_dir}")
    print(f"Model: {model}")
    if agent_type:
        print(f"Agent type: {agent_type}")
    if auto_improve:
        print("Mode: AUTO-IMPROVE (one improvement + commit per session)")
    if yolo_mode:
        print("Mode: YOLO (testing agents disabled)")
    if feature_ids and len(feature_ids) > 1:
        print(f"Feature batch: {', '.join(f'#{fid}' for fid in feature_ids)}")
    elif feature_id:
        print(f"Feature assignment: #{feature_id}")
    if max_iterations:
        print(f"Max iterations: {max_iterations}")
    else:
        print("Max iterations: Unlimited (will run until completion)")
    max_budget_usd = get_max_budget_usd()
    if max_budget_usd is not None:
        print(f"Spending cap: ${max_budget_usd:.2f} (estimated, this agent process)")
    if uses_api_key_billing():
        print("Billing: ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is set - usage is billed per token")
        print("         to that Anthropic API account, not your Claude subscription.")
        if max_budget_usd is None:
            print(f"         Set {MAX_BUDGET_ENV_VAR} to cap spend.")
    print()

    # Create project directory
    project_dir.mkdir(parents=True, exist_ok=True)

    # Determine agent type if not explicitly set
    if agent_type is None:
        # Auto-detect based on whether we have features
        # (This path is for legacy compatibility - orchestrator should always set agent_type)
        is_first_run = not has_features(project_dir)
        if is_first_run:
            agent_type = "initializer"
        else:
            agent_type = "coding"

    is_initializer = agent_type == "initializer"

    if is_initializer:
        print("Running as INITIALIZER agent")
        print()
        print("=" * 70)
        print("  NOTE: Initialization takes 10-20+ minutes!")
        print("  The agent is generating detailed test cases.")
        print("  This may appear to hang - it's working. Watch for [Tool: ...] output.")
        print("=" * 70)
        print()
        # Copy the app spec into the project directory for the agent to read
        copy_spec_to_project(project_dir)
    elif agent_type == "testing":
        print("Running as TESTING agent (regression testing)")
        print_progress_summary(project_dir)
    else:
        print("Running as CODING agent")
        print_progress_summary(project_dir)

    # Agents spawned by the orchestrator (feature/batch/testing) run a single session
    # and exit; on rate limits they report to the orchestrator instead of waiting.
    single_session = (
        feature_id is not None
        or bool(feature_ids)
        or agent_type == "testing"
    )

    # Main loop
    iteration = 0
    rate_limit_retries = 0  # Track consecutive rate limit errors for exponential backoff
    error_retries = 0  # Track consecutive non-rate-limit errors
    total_cost_usd = 0.0  # Estimated spend across sessions in this process
    exit_code = 0

    while True:
        iteration += 1

        # Check if all features are already complete (before starting a new session)
        # Skip this check if running as initializer (needs to create features first)
        # or auto-improve mode (intentionally runs against finished projects)
        if not is_initializer and not auto_improve and iteration == 1:
            passing, in_progress, total, _nhi = count_passing_tests(project_dir)
            if total > 0 and passing == total:
                print("\n" + "=" * 70)
                print("  ALL FEATURES ALREADY COMPLETE!")
                print("=" * 70)
                print(f"\nAll {total} features are passing. Nothing left to do.")
                break

        # Check cost limit (global max_cost_usd setting vs. project's cumulative spend)
        limit_reached, total_cost, cost_limit = check_cost_limit(project_dir)
        if limit_reached and cost_limit is not None:
            print("\n" + "=" * 70)
            print("  COST LIMIT REACHED")
            print("=" * 70)
            print(f"\n{format_cost_limit_message(total_cost, cost_limit)}")
            exit_code = BUDGET_EXCEEDED_EXIT_CODE  # a stop, not a crash (the UI shows "stopped")
            break

        # Check max iterations
        if max_iterations and iteration > max_iterations:
            print(f"\nReached max iterations ({max_iterations})")
            print("To continue, run the script again without --max-iterations")
            break

        # Print session header
        print_session_header(iteration, is_initializer)

        # Create client (fresh context)
        client = create_client(project_dir, model, yolo_mode=yolo_mode, agent_type=agent_type)

        # Choose prompt based on agent type
        # auto_improve takes precedence over other branches — it's a distinct
        # mode where the agent creates its own feature before implementing it.
        if auto_improve:
            prompt = get_auto_improve_prompt(project_dir, yolo_mode=yolo_mode)
        elif agent_type == "initializer":
            prompt = get_initializer_prompt(project_dir)
        elif agent_type == "testing":
            prompt = get_testing_prompt(project_dir, testing_feature_id, testing_feature_ids)
        elif feature_ids and len(feature_ids) > 1:
            # Batch mode (used by orchestrator for multi-feature coding agents)
            prompt = get_batch_feature_prompt(feature_ids, project_dir, yolo_mode)
        elif feature_id or (feature_ids is not None and len(feature_ids) == 1):
            # Single-feature mode (used by orchestrator for coding agents)
            fid = feature_id if feature_id is not None else feature_ids[0]  # type: ignore[index]
            prompt = get_single_feature_prompt(fid, project_dir, yolo_mode)
        else:
            # General coding prompt (legacy path)
            prompt = get_coding_prompt(project_dir, yolo_mode=yolo_mode)

        prompt = with_file_editing_rules(prompt)
        # Point the agent at any user-uploaded knowledge documents
        prompt = with_project_knowledge(prompt, project_dir)
        # Tell the agent which installed skills to invoke, and when
        prompt = with_project_skills(prompt, project_dir, yolo_mode)

        # Run session with async context manager
        # Wrap in try/except to handle MCP server startup failures gracefully
        session_costs: list[float] = []
        try:
            async with client:
                session_feature_ids = feature_ids or (
                    [feature_id] if feature_id is not None
                    else testing_feature_ids or ([testing_feature_id] if testing_feature_id is not None else None)
                )
                status, response = await run_agent_session(
                    client,
                    prompt,
                    project_dir,
                    usage_context={"agent_type": agent_type, "feature_ids": session_feature_ids, "model": model},
                    cost_tracker=session_costs,
                )
        except Exception as e:
            print(f"Client/MCP server error: {e}")
            # Don't crash - return error status so the loop can retry
            status, response = ("billing" if is_billing_error(str(e)) else "error"), str(e)

        # Report session cost (from the SDK's ResultMessage; the orchestrator reads the
        # usage records, never this text)
        if session_costs:
            session_cost = sum(session_costs)
            total_cost_usd += session_cost
            print(f"Session cost (estimated): ${session_cost:.4f} | this agent total: ${total_cost_usd:.4f}", flush=True)

        # Billing errors (credits exhausted) are fatal: retrying keeps spending
        if status == "billing":
            print(f"\nBilling error: {response}", flush=True)
            print(BILLING_ERROR_HELP, flush=True)
            exit_code = BILLING_ERROR_EXIT_CODE
            break

        # Spending cap reached
        if max_budget_usd is not None and total_cost_usd >= max_budget_usd:
            print("\n" + "=" * 70, flush=True)
            print("  SPENDING CAP REACHED - AGENT STOPPED", flush=True)
            print("=" * 70, flush=True)
            print(
                f"Estimated spend ${total_cost_usd:.2f} reached {MAX_BUDGET_ENV_VAR}=${max_budget_usd:.2f}.",
                flush=True,
            )
            exit_code = BUDGET_EXCEEDED_EXIT_CODE
            break

        # Check for project completion - EXIT when all features pass.
        # Use the database as the source of truth rather than scanning the agent's
        # response text, so the loop can't keep spawning regression-only sessions
        # once everything passes (#13), whatever status the session ended with.
        passing, _in_progress, total, _nhi = count_passing_tests(project_dir)
        if not is_initializer and total > 0 and passing == total:
            print("\n" + "=" * 70)
            print("  ALL FEATURES COMPLETE!")
            print("=" * 70)
            print(f"\nCongratulations! All {total} features are passing.")
            print("The autonomous agent has finished its work.")
            break

        # Handle status
        if status == "continue":
            # Reset error retries on success; rate-limit retries reset only if no signal
            error_retries = 0
            reset_rate_limit_retries = True

            delay_seconds = AUTO_CONTINUE_DELAY_SECONDS

            # Check for rate/usage limit indicators in response text
            # (e.g. "Limit reached · resets 6pm (America/New_York)")
            if is_rate_limit_error(response):
                reset_rate_limit_retries = False
                known_delay: Optional[int] = None
                reset_at: Optional[datetime] = None

                reset = parse_limit_reset(response)
                if reset is not None:
                    known_delay, reset_at = reset
                else:
                    retry_seconds = parse_retry_after(response)
                    if retry_seconds is not None:
                        known_delay = clamp_retry_delay(retry_seconds)

                if known_delay is not None:
                    delay_seconds = known_delay
                else:
                    # Use exponential backoff when the reset time is unknown
                    delay_seconds = calculate_rate_limit_backoff(rate_limit_retries)
                    rate_limit_retries += 1

                _print_limit_warning(response, delay_seconds, reset_at)

                if single_session:
                    # Don't sleep for hours inside a feature agent: hand the wait to
                    # the orchestrator so it pauses ALL agents instead of respawning.
                    print(format_rate_limit_marker(known_delay), flush=True)
                    print("Stopping this session; new sessions are paused until the limit resets.", flush=True)
                    break

                print(f"\nAgent will auto-continue in {delay_seconds:.0f}s...", flush=True)
            else:
                print(
                    f"\nAgent will auto-continue in {delay_seconds:.0f}s...", flush=True
                )

            sys.stdout.flush()  # this should allow the pause to be displayed before sleeping
            print_progress_summary(project_dir)

            # Single-feature mode, batch mode, or testing agent: exit after one session
            if feature_ids and len(feature_ids) > 1:
                print(f"\nBatch mode: Features {', '.join(f'#{fid}' for fid in feature_ids)} session complete.")
                break
            elif feature_id is not None or (feature_ids is not None and len(feature_ids) == 1):
                fid = feature_id if feature_id is not None else feature_ids[0]  # type: ignore[index]
                if agent_type == "testing":
                    print("\nTesting agent complete. Terminating session.")
                else:
                    print(f"\nSingle-feature mode: Feature #{fid} session complete.")
                break
            elif agent_type == "testing":
                print("\nTesting agent complete. Terminating session.")
                break

            # Reset rate limit retries only if no rate limit signal was detected
            if reset_rate_limit_retries:
                rate_limit_retries = 0

            await asyncio.sleep(delay_seconds)

        elif status == "rate_limit":
            # Smart rate limit handling with exponential backoff
            # Reset error counter so mixed events don't inflate delays
            error_retries = 0
            if single_session:
                retry_hint = int(response) if response.isdigit() else None
                if retry_hint is not None:
                    retry_hint = clamp_retry_delay(retry_hint)
                _print_limit_warning(
                    "Rate limit hit (API error).",
                    retry_hint if retry_hint is not None else calculate_rate_limit_backoff(rate_limit_retries),
                    None,
                )
                print(format_rate_limit_marker(retry_hint), flush=True)
                print("Stopping this session; new sessions are paused until the limit resets.", flush=True)
                break
            if response != "unknown":
                try:
                    delay_seconds = clamp_retry_delay(int(response))
                except (ValueError, TypeError):
                    # Malformed value - fall through to exponential backoff
                    response = "unknown"
            if response == "unknown":
                # Use exponential backoff when retry-after unknown or malformed
                delay_seconds = calculate_rate_limit_backoff(rate_limit_retries)
                rate_limit_retries += 1
                print(f"\nRate limit hit. Backoff wait: {delay_seconds} seconds (attempt #{rate_limit_retries})...")
            else:
                print(f"\nRate limit hit. Waiting {delay_seconds} seconds before retry...")

            await asyncio.sleep(delay_seconds)

        elif status == "error":
            # Non-rate-limit errors: linear backoff capped at 5 minutes
            # Reset rate limit counter so mixed events don't inflate delays
            rate_limit_retries = 0
            error_retries += 1
            delay_seconds = calculate_error_backoff(error_retries)
            print("\nSession encountered an error")
            print(f"Will retry in {delay_seconds}s (attempt #{error_retries})...")
            await asyncio.sleep(delay_seconds)

        # Small delay between sessions
        if max_iterations is None or iteration < max_iterations:
            print("\nPreparing next session...\n")
            await asyncio.sleep(1)

    # Final summary
    print("\n" + "=" * 70)
    print("  SESSION COMPLETE")
    print("=" * 70)
    print(f"\nProject directory: {project_dir}")
    print_progress_summary(project_dir)

    # Print instructions for running the generated application
    print("\n" + "-" * 70)
    print("  TO RUN THE GENERATED APPLICATION:")
    print("-" * 70)
    print(f"\n  cd {project_dir.resolve()}")
    print("  ./init.sh           # Run the setup script")
    print("  # Or manually:")
    print("  npm install && npm run dev")
    print("\n  Then open http://localhost:3000 (or check init.sh for the URL)")
    print("-" * 70)

    print("\nDone!")
    return exit_code
