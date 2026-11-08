"""
Git Remote Sync
===============

Optional "auto-push": after a coding agent session finishes successfully, push
the project's current branch to the git remote the user chose, so work shows
up on GitHub (or any other remote) without manual intervention.

Off by default and enabled per project (``enable_auto_push``, from the Settings
page). Enabling stores the remote's URL as it is at that moment; every push
goes explicitly to that stored URL.

The push runs in the orchestrator, outside the agent's sandbox, while the
agent may run any ``git`` command in the project. So a push is refused when:
- ``git remote get-url <remote>`` no longer equals the stored URL;
- the repository's own config (local and worktree scopes) holds any key
  outside a small safe set (core repository flags, identity, branch tracking,
  remote url/fetch). Many keys can redirect a push or run a program
  (``url.*`` insteadOf rewrites apply even to an explicit URL,
  ``core.sshCommand``, ``credential.*``, ``remote.*.receivepack``,
  ``include.*`` hiding keys in another file, ...), so an allowlist is used
  rather than a list of known-bad keys.
Hooks never run (``core.hooksPath=/dev/null`` and ``--no-verify``), it never
force-pushes, never creates remotes, and runs non-interactively with a timeout.
"""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Registry setting: JSON map of resolved project path -> {"remote": name, "url": stored URL}
GIT_PUSH_TARGETS_SETTING = "git_push_targets"
PUSH_TIMEOUT_SECONDS = 60

# Repository-scoped config keys a push may run with; any other key refuses it.
# Section and variable names are case-insensitive (git prints them lowercased).
SAFE_REPO_CONFIG_KEYS = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "core.symlinks", "core.autocrlf", "core.eol",
    "core.safecrlf", "user.name", "user.email", "commit.gpgsign", "tag.gpgsign", "init.defaultbranch",
    "pull.rebase", "pull.ff", "extensions.objectformat",
})
# Keys with a subsection: (section, variable) allowed for any subsection
SAFE_REPO_CONFIG_SUBSECTION_KEYS = frozenset({
    ("branch", "remote"), ("branch", "merge"), ("branch", "rebase"),
    ("remote", "url"), ("remote", "fetch"),
})
# Remote nicknames must be plain names: one shaped like a URL ("host:repo.git")
# could stand in for the stored URL on the push command line
_REMOTE_NICKNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

# Overrides that beat any repo-local value (command-line config wins)
_SAFE_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "push.gpgSign=false")

# Serializes pushes when several agents finish at the same time
_push_lock = threading.Lock()


class GitPushError(Exception):
    """Auto-push cannot be enabled for this project (the message says why)."""


def _git(project_dir: Path, *args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        # Fail fast instead of waiting for a username/password prompt
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }
    return subprocess.run(
        ["git", *_SAFE_CONFIG, *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _project_key(project_dir: Path) -> str:
    return project_dir.resolve().as_posix()


def _load_targets() -> dict:
    from registry import get_setting

    try:
        targets = json.loads(get_setting(GIT_PUSH_TARGETS_SETTING) or "{}")
    except (TypeError, ValueError):
        return {}
    return targets if isinstance(targets, dict) else {}


def _save_targets(targets: dict) -> None:
    from registry import set_setting

    set_setting(GIT_PUSH_TARGETS_SETTING, json.dumps(targets, sort_keys=True))


def get_push_target(project_dir: Path) -> dict | None:
    """The stored ``{"remote", "url"}`` for this project, or None when auto-push is off."""
    try:
        target = _load_targets().get(_project_key(project_dir))
    except Exception as e:  # registry unavailable - treat as off
        logger.debug("Could not read %s: %s", GIT_PUSH_TARGETS_SETTING, e)
        return None
    if isinstance(target, dict) and isinstance(target.get("remote"), str) and isinstance(target.get("url"), str):
        return {"remote": target["remote"], "url": target["url"]}
    return None


def find_unsafe_local_config(project_dir: Path) -> list[str]:
    """Repository-scoped config keys (local and worktree) outside the safe set.

    ``git config --local`` would miss ``.git/config.worktree``, so every scope is
    listed and the repository's own scopes are checked.
    """
    result = _git(project_dir, "config", "--list", "--show-scope", "--name-only")
    if result.returncode != 0:
        # Fail closed: nothing is known to be safe (e.g. a broken config, or git older than 2.26)
        detail = (result.stderr or "").strip().splitlines()
        return [f"<could not read the git config: {detail[-1] if detail else f'exit code {result.returncode}'}>"]
    unsafe: set[str] = set()
    for line in result.stdout.splitlines():
        scope, _, key = line.strip().partition("\t")
        if scope in ("local", "worktree") and key and not _is_safe_repo_key(key):
            unsafe.add(key.lower())
    return sorted(unsafe)


def _is_safe_repo_key(key: str) -> bool:
    section, _, rest = key.partition(".")
    subsection, _, variable = rest.rpartition(".")
    section, variable = section.lower(), variable.lower()
    if subsection:
        if section == "remote" and not _REMOTE_NICKNAME_RE.match(subsection):
            return False
        return (section, variable) in SAFE_REPO_CONFIG_SUBSECTION_KEYS
    return f"{section}.{variable}" in SAFE_REPO_CONFIG_KEYS


def _unsafe_config_message(keys: list[str]) -> str:
    return (
        f"refusing: the project's git config sets {', '.join(keys)}; auto-push only runs with plain "
        "repository, identity, branch and remote url/fetch settings, because other keys can redirect "
        "the push or run a program. Remove them (or move them to your global git config)"
    )


def get_remote_url(project_dir: Path, remote: str) -> str | None:
    """``git remote get-url <remote>``, or None when there is no such remote."""
    result = _git(project_dir, "remote", "get-url", "--", remote)
    url = result.stdout.strip()
    return url if result.returncode == 0 and url else None


def enable_auto_push(project_dir: Path, remote: str = "origin") -> dict:
    """Trust the remote's current URL for this project and turn auto-push on.

    Raises:
        GitPushError: not a git repository, no such remote, or unsafe local config.
    """
    inside = _git(project_dir, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise GitPushError("The project folder is not a git repository")
    unsafe = find_unsafe_local_config(project_dir)
    if unsafe:
        raise GitPushError(_unsafe_config_message(unsafe))
    url = get_remote_url(project_dir, remote)
    if url is None:
        raise GitPushError(f"The project has no remote '{remote}' (add it with `git remote add {remote} <url>`)")

    targets = _load_targets()
    target = {"remote": remote, "url": url}
    targets[_project_key(project_dir)] = target
    _save_targets(targets)
    return target


def disable_auto_push(project_dir: Path) -> None:
    targets = _load_targets()
    if targets.pop(_project_key(project_dir), None) is not None:
        _save_targets(targets)


def push_current_branch(project_dir: Path) -> tuple[bool, str]:
    """Push the current branch to the stored URL (see the module docstring for the refusals).

    Returns:
        (success, message) - success is False when the push was skipped, refused or failed.
    """
    target = get_push_target(project_dir)
    if target is None:
        return False, "auto-push is off for this project"

    with _push_lock:
        try:
            inside = _git(project_dir, "rev-parse", "--is-inside-work-tree")
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                return False, "not a git repository, skipping push"

            branch_res = _git(project_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
            branch = branch_res.stdout.strip()
            if branch_res.returncode != 0 or not branch:
                return False, "detached HEAD, skipping push"

            unsafe = find_unsafe_local_config(project_dir)
            if unsafe:
                return False, _unsafe_config_message(unsafe)

            current = get_remote_url(project_dir, target["remote"])
            if current != target["url"]:
                return False, (
                    f"refusing: remote '{target['remote']}' changed from {target['url']} to {current or '(none)'}; "
                    "turn auto-push off and on again in Settings if you trust the new URL"
                )

            # Full ref names: no "+" (force) prefix can reach the refspec
            refspec = f"refs/heads/{branch}:refs/heads/{branch}"
            result = _git(
                project_dir, "push", "--no-verify", "--no-recurse-submodules", "--", target["url"], refspec,
                timeout=PUSH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, f"git push timed out after {PUSH_TIMEOUT_SECONDS}s"
        except (FileNotFoundError, OSError) as e:
            return False, f"git unavailable: {e}"

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"exit code {result.returncode}"
            return False, f"git push of '{branch}' to {target['url']} failed: {reason}"
        return True, f"pushed '{branch}' to {target['url']}"
