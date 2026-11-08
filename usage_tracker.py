"""
Token/Cost Usage Tracking
=========================

Records the token usage and USD cost that the Claude Agent SDK reports at the
end of every agent session (``ResultMessage``), and enforces an optional
per-project spending limit.

Usage is stored in the project's ``features.db`` (``agent_usage`` table) so that
parallel agents, the orchestrator and the API server all see the same totals.

The limit comes from the global ``max_cost_usd`` setting (Settings UI). A value
of 0 or an unset/invalid value means "no limit". When a project's cumulative
cost reaches the limit, the orchestrator stops spawning new agents and the
single-agent loop exits after the current session.

Note: ``total_cost_usd`` is computed by the Claude CLI using Anthropic pricing.
With alternative providers (GLM, Ollama, ...) the dollar figure is only an
estimate, but token counts remain accurate.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func

from api.database import AgentUsage, create_database
from api.safe_project_io import UnsafeProjectPathError
from autoforge_paths import get_features_db_path

logger = logging.getLogger(__name__)

COST_LIMIT_SETTING = "max_cost_usd"
RECENT_SESSIONS_LIMIT = 20


def _to_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return max(float(value or 0.0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def extract_usage(result_message: Any) -> dict[str, Any]:
    """Pull token/cost figures out of an SDK ``ResultMessage``.

    Tolerates missing attributes and partial ``usage`` dicts so that SDK/CLI
    version drift never breaks an agent session.
    """
    usage = getattr(result_message, "usage", None) or {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": _to_int(usage.get("input_tokens")),
        "output_tokens": _to_int(usage.get("output_tokens")),
        "cache_creation_tokens": _to_int(usage.get("cache_creation_input_tokens")),
        "cache_read_tokens": _to_int(usage.get("cache_read_input_tokens")),
        "cost_usd": _to_float(getattr(result_message, "total_cost_usd", None)),
        "duration_ms": _to_int(getattr(result_message, "duration_ms", None)),
        "num_turns": _to_int(getattr(result_message, "num_turns", None)),
    }


def record_usage(
    project_dir: Path,
    usage: dict[str, Any],
    agent_type: str | None = None,
    feature_ids: list[int] | None = None,
    model: str | None = None,
) -> None:
    """Persist one session's usage. Failures are logged, never raised."""
    try:
        _, SessionLocal = create_database(project_dir)
        session = SessionLocal()
        try:
            session.add(AgentUsage(
                agent_type=agent_type,
                feature_ids=feature_ids or None,
                model=model,
                input_tokens=_to_int(usage.get("input_tokens")),
                output_tokens=_to_int(usage.get("output_tokens")),
                cache_creation_tokens=_to_int(usage.get("cache_creation_tokens")),
                cache_read_tokens=_to_int(usage.get("cache_read_tokens")),
                cost_usd=_to_float(usage.get("cost_usd")),
                duration_ms=_to_int(usage.get("duration_ms")),
                num_turns=_to_int(usage.get("num_turns")),
            ))
            session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.warning("Failed to record usage for %s: %s", project_dir, e)


class CostCheckError(Exception):
    """The cost limit or the project's spend could not be read; callers must not spend."""


def get_cost_limit(strict: bool = False) -> float | None:
    """Return the configured per-project cost limit in USD, or None if unlimited.

    With ``strict`` a registry read error raises CostCheckError instead of
    reading as "unlimited" (the enforcement path fails closed).
    """
    try:
        from registry import get_setting
        raw = get_setting(COST_LIMIT_SETTING)
    except Exception as e:
        if strict:
            raise CostCheckError(f"Could not read the cost limit setting: {e}") from e
        return None
    limit = _to_float(raw)
    return limit if limit > 0 else None


def get_usage_totals(project_dir: Path, strict: bool = False) -> dict[str, Any]:
    """Return cumulative usage for a project (all zeros if nothing recorded).

    A read error returns zeros with a warning (display path), or raises
    CostCheckError with ``strict`` (enforcement path: never a silent $0).
    """
    totals: dict[str, Any] = {
        "sessions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
    }
    if not get_features_db_path(project_dir).exists():
        return totals
    try:
        _, SessionLocal = create_database(project_dir)
        session = SessionLocal()
        try:
            row = session.query(
                func.count(AgentUsage.id),
                func.coalesce(func.sum(AgentUsage.input_tokens), 0),
                func.coalesce(func.sum(AgentUsage.output_tokens), 0),
                func.coalesce(func.sum(AgentUsage.cache_creation_tokens), 0),
                func.coalesce(func.sum(AgentUsage.cache_read_tokens), 0),
                func.coalesce(func.sum(AgentUsage.cost_usd), 0.0),
            ).one()
        finally:
            session.close()
    except UnsafeProjectPathError:
        raise  # a planted link is a refusal, never a silent $0 spend (shown as 409)
    except Exception as e:
        if strict:
            raise CostCheckError(f"Could not read the project's spend: {e}") from e
        logger.warning("Failed to read usage for %s: %s", project_dir, e)
        return totals
    totals.update(
        sessions=int(row[0]),
        input_tokens=int(row[1]),
        output_tokens=int(row[2]),
        cache_creation_tokens=int(row[3]),
        cache_read_tokens=int(row[4]),
        cost_usd=float(row[5]),
    )
    return totals


def get_usage_cost_since(project_dir: Path, since: datetime, strict: bool = False) -> float:
    """Total cost of the usage records created at or after ``since`` (one run's spend).

    A read error returns 0.0 with a warning, or raises CostCheckError with ``strict``.
    """
    if not get_features_db_path(project_dir).exists():
        return 0.0
    try:
        _, SessionLocal = create_database(project_dir)
        session = SessionLocal()
        try:
            total = (
                session.query(func.coalesce(func.sum(AgentUsage.cost_usd), 0.0))
                .filter(AgentUsage.created_at >= since)
                .scalar()
            )
        finally:
            session.close()
    except UnsafeProjectPathError:
        raise
    except Exception as e:
        if strict:
            raise CostCheckError(f"Could not read this run's spend: {e}") from e
        logger.warning("Failed to read run usage for %s: %s", project_dir, e)
        return 0.0
    return float(total or 0.0)


def get_recent_sessions(project_dir: Path, limit: int = RECENT_SESSIONS_LIMIT) -> list[dict[str, Any]]:
    """Return the most recent usage records, newest first."""
    if not get_features_db_path(project_dir).exists():
        return []
    try:
        _, SessionLocal = create_database(project_dir)
        session = SessionLocal()
        try:
            rows = (
                session.query(AgentUsage)
                .order_by(AgentUsage.id.desc())
                .limit(limit)
                .all()
            )
            return [r.to_dict() for r in rows]
        finally:
            session.close()
    except UnsafeProjectPathError:
        raise
    except Exception as e:
        logger.warning("Failed to read usage sessions for %s: %s", project_dir, e)
        return []


def reset_usage(project_dir: Path) -> int:
    """Delete all usage records for a project. Returns the number of rows removed."""
    if not get_features_db_path(project_dir).exists():
        return 0
    _, SessionLocal = create_database(project_dir)
    session = SessionLocal()
    try:
        deleted = session.query(AgentUsage).delete()
        session.commit()
        return int(deleted)
    finally:
        session.close()


def check_cost_limit(project_dir: Path) -> tuple[bool, float, float | None]:
    """Check whether the project has reached its cost limit.

    Returns:
        (limit_reached, total_cost_usd, limit_usd_or_None)

    Raises:
        CostCheckError: the limit or the spend could not be read (fail closed)
        UnsafeProjectPathError: features.db is refused (planted link)
    """
    limit = get_cost_limit(strict=True)
    if limit is None:
        return False, 0.0, None
    total = get_usage_totals(project_dir, strict=True)["cost_usd"]
    return total >= limit, total, limit


def format_cost_limit_message(total: float, limit: float) -> str:
    return (
        f"Cost limit reached: ${total:.2f} spent of ${limit:.2f} budget. "
        "Raise or clear the limit in Settings (or reset project usage) to continue."
    )
