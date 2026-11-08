"""
Security Hooks for Autonomous Coding Agent
==========================================

Pre-tool-use hooks that validate bash commands for security.
Uses an allowlist approach - only explicitly permitted commands can run.
"""

import asyncio
import glob
import logging
import os
import re
import shlex
from pathlib import Path, PurePath
from typing import Optional

import yaml

# Logger for security-related events (fallback parsing, validation failures, etc.)
logger = logging.getLogger(__name__)

# Regex pattern for valid pkill process names (no regex metacharacters allowed)
# Matches alphanumeric names with dots, underscores, and hyphens
VALID_PROCESS_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# Allowed commands for development tasks
# Minimal set needed for the autonomous coding demo
ALLOWED_COMMANDS = {
    # File inspection
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    # File operations (agent uses SDK tools for most file ops, but cp/mkdir needed occasionally)
    "cp",
    "mkdir",
    "chmod",  # For making scripts executable; validated separately
    # Directory
    "pwd",
    # Output
    "echo",
    # Node.js development
    "npm",
    "npx",
    "pnpm",  # Project uses pnpm
    "node",
    # Version control
    "git",
    # Docker (for PostgreSQL)
    "docker",
    # Process management
    "ps",
    "lsof",
    "sleep",
    "kill",  # Kill by PID
    "pkill",  # For killing dev servers; validated separately
    # Network/API testing
    "curl",
    # File operations
    "mv",
    "rm",  # Targets restricted to the project directory; validated separately
    "touch",
    # Shell scripts
    "sh",
    "bash",
    # Script execution
    "init.sh",  # Init scripts; validated separately
    # Browser automation
    "playwright-cli",  # Playwright CLI for browser testing; validated separately
}

# Commands that need additional validation even when in the allowlist
COMMANDS_NEEDING_EXTRA_VALIDATION = {"pkill", "chmod", "init.sh", "playwright-cli", "rm"}

# Commands that are NEVER allowed, even with user approval
# These commands can cause permanent system damage or security breaches
BLOCKED_COMMANDS = {
    # Disk operations
    "dd",
    "mkfs",
    "fdisk",
    "parted",
    # System control
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "init",
    # Ownership changes
    "chown",
    "chgrp",
    # System services
    "systemctl",
    "service",
    "launchctl",
    # Network security
    "iptables",
    "ufw",
}

# Sensitive directories (relative to home) that should never be exposed.
# Used by both the EXTRA_READ_PATHS validator (client.py) and the filesystem
# browser API (server/routers/filesystem.py) to block credential/key directories.
# This is the single source of truth -- import from here in both places.
#
# SENSITIVE_DIRECTORIES is the union of the previous filesystem browser blocklist
# (filesystem.py) and the previous EXTRA_READ_PATHS blocklist (client.py).
# Some entries are new to each consumer -- this is intentional for defense-in-depth.
SENSITIVE_DIRECTORIES = {
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".gnupg",
    ".gpg",
    ".password-store",
    ".docker",
    ".config/gcloud",
    ".config/gh",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".terraform",
}

# Commands that trigger emphatic warnings but CAN be approved (Phase 3)
# For now, these are blocked like BLOCKED_COMMANDS until Phase 3 implements approval
DANGEROUS_COMMANDS = {
    # Privilege escalation
    "sudo",
    "su",
    "doas",
    # Cloud CLIs (can modify production infrastructure)
    "aws",
    "gcloud",
    "az",
    # Container and orchestration
    "kubectl",
    # Note: docker-compose removed - commonly needed for local dev environments
}


def split_command_segments(command_string: str) -> list[str]:
    """
    Split a compound command into individual command segments.

    Handles command chaining (&&, ||, ;) but not pipes (those are single commands).

    Args:
        command_string: The full shell command

    Returns:
        List of individual command segments
    """
    import re

    # Split on && and || while preserving the ability to handle each segment
    # This regex splits on && or || that aren't inside quotes
    segments = re.split(r"\s*(?:&&|\|\|)\s*", command_string)

    # Further split on semicolons
    result = []
    for segment in segments:
        sub_segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', segment)
        for sub in sub_segments:
            sub = sub.strip()
            if sub:
                result.append(sub)

    return result


def _extract_primary_command(segment: str) -> str | None:
    """
    Fallback command extraction when shlex fails.

    Extracts the first word that looks like a command, handling cases
    like complex docker exec commands with nested quotes.

    Args:
        segment: The command segment to parse

    Returns:
        The primary command name, or None if extraction fails
    """
    # Remove leading whitespace
    segment = segment.lstrip()

    if not segment:
        return None

    # Skip env var assignments at start (VAR=value cmd)
    words = segment.split()
    while words and "=" in words[0] and not words[0].startswith("="):
        words = words[1:]

    if not words:
        return None

    # Extract first token (the command)
    first_word = words[0]

    # Match valid command characters (alphanumeric, dots, underscores, hyphens, slashes)
    match = re.match(r"^([a-zA-Z0-9_./-]+)", first_word)
    if match:
        cmd = match.group(1)
        return os.path.basename(cmd)

    return None


def extract_commands(command_string: str) -> list[str]:
    """
    Extract command names from a shell command string.

    Handles pipes, command chaining (&&, ||, ;), and subshells.
    Returns the base command names (without paths).

    Args:
        command_string: The full shell command

    Returns:
        List of command names found in the string
    """
    commands = []

    # shlex doesn't treat ; as a separator, so we need to pre-process
    # (re is already imported at module level)

    # Split on semicolons that aren't inside quotes (simple heuristic)
    # This handles common cases like "echo hello; ls"
    segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', command_string)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Malformed command (unclosed quotes, etc.)
            # Try fallback extraction instead of blocking entirely
            fallback_cmd = _extract_primary_command(segment)
            if fallback_cmd:
                logger.debug(
                    "shlex fallback used: segment=%r -> command=%r",
                    segment,
                    fallback_cmd,
                )
                commands.append(fallback_cmd)
            else:
                logger.debug(
                    "shlex fallback failed: segment=%r (no command extracted)",
                    segment,
                )
            continue

        if not tokens:
            continue

        # Track when we expect a command vs arguments
        expect_command = True
        # Inside a 'for/select NAME in WORDS' header: the name and words are data
        in_loop_header = False

        for token in tokens:
            # Shell operators indicate a new command follows
            if token in ("|", "||", "&&", "&"):
                expect_command = True
                in_loop_header = False
                continue

            if expect_command and token in ("for", "select"):
                in_loop_header = True
                continue
            if in_loop_header:
                if token == "do":
                    in_loop_header = False
                continue

            # Skip shell keywords that precede commands
            if token in (
                "if",
                "then",
                "else",
                "elif",
                "fi",
                "for",
                "while",
                "until",
                "do",
                "done",
                "case",
                "esac",
                "in",
                "!",
                "{",
                "}",
            ):
                continue

            # Skip flags/options
            if token.startswith("-"):
                continue

            # Skip variable assignments (VAR=value)
            if "=" in token and not token.startswith("="):
                continue

            if expect_command:
                # Extract the base command name (handle paths like /usr/bin/python)
                cmd = os.path.basename(token)
                commands.append(cmd)
                expect_command = False

    return commands


# Default pkill process names (hardcoded baseline, always available)
DEFAULT_PKILL_PROCESSES = {
    "node",
    "npm",
    "npx",
    "vite",
    "next",
}


def validate_pkill_command(
    command_string: str,
    extra_processes: Optional[set[str]] = None
) -> tuple[bool, str]:
    """
    Validate pkill commands - only allow killing dev-related processes.

    Uses shlex to parse the command, avoiding regex bypass vulnerabilities.

    Args:
        command_string: The pkill command to validate
        extra_processes: Optional set of additional process names to allow
                        (from org/project config pkill_processes)

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    # Merge default processes with any extra configured processes
    allowed_process_names = DEFAULT_PKILL_PROCESSES.copy()
    if extra_processes:
        allowed_process_names |= extra_processes

    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse pkill command"

    if not tokens:
        return False, "Empty pkill command"

    # Separate flags from arguments
    args = []
    for token in tokens[1:]:
        if not token.startswith("-"):
            args.append(token)

    if not args:
        return False, "pkill requires a process name"

    # Validate every non-flag argument (pkill accepts multiple patterns on BSD)
    # This defensively ensures no disallowed process can be targeted
    targets = []
    for arg in args:
        # For -f flag (full command line match), take the first word as process name
        # e.g., "pkill -f 'node server.js'" -> target is "node server.js", process is "node"
        t = arg.split()[0] if " " in arg else arg
        targets.append(t)

    disallowed = [t for t in targets if t not in allowed_process_names]
    if not disallowed:
        return True, ""
    return False, f"pkill only allowed for processes: {sorted(allowed_process_names)}"


def validate_chmod_command(command_string: str) -> tuple[bool, str]:
    """
    Validate chmod commands - only allow making files executable with +x.

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse chmod command"

    if not tokens or tokens[0] != "chmod":
        return False, "Not a chmod command"

    # Look for the mode argument
    # Valid modes: +x, u+x, a+x, etc. (anything ending with +x for execute permission)
    mode = None
    files = []

    for token in tokens[1:]:
        if token.startswith("-"):
            # Skip flags like -R (we don't allow recursive chmod anyway)
            return False, "chmod flags are not allowed"
        elif mode is None:
            mode = token
        else:
            files.append(token)

    if mode is None:
        return False, "chmod requires a mode"

    if not files:
        return False, "chmod requires at least one file"

    # Only allow +x variants (making files executable)
    # This matches: +x, u+x, g+x, o+x, a+x, ug+x, etc.
    import re

    if not re.match(r"^[ugoa]*\+x$", mode):
        return False, f"chmod only allowed with +x mode, got: {mode}"

    return True, ""


def _is_within(path: str, root: str) -> bool:
    """Return True if path is strictly inside root (not root itself)."""
    try:
        return os.path.commonpath([path, root]) == root and path != root
    except ValueError:
        # Different drives on Windows
        return False


def _resolve_rm_target(target: str, base: str) -> str:
    """Resolve an rm target to the real path rm would act on."""
    joined = os.path.normpath(os.path.join(base, target))
    stripped = target.rstrip("/")
    if target.endswith("/") or stripped == "." or stripped.endswith("/.") or stripped in ("", ".."):
        # With a trailing slash (or '.'), rm follows a symlink to its target
        return os.path.realpath(joined)
    # Otherwise resolve symlinks in the parent only: rm removes a symlink
    # itself, not what it points to.
    parent, name = os.path.split(joined)
    return os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(joined)


def validate_rm_command(command_string: str, project_dir: Optional[Path] = None) -> tuple[bool, str]:
    """
    Validate rm commands - targets must stay inside the project directory.

    Blocks:
    - Root, home, current/parent directory targets (rm -rf /, ~, ., ..)
    - Home, variable, command-substitution and brace-expansion targets that
      can't be validated statically (~/x, $HOME, `pwd`, {../x,y})
    - Symlinks followed via a trailing slash (link/) or a glob match
    - Bare wildcards at the project root (rm -rf *, rm -rf .*)
    - Absolute or ../ paths that resolve outside the project directory
      (or to the project directory itself)
    - --no-preserve-root

    Relative paths are resolved against project_dir (or the current working
    directory when project_dir is unknown).

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse rm command"

    if not tokens or os.path.basename(tokens[0]) != "rm":
        return False, "Not an rm command"

    targets = []
    end_of_options = False
    for token in tokens[1:]:
        if not end_of_options and token == "--":
            end_of_options = True
        elif not end_of_options and token.startswith("-") and token != "-":
            if token == "--no-preserve-root":
                return False, "rm --no-preserve-root is not allowed"
        else:
            targets.append(token)

    if not targets:
        return False, "rm requires at least one target"

    base = os.path.realpath(str(project_dir) if project_dir else os.getcwd())

    for target in targets:
        if any(ch in target for ch in "~$`{"):
            return False, (
                f"rm target '{target}' uses home, variable, command or brace expansion and cannot be validated"
            )

        candidates = [target]
        if glob.has_magic(target):
            # A glob can match a symlink (e.g. 'es*/' -> 'esc/'), so validate
            # every path bash would expand it to, not just the pattern.
            pattern = os.path.join(base, target)
            candidates += glob.glob(pattern)
            # Parent directories matched by a glob are followed by rm even
            # when the final file doesn't exist yet (e.g. 'e*/x' via 'esc/').
            parent_pattern = os.path.dirname(pattern.rstrip("/"))
            while glob.has_magic(parent_pattern):
                for match in glob.glob(parent_pattern):
                    real = os.path.realpath(match)
                    if real != base and not _is_within(real, base):
                        return False, f"rm target '{target}' is outside the project directory"
                parent_pattern = os.path.dirname(parent_pattern)

        for candidate in candidates:
            resolved = _resolve_rm_target(candidate, base)
            if not _is_within(resolved, base):
                return False, f"rm target '{target}' is outside the project directory"

        # Pure wildcard directly in the project root would wipe the whole project
        resolved = _resolve_rm_target(target, base)
        name = os.path.basename(resolved)
        if os.path.dirname(resolved) == base and re.fullmatch(r"[*?.]+", name) and re.search(r"[*?]", name):
            return False, f"rm target '{target}' would delete the entire project directory"

    return True, ""


def validate_init_script(command_string: str) -> tuple[bool, str]:
    """
    Validate init.sh script execution - only allow ./init.sh.

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse init script command"

    if not tokens:
        return False, "Empty command"

    # The command should be exactly ./init.sh (possibly with arguments)
    script = tokens[0]

    # Allow ./init.sh or paths ending in /init.sh
    if script == "./init.sh" or script.endswith("/init.sh"):
        return True, ""

    return False, f"Only ./init.sh is allowed, got: {script}"


def validate_playwright_command(command_string: str) -> tuple[bool, str]:
    """
    Validate playwright-cli commands - block dangerous subcommands.

    Blocks `run-code` (arbitrary Node.js execution) and `eval` (arbitrary JS
    evaluation) which bypass the security sandbox.

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse playwright-cli command"

    if not tokens:
        return False, "Empty command"

    BLOCKED_SUBCOMMANDS = {"run-code", "eval"}

    # Find the subcommand: first non-flag token after 'playwright-cli'
    for token in tokens[1:]:
        if token.startswith("-"):
            continue  # skip flags like -s=agent-1
        if token in BLOCKED_SUBCOMMANDS:
            return False, f"playwright-cli '{token}' is not allowed"
        break  # first non-flag token is the subcommand

    return True, ""


def matches_pattern(command: str, pattern: str) -> bool:
    """
    Check if a command matches a pattern.

    Supports:
    - Exact match: "swift"
    - Prefix wildcard: "swift*" matches "swift", "swiftc", "swiftformat"
    - Local script paths: "./scripts/build.sh" or "scripts/test.sh"

    Args:
        command: The command to check
        pattern: The pattern to match against

    Returns:
        True if command matches pattern
    """
    # Reject bare wildcards - security measure to prevent matching everything
    if pattern == "*":
        return False

    # Exact match
    if command == pattern:
        return True

    # Prefix wildcard (e.g., "swift*" matches "swiftc", "swiftlint")
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        # Also reject if prefix is empty (would be bare "*")
        if not prefix:
            return False
        return command.startswith(prefix)

    # Path patterns (./scripts/build.sh, scripts/test.sh, etc.)
    if "/" in pattern:
        # Extract the script name from the pattern
        pattern_name = os.path.basename(pattern)
        return command == pattern or command == pattern_name or command.endswith("/" + pattern_name)

    return False


def _validate_command_list(commands: list, config_path: Path, field_name: str) -> bool:
    """
    Validate a list of command entries from a YAML config.

    Each entry must be a dict with a non-empty string 'name' field.
    Used by both load_org_config() and load_project_commands() to avoid
    duplicating the same validation logic.

    Args:
        commands: List of command entries to validate
        config_path: Path to the config file (for log messages)
        field_name: Name of the YAML field being validated (e.g., 'allowed_commands', 'commands')

    Returns:
        True if all entries are valid, False otherwise
    """
    if not isinstance(commands, list):
        logger.warning(f"Config at {config_path}: '{field_name}' must be a list")
        return False
    for i, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            logger.warning(f"Config at {config_path}: {field_name}[{i}] must be a dict")
            return False
        if "name" not in cmd:
            logger.warning(f"Config at {config_path}: {field_name}[{i}] missing 'name'")
            return False
        if not isinstance(cmd["name"], str) or cmd["name"].strip() == "":
            logger.warning(f"Config at {config_path}: {field_name}[{i}] has invalid 'name'")
            return False
    return True


def _validate_pkill_processes(config: dict, config_path: Path) -> Optional[list[str]]:
    """
    Validate and normalize pkill_processes from a YAML config.

    Each entry must be a non-empty string matching VALID_PROCESS_NAME_PATTERN
    (alphanumeric, dots, underscores, hyphens only -- no regex metacharacters).
    Used by both load_org_config() and load_project_commands().

    Args:
        config: Parsed YAML config dict that may contain 'pkill_processes'
        config_path: Path to the config file (for log messages)

    Returns:
        Normalized list of process names, or None if validation fails.
        Returns an empty list if 'pkill_processes' is not present.
    """
    if "pkill_processes" not in config:
        return []

    processes = config["pkill_processes"]
    if not isinstance(processes, list):
        logger.warning(f"Config at {config_path}: 'pkill_processes' must be a list")
        return None

    normalized = []
    for i, proc in enumerate(processes):
        if not isinstance(proc, str):
            logger.warning(f"Config at {config_path}: pkill_processes[{i}] must be a string")
            return None
        proc = proc.strip()
        if not proc or not VALID_PROCESS_NAME_PATTERN.fullmatch(proc):
            logger.warning(f"Config at {config_path}: pkill_processes[{i}] has invalid value '{proc}'")
            return None
        normalized.append(proc)
    return normalized


def get_org_config_path() -> Path:
    """
    Get the organization-level config file path.

    Returns:
        Path to ~/.autoforge/config.yaml (falls back to ~/.autocoder/config.yaml)
    """
    new_path = Path.home() / ".autoforge" / "config.yaml"
    if new_path.exists():
        return new_path
    # Backward compatibility: check old location
    old_path = Path.home() / ".autocoder" / "config.yaml"
    if old_path.exists():
        return old_path
    return new_path


def load_org_config() -> Optional[dict]:
    """
    Load organization-level config from ~/.autoforge/config.yaml.

    Falls back to ~/.autocoder/config.yaml for backward compatibility.

    Returns:
        Dict with parsed org config, or None if file doesn't exist or is invalid
    """
    config_path = get_org_config_path()

    if not config_path.exists():
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        if not config:
            logger.warning(f"Org config at {config_path} is empty")
            return None

        # Validate structure
        if not isinstance(config, dict):
            logger.warning(f"Org config at {config_path} must be a YAML dictionary")
            return None

        if "version" not in config:
            logger.warning(f"Org config at {config_path} missing required 'version' field")
            return None

        # Validate allowed_commands if present
        if "allowed_commands" in config:
            if not _validate_command_list(config["allowed_commands"], config_path, "allowed_commands"):
                return None

        # Validate blocked_commands if present
        if "blocked_commands" in config:
            blocked = config["blocked_commands"]
            if not isinstance(blocked, list):
                logger.warning(f"Org config at {config_path}: 'blocked_commands' must be a list")
                return None
            for i, cmd in enumerate(blocked):
                if not isinstance(cmd, str):
                    logger.warning(f"Org config at {config_path}: blocked_commands[{i}] must be a string")
                    return None

        # Validate pkill_processes if present
        normalized = _validate_pkill_processes(config, config_path)
        if normalized is None:
            return None
        if normalized:
            config["pkill_processes"] = normalized

        return config

    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse org config at {config_path}: {e}")
        return None
    except (IOError, OSError) as e:
        logger.warning(f"Failed to read org config at {config_path}: {e}")
        return None


PROJECT_ALLOWLIST_NAME = "allowed_commands.yaml"
# Project-relative locations of the allowlist (current first, then the legacy fallback)
PROJECT_ALLOWLIST_FILES = (f".autoforge/{PROJECT_ALLOWLIST_NAME}", f".autocoder/{PROJECT_ALLOWLIST_NAME}")
# Commands that may name the allowlist in a Bash command (they only read it)
ALLOWLIST_READ_COMMANDS = frozenset({"cat", "head", "tail", "grep", "less", "more", "wc", "ls", "diff", "stat", "file"})


def protected_config_paths(project_dir: Path) -> list[str]:
    """Absolute paths of the project allowlist files, which only the user may edit.

    The allowlist widens what the agent may run, so agent settings deny the file
    tools on these paths and make them read-only inside the Bash sandbox.
    """
    root = project_dir.resolve()
    return [str(root / relative) for relative in PROJECT_ALLOWLIST_FILES]


def cli_absolute_rule(tool: str, path: PurePath, suffix: str = "") -> str:
    """Claude Code permission rule for an absolute path, e.g. ``Edit(//home/me/x)``.

    In Read/Edit rules ``/x`` is project-relative and ``//x`` absolute; the CLI
    matches Windows paths in POSIX form (``C:\\Users`` -> ``/c/Users``).
    """
    posix = path.as_posix()
    if len(posix) >= 2 and posix[1] == ":":
        posix = f"/{posix[0].lower()}{posix[2:]}"
    return f"{tool}(/{posix}{suffix})"


def load_project_commands(project_dir: Path) -> Optional[dict]:
    """
    Load allowed commands from project-specific YAML config.

    Args:
        project_dir: Path to the project directory

    Returns:
        Dict with parsed YAML config, or None if file doesn't exist or is invalid
    """
    # Check new location first, fall back to old for backward compatibility
    config_path = project_dir.resolve() / ".autoforge" / "allowed_commands.yaml"
    if not config_path.exists():
        config_path = project_dir.resolve() / ".autocoder" / "allowed_commands.yaml"

    if not config_path.exists():
        return None

    from api.safe_project_io import UnsafeProjectPathError, read_project_text

    try:
        # Never through a link: one planted by the agent in an earlier run would point
        # the allowlist at a file the agent can write
        try:
            config = yaml.safe_load(read_project_text(project_dir.resolve(), config_path))
        except UnsafeProjectPathError as e:
            logger.warning(f"Project config at {config_path} refused ({e}); using no project commands")
            return None

        if not config:
            logger.warning(f"Project config at {config_path} is empty")
            return None

        # Validate structure
        if not isinstance(config, dict):
            logger.warning(f"Project config at {config_path} must be a YAML dictionary")
            return None

        if "version" not in config:
            logger.warning(f"Project config at {config_path} missing required 'version' field")
            return None

        commands = config.get("commands", [])

        # Enforce 100 command limit
        if isinstance(commands, list) and len(commands) > 100:
            logger.warning(f"Project config at {config_path} exceeds 100 command limit ({len(commands)} commands)")
            return None

        # Validate each command entry using shared helper
        if not _validate_command_list(commands, config_path, "commands"):
            return None

        # Validate pkill_processes if present
        normalized = _validate_pkill_processes(config, config_path)
        if normalized is None:
            return None
        if normalized:
            config["pkill_processes"] = normalized

        return config

    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse project config at {config_path}: {e}")
        return None
    except (IOError, OSError) as e:
        logger.warning(f"Failed to read project config at {config_path}: {e}")
        return None


def validate_project_command(cmd_config: dict) -> tuple[bool, str]:
    """
    Validate a single command entry from project config.

    Checks that the command has a valid name and is not in any blocklist.
    Called during hierarchy resolution to gate each project command before
    it is added to the effective allowed set.

    Args:
        cmd_config: Dict with command configuration (name, description)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(cmd_config, dict):
        return False, "Command must be a dict"

    if "name" not in cmd_config:
        return False, "Command must have 'name' field"

    name = cmd_config["name"]
    if not isinstance(name, str) or not name:
        return False, "Command name must be a non-empty string"

    # Reject bare wildcard - security measure to prevent matching all commands
    if name == "*":
        return False, "Bare wildcard '*' is not allowed (security risk: matches all commands)"

    # Check if command is in the blocklist or dangerous commands
    base_cmd = os.path.basename(name.rstrip("*"))
    if base_cmd in BLOCKED_COMMANDS:
        return False, f"Command '{name}' is in the blocklist and cannot be allowed"
    if base_cmd in DANGEROUS_COMMANDS:
        return False, f"Command '{name}' is in the blocklist and cannot be allowed"

    # Description is optional
    if "description" in cmd_config and not isinstance(cmd_config["description"], str):
        return False, "Description must be a string"

    return True, ""


def get_effective_commands(project_dir: Optional[Path]) -> tuple[set[str], set[str]]:
    """
    Get effective allowed and blocked commands after hierarchy resolution.

    Hierarchy (highest to lowest priority):
    1. BLOCKED_COMMANDS (hardcoded) - always blocked
    2. Org blocked_commands - cannot be unblocked
    3. Org allowed_commands - adds to global
    4. Project allowed_commands - adds to global + org

    Args:
        project_dir: Path to the project directory, or None

    Returns:
        Tuple of (allowed_commands, blocked_commands)
    """
    # Start with global allowed commands
    allowed = ALLOWED_COMMANDS.copy()
    blocked = BLOCKED_COMMANDS.copy()

    # Add dangerous commands to blocked (Phase 3 will add approval flow)
    blocked |= DANGEROUS_COMMANDS

    # Load org config and apply
    org_config = load_org_config()
    if org_config:
        # Add org-level blocked commands (cannot be overridden)
        org_blocked = org_config.get("blocked_commands", [])
        blocked |= set(org_blocked)

        # Add org-level allowed commands
        for cmd_config in org_config.get("allowed_commands", []):
            if isinstance(cmd_config, dict) and "name" in cmd_config:
                allowed.add(cmd_config["name"])

    # Load project config and apply
    if project_dir:
        project_config = load_project_commands(project_dir)
        if project_config:
            # Add project-specific commands
            for cmd_config in project_config.get("commands", []):
                valid, error = validate_project_command(cmd_config)
                if valid:
                    allowed.add(cmd_config["name"])

    # Remove blocked commands from allowed (blocklist takes precedence)
    allowed -= blocked

    return allowed, blocked


def get_project_allowed_commands(project_dir: Optional[Path]) -> set[str]:
    """
    Get the set of allowed commands for a project.

    Uses hierarchy resolution from get_effective_commands().

    Args:
        project_dir: Path to the project directory, or None

    Returns:
        Set of allowed command names (including patterns)
    """
    allowed, blocked = get_effective_commands(project_dir)
    return allowed


def get_effective_pkill_processes(project_dir: Optional[Path]) -> set[str]:
    """
    Get effective pkill process names after hierarchy resolution.

    Merges processes from:
    1. DEFAULT_PKILL_PROCESSES (hardcoded baseline)
    2. Org config pkill_processes
    3. Project config pkill_processes

    Args:
        project_dir: Path to the project directory, or None

    Returns:
        Set of allowed process names for pkill
    """
    # Start with default processes
    processes = DEFAULT_PKILL_PROCESSES.copy()

    # Add org-level pkill_processes
    org_config = load_org_config()
    if org_config:
        org_processes = org_config.get("pkill_processes", [])
        if isinstance(org_processes, list):
            processes |= {p for p in org_processes if isinstance(p, str) and p.strip()}

    # Add project-level pkill_processes
    if project_dir:
        project_config = load_project_commands(project_dir)
        if project_config:
            proj_processes = project_config.get("pkill_processes", [])
            if isinstance(proj_processes, list):
                processes |= {p for p in proj_processes if isinstance(p, str) and p.strip()}

    return processes


def is_command_allowed(command: str, allowed_commands: set[str]) -> bool:
    """
    Check if a command is allowed (supports patterns).

    Args:
        command: The command to check
        allowed_commands: Set of allowed commands (may include patterns)

    Returns:
        True if command is allowed
    """
    # Check exact match first
    if command in allowed_commands:
        return True

    # Check pattern matches
    for pattern in allowed_commands:
        if matches_pattern(command, pattern):
            return True

    return False


SHELL_INTERPRETERS = {"bash", "sh"}

# Shell options that consume the next token as their argument
_SHELL_OPTIONS_WITH_ARGUMENT = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}

# bash -c "bash -c '...'" chains deeper than this are refused
MAX_NESTED_SHELL_DEPTH = 5

# Stands in for a $(...) / `...` / <(...) body in the enclosing line: the body is
# validated on its own, and the '$' keeps validators such as rm and pkill
# treating the value as dynamic (blocked)
_NESTED_PLACEHOLDER = "$(__nested__)"


def _matching_paren(source: str, start: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """Return the index of the ``close_ch`` closing the ``open_ch`` just before ``start``.

    Respects quotes and backslash escapes. Returns ``len(source)`` when the
    bracket is never closed, so the rest of the string is treated as nested
    code (fail closed).
    """
    depth = 1
    quote: str | None = None
    i = start
    while i < len(source):
        ch = source[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\":
            i += 1
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(source)


def _decode_ansi_c_string(body: str) -> str:
    """Decode the escapes of a bash $'...' string (\\n, \\t, \\', \\xHH, ...)."""
    import codecs

    return codecs.decode(body.encode("latin-1", "backslashreplace"), "unicode_escape")


def _substitutions_in_text(text: str) -> list[str]:
    """Return the bodies of $(...) and `...` found in text that expands them (e.g. a heredoc body)."""
    scripts = []
    i = 0
    while i < len(text):
        if text.startswith("$(", i):
            end = _matching_paren(text, i + 2)
            scripts.append(text[i + 2:end])
            i = end + 1
        elif text[i] == "`":
            end = text.find("`", i + 1)
            end = len(text) if end == -1 else end
            scripts.append(text[i + 1:end])
            i = end + 1
        else:
            i += 1
    return scripts


def _read_heredoc_delimiter(source: str, i: int) -> tuple[str, bool, bool, int]:
    """Parse the delimiter after '<<' at index i.

    Returns (delimiter, body_expands, strip_tabs, index_after_delimiter).
    """
    strip_tabs = source.startswith("-", i)
    if strip_tabs:
        i += 1
    while i < len(source) and source[i] in " \t":
        i += 1
    start = i
    while i < len(source) and source[i] not in " \t\n;&|<>()":
        i += 1
    word = source[start:i]
    expands = not any(q in word for q in ("'", '"', "\\"))
    delimiter = word.replace("'", "").replace('"', "").replace("\\", "")
    return delimiter, expands, strip_tabs, i


def _split_shell_source(source: str) -> tuple[list[str], list[str]]:
    """Split shell source into top-level command lines and nested scripts.

    The bash hook used to validate a command as one shlex token stream, so
    anything after a newline or inside $(...) / backticks was never checked.
    This scanner follows shell quoting rules closely enough to:

    - split on unquoted newlines (a backslash-newline continues the line),
    - drop comments,
    - collect the bodies of $(...), backticks and <(...) / >(...), which bash
      executes even inside double quotes (but not inside single quotes), and
      leave a placeholder in the line,
    - split at control operators (; && || | &) even without spaces,
    - skip heredoc bodies (data), while still collecting substitutions from
      bodies whose delimiter is unquoted (bash expands those).

    Returns:
        (lines, nested_scripts)
    """
    lines: list[str] = []
    nested: list[str] = []
    current: list[str] = []
    pending_heredocs: list[tuple[str, bool, bool]] = []
    quote: str | None = None
    i = 0
    n = len(source)

    def finish_line() -> None:
        line = "".join(current).strip()
        if line:
            lines.append(line)
        current.clear()

    while i < n:
        ch = source[i]

        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue

        if ch == "\\" and i + 1 < n:
            if source[i + 1] == "\n" and quote is None:
                i += 2  # line continuation
                continue
            current.append(source[i:i + 2])
            i += 2
            continue

        if ch == "`":
            end = source.find("`", i + 1)
            end = n if end == -1 else end
            nested.append(source[i + 1:end])
            current.append(_NESTED_PLACEHOLDER)
            i = end + 1
            continue

        if quote is None and source.startswith("$'", i):
            # ANSI-C quoting: backslash escapes are honoured, so \' does not end it.
            # Re-quote the decoded value so later shlex parsing sees what bash sees.
            j = i + 2
            while j < n and source[j] != "'":
                j += 2 if source[j] == "\\" else 1
            current.append(shlex.quote(_decode_ansi_c_string(source[i + 2:j])))
            i = j + 1
            continue

        if source.startswith("${", i) or source.startswith("$[", i):
            # Parameter expansion / legacy arithmetic: '<<' inside is not a heredoc,
            # but $(...) inside still runs
            close_ch = "}" if source[i + 1] == "{" else "]"
            end = _matching_paren(source, i + 2, source[i + 1], close_ch)
            nested.extend(_split_shell_source(source[i + 2:end])[1])
            current.append(source[i:end + 1])
            i = end + 1
            continue

        if quote is None and source.startswith("((", i):
            # Arithmetic command, e.g. for ((i=0; i<<2; i++)): may hold substitutions
            end = _matching_paren(source, i + 1)
            nested.extend(_split_shell_source(source[i + 2:end - 1])[1])
            current.append(source[i:end + 1])
            i = end + 1
            continue

        if source.startswith("$(", i) or (quote is None and source[i:i + 2] in ("<(", ">(")):
            end = _matching_paren(source, i + 2)
            body = source[i + 2:end]
            # bash reads $((...)) as arithmetic only when the inner '(' closes right
            # before the outer ')'; '$((a);(b))' and '<((a))' run commands
            if source.startswith("$((", i) and _matching_paren(source, i + 3) + 1 == end:
                # Arithmetic $((...)): not a command, but may hold substitutions
                nested.extend(_split_shell_source(body[1:-1])[1])
                current.append(source[i:end + 1])
            else:
                nested.append(body)
                current.append(_NESTED_PLACEHOLDER)
            i = end + 1
            continue

        if quote == '"':
            current.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue

        if ch == "#" and (not current or current[-1] in " \t;&|("):
            # Comment: skip to the end of the line
            while i < n and source[i] != "\n":
                i += 1
            continue

        if source.startswith("<<", i) and not source.startswith("<<<", i):
            delimiter, expands, strip_tabs, after = _read_heredoc_delimiter(source, i + 2)
            current.append(source[i:after])
            if delimiter:
                pending_heredocs.append((delimiter, expands, strip_tabs))
            i = after
            continue

        if ch in ";|&":
            # Control operators separate commands even without surrounding spaces
            # or right after a quote ("echo 'x';pkill" / "a&&b"). Redirections
            # (2>&1, &>file, >|file) are not operators.
            prev = current[-1] if current else ""
            if ch == "|" and prev == ">" or ch == "&" and (prev in ("<", ">") or source.startswith("&>", i)):
                current.append(ch)
                i += 1
                continue
            finish_line()
            i += 2 if source[i:i + 2] in (";;", "&&", "||", "|&") else 1
            continue

        if ch == "\n":
            finish_line()
            i += 1
            # Heredoc bodies start on the line after the operator
            for delimiter, expands, strip_tabs in pending_heredocs:
                body_lines = []
                terminated = False
                while i < n:
                    end = source.find("\n", i)
                    end = n if end == -1 else end
                    body_line = source[i:end]
                    i = end + 1
                    if (body_line.lstrip("\t") if strip_tabs else body_line) == delimiter:
                        terminated = True
                        break
                    body_lines.append(body_line)
                if not terminated:
                    # No delimiter line: our heredoc reading may differ from bash's,
                    # so check the would-be body as commands (fail closed)
                    sub_lines, sub_nested = _split_shell_source("\n".join(body_lines))
                    lines.extend(sub_lines)
                    nested.extend(sub_nested)
                elif expands:
                    nested.extend(_substitutions_in_text("\n".join(body_lines)))
            pending_heredocs.clear()
            continue

        current.append(ch)
        i += 1

    finish_line()
    return lines, nested


def _shell_c_payloads(line: str) -> list[str] | None:
    """Return the scripts passed to ``bash -c`` / ``sh -c`` (also ``-lc``, ``env bash -c``) in a line.

    Returns None when a segment that runs a shell can't be tokenized, so the
    caller can block instead of letting an unchecked payload through.
    """
    payloads = []
    for segment in split_command_segments(line):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            if SHELL_INTERPRETERS & set(extract_commands(segment)):
                return None
            continue
        for idx, token in enumerate(tokens):
            if os.path.basename(token) not in SHELL_INTERPRETERS:
                continue
            j = idx + 1
            has_c = False
            while j < len(tokens) and tokens[j][:1] in ("-", "+") and tokens[j] not in ("-", "--"):
                option = tokens[j]
                if option in _SHELL_OPTIONS_WITH_ARGUMENT:
                    j += 2
                    continue
                if not option.startswith("--") and "c" in option[1:]:
                    has_c = True
                j += 1
            if j < len(tokens) and tokens[j] == "--":
                j += 1
            if has_c and j < len(tokens):
                payloads.append(tokens[j])
    return payloads


def _validate_command_line(
    line: str,
    project_dir: Optional[Path],
    allowed_commands: set[str],
    blocked_commands: set[str],
    pkill_processes: set[str],
) -> dict:
    """Validate the commands of one line (no newlines or substitutions) against the allowlist."""
    commands = extract_commands(line)

    # The project allowlist widens what the agent may run: the agent may read it, never change it.
    # (The Bash sandbox also mounts it read-only; this gives the agent the reason.)
    if PROJECT_ALLOWLIST_NAME in line and (">" in line or any(c not in ALLOWLIST_READ_COMMANDS for c in commands)):
        return {
            "decision": "block",
            "reason": (
                f"Only the user can change {PROJECT_ALLOWLIST_FILES[0]}. "
                "Do not edit it; if a command is missing, tell the user which one and why."
            ),
        }

    # Split into segments for per-command validation
    segments = split_command_segments(line)

    # Check each command against the blocklist and allowlist
    for cmd in commands:
        # Check blocklist first (highest priority)
        if cmd in blocked_commands:
            return {
                "decision": "block",
                "reason": f"Command '{cmd}' is blocked at organization level and cannot be approved.",
            }

        # Check allowlist (with pattern matching)
        if not is_command_allowed(cmd, allowed_commands):
            # Provide helpful error message with config hint
            error_msg = f"Command '{cmd}' is not allowed.\n"
            error_msg += (
                f"Ask the user to add it to {PROJECT_ALLOWLIST_FILES[0]} (only the user can change that file).\n"
            )
            error_msg += "Note: Some commands are blocked at org-level and cannot be overridden."
            return {
                "decision": "block",
                "reason": error_msg,
            }

        # Additional validation for sensitive commands
        if cmd in COMMANDS_NEEDING_EXTRA_VALIDATION:
            # Validate EVERY segment containing this command, not just the
            # first one: 'rm -rf dist && rm -rf ../x' must check both.
            cmd_segments = [segment for segment in segments if cmd in extract_commands(segment)]
            if not cmd_segments:
                cmd_segments = [line]  # Fallback to full line

            for cmd_segment in cmd_segments:
                if cmd == "pkill":
                    # Pass configured extra processes (beyond defaults)
                    extra_procs = pkill_processes - DEFAULT_PKILL_PROCESSES
                    allowed, reason = validate_pkill_command(cmd_segment, extra_procs if extra_procs else None)
                elif cmd == "chmod":
                    allowed, reason = validate_chmod_command(cmd_segment)
                elif cmd == "init.sh":
                    allowed, reason = validate_init_script(cmd_segment)
                elif cmd == "playwright-cli":
                    allowed, reason = validate_playwright_command(cmd_segment)
                elif cmd == "rm":
                    allowed, reason = validate_rm_command(cmd_segment, project_dir)
                else:
                    continue
                if not allowed:
                    return {"decision": "block", "reason": reason}

    return {}


def _validate_shell_source(
    source: str,
    project_dir: Optional[Path],
    allowed_commands: set[str],
    blocked_commands: set[str],
    pkill_processes: set[str],
    depth: int = 0,
) -> dict:
    """Validate shell source line by line, recursing into bash -c payloads and substitutions."""
    if depth > MAX_NESTED_SHELL_DEPTH:
        return {"decision": "block", "reason": "Command nests shells or substitutions too deeply to validate"}

    lines, nested = _split_shell_source(source)
    validated = False

    def check_nested(script: str) -> dict:
        result = _validate_shell_source(
            script, project_dir, allowed_commands, blocked_commands, pkill_processes, depth + 1
        )
        if result:
            return {"decision": "block", "reason": f"In nested command {script!r}: {result['reason']}"}
        return {}

    for line in lines:
        if not extract_commands(line):
            try:
                shlex.split(line)
            except ValueError:
                return {"decision": "block", "reason": f"Could not parse command for security validation: {line}"}
            continue  # Parses but runs nothing (e.g. 'done', 'fi', a loop header)
        validated = True

        result = _validate_command_line(line, project_dir, allowed_commands, blocked_commands, pkill_processes)
        if result:
            return result

        payloads = _shell_c_payloads(line)
        if payloads is None:
            return {"decision": "block", "reason": f"Could not parse shell invocation for security validation: {line}"}
        for payload in payloads:
            result = check_nested(payload)
            if result:
                return result

    for script in nested:
        result = check_nested(script)
        if result:
            return result

    if not validated:
        # Could not find any command - fail safe by blocking
        return {
            "decision": "block",
            "reason": f"Could not parse command for security validation: {source}",
        }
    return {}


async def bash_security_hook(input_data, tool_use_id=None, context=None):
    """
    Pre-tool-use hook that validates bash commands using an allowlist.

    Validation reads YAML config files and parses the command synchronously,
    so it runs in a worker thread. Blocking the event loop here delays the
    hook response to the Claude Code CLI (and every other pending control
    message), which makes it more likely that the CLI aborts the hook call
    and logs "Error in hook callback ... AbortError" (issue #38).

    Args:
        input_data: Dict containing tool_name and tool_input
        tool_use_id: Optional tool use ID
        context: Optional context dict with 'project_dir' key

    Returns:
        Empty dict to allow, or {"decision": "block", "reason": "..."} to block
    """
    return await asyncio.to_thread(validate_bash_tool_use, input_data, context)


def validate_bash_tool_use(input_data, context=None):
    """
    Validate a Bash tool use against the allowlist (synchronous core of
    bash_security_hook).

    Only commands in ALLOWED_COMMANDS and project-specific commands are permitted.

    Args:
        input_data: Dict containing tool_name and tool_input
        context: Optional context dict with 'project_dir' key

    Returns:
        Empty dict to allow, or {"decision": "block", "reason": "..."} to block
    """
    if input_data.get("tool_name") != "Bash":
        return {}

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        return {}

    # Get project directory from context
    project_dir = None
    if context and isinstance(context, dict):
        project_dir_str = context.get("project_dir")
        if project_dir_str:
            project_dir = Path(project_dir_str)

    # Get effective commands using hierarchy resolution
    allowed_commands, blocked_commands = get_effective_commands(project_dir)

    # Get effective pkill processes (includes org/project config)
    pkill_processes = get_effective_pkill_processes(project_dir)

    try:
        return _validate_shell_source(command, project_dir, allowed_commands, blocked_commands, pkill_processes)
    except Exception as e:  # Fail closed: a parser error must never let a command through
        logger.warning("bash security hook failed to validate %r: %s", command, e)
        return {"decision": "block", "reason": f"Could not validate command ({type(e).__name__}): {command}"}
