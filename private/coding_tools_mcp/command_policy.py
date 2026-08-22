from __future__ import annotations

import re
import shlex
import shutil
from pathlib import PurePosixPath
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .errors import ToolFailure
from .workspace import Workspace


SHELL_CONTROL_TOKENS = {"|", "||", "&", "&&", ";", "(", ")"}
REDIRECTION_TOKENS = {">", ">>", "<", "<>", ">&", "<&", "&>", "&>>"}
HEREDOC_TOKENS = {"<<", "<<<"}
PATH_ARGUMENT_COMMANDS = {
    "cat",
    "cd",
    "chdir",
    "chmod",
    "chown",
    "cp",
    "head",
    "less",
    "ln",
    "ls",
    "mkdir",
    "more",
    "mv",
    "rm",
    "rmdir",
    "stat",
    "tail",
    "touch",
    "wc",
}
PATTERN_THEN_PATH_COMMANDS = {"grep", "egrep", "fgrep", "rg", "sed", "awk"}
SCRIPT_COMMANDS = {"bash", "sh", "zsh", "python", "python3", "node", "ruby", "perl"}
ENV_OPTIONS_WITH_ARGUMENT = {
    "-u",
    "--unset",
    "-C",
    "--chdir",
    "-S",
    "--split-string",
    "-a",
    "--argv0",
}
ENV_LONG_OPTIONS_WITH_ARGUMENT = {
    "--unset",
    "--chdir",
    "--split-string",
    "--argv0",
}
ENV_LONG_OPTIONS_WITH_OPTIONAL_ARGUMENT = {
    "--ignore-signal",
    "--default-signal",
    "--block-signal",
}
ENV_SHORT_OPTIONS_WITH_ATTACHED_ARGUMENT = ("-u", "-C", "-S", "-a")
ENV_FLAG_OPTIONS = {
    "-i",
    "--ignore-environment",
    "-0",
    "--null",
    "-v",
    "--debug",
    "--ignore-signal",
    "--default-signal",
    "--block-signal",
    "--list-signal-handling",
}
NETWORK_LITERAL_COMMANDS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc"}
NETWORK_RE = re.compile(
    r"(https?://|urllib\.request|urllib3|requests\.|http\.client|\bHTTPConnection\b|\bHTTPSConnection\b|socket\.|aiohttp|httpx|\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bssh\b|\bscp\b|\bftp\b)",
    re.I,
)
SHELL_EXPANSION_RE = re.compile(r"(`|\$\(|\$\{)")
DESTRUCTIVE_RE = re.compile(
    r"(^|\s)(sudo|su|chmod\s+-R|chown\s+-R|mkfs|mount|umount|find\b[^;&|]*\s-delete\b|git\b[^;&|]*\breset\s+--hard\b|git\b[^;&|]*\bclean\s+-[^\s]*[fx][^\s]*|rm\s+-[^\s]*r[^\s]*f|rm\s+-[^\s]*f[^\s]*r)\b",
    re.I,
)


def shlex_split(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def parse_heredoc_delimiter(command: str, start: int) -> tuple[int, str, bool]:
    index = start
    length = len(command)
    strip_tabs = False
    if index < length and command[index] == "-":
        strip_tabs = True
        index += 1
    while index < length and command[index] in " \t":
        index += 1
    delimiter: list[str] = []
    while index < length:
        char = command[index]
        if char in "'\"":
            quote = char
            index += 1
            while index < length and command[index] != quote:
                delimiter.append(command[index])
                index += 1
            if index < length:
                index += 1
            continue
        if char == "\\" and index + 1 < length:
            delimiter.append(command[index + 1])
            index += 2
            continue
        if char.isspace() or char in ";&|<>()":
            break
        delimiter.append(char)
        index += 1
    return index, "".join(delimiter), strip_tabs


def strip_heredoc_payloads(command: str) -> str:
    """Drop heredoc body lines so command scanning sees only live shell code.

    Heredoc bodies are stdin data, not code: scanning XML payloads produces fake
    escape candidates such as ``/modelVersion`` from ``</modelVersion>``. Bash
    starts the body on the line after the operator, so everything else stays
    visible to the scanner: redirections on the operator's own line
    (``cat <<EOF > /etc/cron.d/evil``) and commands after the closing delimiter.
    ``<<`` inside quotes or inside ``((...))`` arithmetic never opens a heredoc,
    which keeps fake heredocs from hiding live commands; an unterminated heredoc
    swallows the remaining lines exactly as bash treats them (as body).
    """
    if "<<" not in command:
        return command
    live: list[str] = []
    pending: list[tuple[str, bool]] = []
    index = 0
    length = len(command)
    in_single = False
    in_double = False
    arith_parens = 0
    while index < length:
        char = command[index]
        if in_single:
            live.append(char)
            in_single = char != "'"
            index += 1
            continue
        if in_double:
            if char == "\\" and index + 1 < length:
                live.append(command[index : index + 2])
                index += 2
                continue
            live.append(char)
            in_double = char != '"'
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            live.append(command[index : index + 2])
            index += 2
            continue
        if char == "'":
            in_single = True
            live.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            live.append(char)
            index += 1
            continue
        if arith_parens:
            if char == "(":
                arith_parens += 1
            elif char == ")":
                arith_parens -= 1
            live.append(char)
            index += 1
            continue
        if char == "(" and command[index : index + 2] == "((":
            arith_parens = 2
            live.append("((")
            index += 2
            continue
        if char == "<" and command[index : index + 3] == "<<<":
            live.append("<<<")
            index += 3
            continue
        if char == "<" and command[index : index + 2] == "<<":
            operator_end, delimiter, strip_tabs = parse_heredoc_delimiter(command, index + 2)
            live.append(command[index:operator_end])
            index = operator_end
            if delimiter:
                pending.append((delimiter, strip_tabs))
            continue
        if char == "\n":
            live.append(char)
            index += 1
            for delimiter, strip_tabs in pending:
                while index < length:
                    line_end = command.find("\n", index)
                    if line_end < 0:
                        line_end = length
                    line = command[index:line_end].rstrip("\r")
                    index = line_end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
            pending = []
            continue
        live.append(char)
        index += 1
    return "".join(live)


def command_executables(tokens: list[str]) -> list[str]:
    executables: list[str] = []
    expect_command = True
    for index, token in enumerate(tokens):
        if not token:
            continue
        if token in SHELL_CONTROL_TOKENS:
            expect_command = True
            continue
        if token in REDIRECTION_TOKENS or token in HEREDOC_TOKENS:
            expect_command = False
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTION_TOKENS:
            continue
        if expect_command:
            if is_env_assignment_token(token):
                continue
            executables.append(token)
            expect_command = False
    return executables


def explicit_command_path_candidates(tokens: list[str]) -> list[str]:
    candidates: list[str] = []
    index = 0
    current_command: str | None = None
    current_args: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_CONTROL_TOKENS:
            candidates.extend(command_argument_path_candidates(current_command, current_args))
            current_command = None
            current_args = []
            index += 1
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTION_TOKENS:
            index += 1
            continue
        if token in REDIRECTION_TOKENS:
            if index + 1 < len(tokens):
                candidates.append(tokens[index + 1])
            index += 2
            continue
        if token in HEREDOC_TOKENS:
            index += 2
            continue
        if current_command is None:
            if not is_env_assignment_token(token):
                current_command = token
        else:
            current_args.append(token)
        index += 1
    candidates.extend(command_argument_path_candidates(current_command, current_args))
    return list(dict.fromkeys(candidates))


def command_argument_path_candidates(command: str | None, args: list[str]) -> list[str]:
    if not command:
        return []
    name = PurePosixPath(command.replace("\\", "/")).name.lower()
    if name == "env":
        candidates, wrapped_command, wrapped_args = env_wrapped_command(args)
        if wrapped_command is not None:
            candidates.extend(command_argument_path_candidates(wrapped_command, wrapped_args))
        return candidates
    if name in PATH_ARGUMENT_COMMANDS:
        return [arg for arg in args if is_inspectable_path_argument(arg)]
    if name in PATTERN_THEN_PATH_COMMANDS:
        return pattern_command_path_candidates(args)
    if name == "find":
        return find_command_path_candidates(args)
    if name in SCRIPT_COMMANDS:
        return script_command_path_candidates(name, args)
    return []


def inline_script_command(command: str) -> dict[str, str] | None:
    try:
        tokens = shlex_split(command)
    except ValueError:
        tokens = command.split()
    index = 0
    current_command: str | None = None
    current_args: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_CONTROL_TOKENS:
            result = inline_script_segment(current_command, current_args)
            if result is not None:
                return result
            current_command = None
            current_args = []
            index += 1
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTION_TOKENS:
            index += 1
            continue
        if token in HEREDOC_TOKENS:
            result = stdin_script_segment(current_command, current_args, token)
            if result is not None:
                return result
            index += 2
            continue
        if token in REDIRECTION_TOKENS:
            index += 2
            continue
        if current_command is None:
            if not is_env_assignment_token(token):
                current_command = token
        else:
            current_args.append(token)
        index += 1
    return inline_script_segment(current_command, current_args)


def inline_script_segment(command: str | None, args: list[str]) -> dict[str, str] | None:
    if not command:
        return None
    name = PurePosixPath(command.replace("\\", "/")).name.lower()
    if name == "env":
        _candidates, wrapped_command, wrapped_args = env_wrapped_command(args)
        return inline_script_segment(wrapped_command, wrapped_args)
    if name in {"bash", "sh", "zsh"}:
        for arg in args:
            if arg.startswith("-") and "c" in arg.lstrip("-"):
                return {"command": name, "option": arg}
        return None
    if name in {"python", "python3"}:
        if "-c" in args:
            return {"command": name, "option": "-c"}
        if "-" in args:
            return {"command": name, "option": "-"}
        return None
    if name == "node":
        for option in ("-e", "--eval", "-p", "--print"):
            if option in args:
                return {"command": name, "option": option}
    if name in {"ruby", "perl"} and "-e" in args:
        return {"command": name, "option": "-e"}
    return None


def env_wrapped_command(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    candidates: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in {"-S", "--split-string"}:
            if index + 1 >= len(args):
                return candidates, None, []
            return env_split_command(candidates, args[index + 1])
        if arg.startswith("--split-string="):
            return env_split_command(candidates, arg.split("=", 1)[1])
        if arg.startswith("-S") and arg != "-S":
            return env_split_command(candidates, arg[2:])
        if arg in {"-C", "--chdir"}:
            if index + 1 >= len(args):
                return candidates, None, []
            candidates.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--chdir="):
            candidates.append(arg.split("=", 1)[1])
            index += 1
            continue
        if arg.startswith("-C") and arg != "-C":
            candidates.append(arg[2:])
            index += 1
            continue
        if arg in ENV_OPTIONS_WITH_ARGUMENT:
            index += 2
            continue
        if any(arg.startswith(f"{option}=") for option in ENV_LONG_OPTIONS_WITH_ARGUMENT):
            index += 1
            continue
        if any(arg.startswith(f"{option}=") for option in ENV_LONG_OPTIONS_WITH_OPTIONAL_ARGUMENT):
            index += 1
            continue
        if any(arg.startswith(prefix) and arg != prefix for prefix in ENV_SHORT_OPTIONS_WITH_ATTACHED_ARGUMENT):
            index += 1
            continue
        if arg in ENV_FLAG_OPTIONS:
            index += 1
            continue
        if arg.startswith("-") or is_env_assignment_token(arg):
            index += 1
            continue
        return candidates, arg, args[index + 1 :]
    if index < len(args):
        return candidates, args[index], args[index + 1 :]
    return candidates, None, []


def env_split_command(candidates: list[str], command: str) -> tuple[list[str], str | None, list[str]]:
    try:
        tokens = shlex_split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return candidates, None, []
    return candidates, tokens[0], tokens[1:]


def stdin_script_segment(command: str | None, args: list[str], redirection: str) -> dict[str, str] | None:
    if not command:
        return None
    name = PurePosixPath(command.replace("\\", "/")).name.lower()
    if name not in SCRIPT_COMMANDS:
        return None
    if name in {"python", "python3"} and "-m" in args:
        return None
    for arg in args:
        if not arg.startswith("-") or arg == "-":
            return None
    return {"command": name, "option": redirection}


def pattern_command_path_candidates(args: list[str]) -> list[str]:
    candidates: list[str] = []
    pattern_consumed = False
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-e", "-f", "--regexp", "--file", "-g", "--glob"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if not pattern_consumed:
            pattern_consumed = True
            continue
        if is_inspectable_path_argument(arg):
            candidates.append(arg)
    return candidates


def find_command_path_candidates(args: list[str]) -> list[str]:
    candidates: list[str] = []
    for arg in args:
        if arg in {"!", "(", ")"} or arg.startswith("-"):
            break
        if is_inspectable_path_argument(arg):
            candidates.append(arg)
    return candidates


def script_command_path_candidates(command_name: str, args: list[str]) -> list[str]:
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if command_name in {"bash", "sh", "zsh"} and arg.startswith("-") and "c" in arg.lstrip("-"):
            return []
        if command_name in {"python", "python3"} and arg == "-c":
            return []
        if command_name == "node" and arg in {"-e", "--eval", "-p", "--print"}:
            return []
        if command_name in {"ruby", "perl"} and arg == "-e":
            return []
        if arg in {"-m", "--require", "-r"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if command_name.startswith("python") and arg == "-":
            return []
        return [arg] if is_inspectable_path_argument(arg) else []
    return []


def is_env_assignment_token(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def is_inspectable_path_argument(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    normalized = token.replace("\\", "/")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", normalized):
        return False
    if normalized.startswith(("/", "~", "./", "../")) or re.match(r"^[A-Za-z]:/", normalized):
        return True
    if "/" in normalized:
        return True
    return "." in PurePosixPath(normalized).name


def is_literal_network_reference_command(command: str) -> bool:
    try:
        tokens = shlex_split(command)
    except ValueError:
        return False
    executables = command_executables(tokens)
    if not executables:
        return False
    return all(
        PurePosixPath(executable.replace("\\", "/")).name.lower() in NETWORK_LITERAL_COMMANDS
        for executable in executables
    )


class CommandPolicy:
    """Evaluate command safety using explicit Runtime-owned dependencies."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        permission_granted: Callable[[str], bool],
        dangerously_skip_all_permissions: bool,
        allow_network: bool,
        inline_script_allowed: bool,
        shell_expansion_allowed: bool,
        inline_script_permission: str,
        is_filtered_env_var: Callable[[str, str], bool],
        is_allowed_tmp_path: Callable[[str], bool],
        is_allowed_external_executable: Callable[[str], bool],
        special_device_paths: tuple[str, ...],
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.workspace = workspace
        self.permission_granted = permission_granted
        self.dangerously_skip_all_permissions = dangerously_skip_all_permissions
        self.allow_network = allow_network
        self.inline_script_allowed = inline_script_allowed
        self.shell_expansion_allowed = shell_expansion_allowed
        self.inline_script_permission = inline_script_permission
        self.is_filtered_env_var = is_filtered_env_var
        self.is_allowed_tmp_path = is_allowed_tmp_path
        self.is_allowed_external_executable = is_allowed_external_executable
        self.special_device_paths = special_device_paths
        self.which = which

    def check(self, cmd: str, args: dict[str, Any]) -> None:
        execution_context = str(args.get("execution_context", "service") or "service").strip().lower()
        if (
            execution_context == "active_user"
            and not self.dangerously_skip_all_permissions
            and not self.permission_granted("interactive_session")
        ):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Running a command in the signed-in user's interactive desktop requires explicit permission.",
                category="permission",
                details={
                    "permission": "interactive_session",
                    "execution_context": "active_user",
                    "os_privileges": "signed-in user, non-elevated",
                },
            )
        if self.dangerously_skip_all_permissions:
            return
        self.check_paths(cmd)
        env = args.get("env", {})
        if isinstance(env, dict) and any(
            self.is_filtered_env_var(str(key), str(value)) for key, value in env.items()
        ) and not self.permission_granted("sensitive_env"):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Sensitive or loader/startup environment variables require explicit permission.",
                category="permission",
                details={"permission": "sensitive_env", "env_keys": sorted(str(key) for key in env)},
            )
        if not self.inline_script_allowed and not self.permission_granted(self.inline_script_permission):
            inline_script = inline_script_command(cmd)
            if inline_script is not None:
                raise ToolFailure(
                    "PERMISSION_REQUIRED",
                    "Inline interpreter or shell code requires explicit permission because network and filesystem effects cannot be verified statically.",
                    category="permission",
                    details={"permission": self.inline_script_permission, **inline_script},
                )
        compact = " ".join(cmd.split()).lower()
        if not self.shell_expansion_allowed and SHELL_EXPANSION_RE.search(cmd) and not self.permission_granted("shell_expansion"):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Shell command substitution and parameter expansion require explicit permission.",
                category="permission",
                details={"permission": "shell_expansion", "command": compact},
            )
        if re.search(r"(^|[;&|]\s*)rm\s+(-[^\s]*r[^\s]*f|-?[^\s]*f[^\s]*r)\s+/", compact) and not self.permission_granted("destructive_command"):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command", "command": compact},
            )
        if DESTRUCTIVE_RE.search(cmd) and not self.permission_granted("destructive_command"):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command", "command": compact},
            )
        if (
            not self.allow_network
            and NETWORK_RE.search(cmd)
            and not is_literal_network_reference_command(cmd)
            and not self.permission_granted("network")
        ):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Network access is denied by default.",
                category="permission",
                details={"permission": "network", "command": compact},
            )

    def check_paths(self, cmd: str) -> None:
        scannable = strip_heredoc_payloads(cmd)
        try:
            tokens = shlex_split(scannable)
        except ValueError:
            tokens = scannable.split()
        for executable in command_executables(tokens):
            self.reject_setuid_executable(executable)
            normalized_executable = executable.replace("\\", "/")
            if normalized_executable.startswith("/") or re.match(r"^[A-Za-z]:/", normalized_executable):
                self.check_path_candidate(executable)
        for candidate in explicit_command_path_candidates(tokens):
            self.check_path_candidate(candidate)

    def check_path_candidate(self, candidate: str) -> None:
        candidate = candidate.strip()
        if not candidate or candidate in {"-", "--"}:
            return
        if self.permission_granted("filesystem_escape"):
            return

        def escape_failure() -> ToolFailure:
            return ToolFailure(
                "PERMISSION_REQUIRED",
                "Command path escapes the workspace and is blocked.",
                category="permission",
                details={"permission": "filesystem_escape", "path": candidate},
            )

        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", candidate):
            return
        normalized = candidate.replace("\\", "/")
        if normalized in self.special_device_paths:
            return
        if self.is_allowed_tmp_path(normalized):
            return
        absolute_candidate = (
            normalized.startswith("/")
            or normalized.startswith("~")
            or re.match(r"^[A-Za-z]:/", normalized)
        )
        if absolute_candidate:
            try:
                self.workspace.resolve_existing(normalized)
                return
            except ToolFailure as exc:
                if exc.code == "NOT_FOUND":
                    try:
                        self.workspace.resolve_for_write(normalized)
                        return
                    except ToolFailure as write_exc:
                        if write_exc.code not in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                            return
                if self.is_allowed_external_executable(candidate):
                    return
                raise escape_failure() from exc
        if any(part == ".." for part in PurePosixPath(normalized).parts):
            raise escape_failure()
        try:
            self.workspace.resolve_existing(normalized)
        except OSError as exc:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "Command path could not be inspected safely.",
                category="validation",
                details={"path": candidate[:200], "errno": exc.errno, "reason": exc.strerror},
            ) from exc
        except ToolFailure as exc:
            if exc.code == "NOT_FOUND":
                try:
                    self.workspace.resolve_for_write(normalized)
                except ToolFailure as write_exc:
                    if write_exc.code == "NOT_FOUND":
                        return
                    if write_exc.code in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                        raise escape_failure() from write_exc
                    raise
                return
            if exc.code in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                raise escape_failure() from exc

    def reject_setuid_executable(self, executable: str) -> None:
        if not executable:
            return
        executable_path = Path(executable) if "/" in executable else Path(self.which(executable) or "")
        if not str(executable_path):
            return
        try:
            file_stat = executable_path.stat()
        except OSError:
            return
        if file_stat.st_mode & 0o6000:
            if self.permission_granted("privileged_executable"):
                return
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Setuid/setgid executables are denied because they can bypass runtime process guards.",
                category="permission",
                details={"permission": "privileged_executable", "path": str(executable_path)},
            )


__all__ = [
    "CommandPolicy",
    "DESTRUCTIVE_RE",
    "ENV_FLAG_OPTIONS",
    "ENV_LONG_OPTIONS_WITH_ARGUMENT",
    "ENV_LONG_OPTIONS_WITH_OPTIONAL_ARGUMENT",
    "ENV_OPTIONS_WITH_ARGUMENT",
    "ENV_SHORT_OPTIONS_WITH_ATTACHED_ARGUMENT",
    "HEREDOC_TOKENS",
    "NETWORK_LITERAL_COMMANDS",
    "NETWORK_RE",
    "PATH_ARGUMENT_COMMANDS",
    "PATTERN_THEN_PATH_COMMANDS",
    "REDIRECTION_TOKENS",
    "SCRIPT_COMMANDS",
    "SHELL_EXPANSION_RE",
    "SHELL_CONTROL_TOKENS",
    "command_argument_path_candidates",
    "command_executables",
    "env_split_command",
    "env_wrapped_command",
    "explicit_command_path_candidates",
    "find_command_path_candidates",
    "inline_script_command",
    "inline_script_segment",
    "is_env_assignment_token",
    "is_inspectable_path_argument",
    "is_literal_network_reference_command",
    "parse_heredoc_delimiter",
    "pattern_command_path_candidates",
    "script_command_path_candidates",
    "shlex_split",
    "stdin_script_segment",
    "strip_heredoc_payloads",
]
