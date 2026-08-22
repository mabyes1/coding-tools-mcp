from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def run_command_policy_checks(server: Any) -> None:
    heredoc_command = "cat <<EOF > /etc/cron.d/evil\n</modelVersion>\nEOF\necho done\n"
    heredoc_live = server.strip_heredoc_payloads(heredoc_command)
    if "</modelVersion>" in heredoc_live:
        raise RuntimeError("heredoc payload stripping stopped removing stdin body data")
    if "> /etc/cron.d/evil" not in heredoc_live or "echo done" not in heredoc_live:
        raise RuntimeError("heredoc payload stripping hid live redirection/commands")
    quoted_heredoc = "printf '%s\\n' '<<EOF'\necho live\n"
    if server.strip_heredoc_payloads(quoted_heredoc) != quoted_heredoc:
        raise RuntimeError("quoted heredoc marker started being treated as a live heredoc")

    parsed_tokens = server.shlex_split("FOO=1 echo hi | cat ./file.txt")
    if server.command_executables(parsed_tokens) != ["echo", "cat"]:
        raise RuntimeError("shell executable discovery contract drifted")
    path_candidates = set(
        server.explicit_command_path_candidates(
            server.shlex_split("env -C ./sub FOO=1 python ./script.py > ./out.txt")
        )
    )
    if path_candidates != {"./sub", "./script.py", "./out.txt"}:
        raise RuntimeError("env-wrapped command path discovery contract drifted")
    env_candidates, env_command, env_args = server.env_wrapped_command(
        ["-C", "./sub", "FOO=1", "python", "./script.py"]
    )
    if env_candidates != ["./sub"] or env_command != "python" or env_args != ["./script.py"]:
        raise RuntimeError("env wrapped-command parsing contract drifted")
    if server.inline_script_command("env FOO=1 python -c 'print(1)'") != {
        "command": "python",
        "option": "-c",
    }:
        raise RuntimeError("inline-script detection contract drifted")

    inspectable_cases = {
        "file.txt": True,
        "./file": True,
        "../file": True,
        "https://example.invalid/file.txt": False,
        "bareword": False,
    }
    for candidate, expected in inspectable_cases.items():
        if server.is_inspectable_path_argument(candidate) is not expected:
            raise RuntimeError(f"inspectable path classification drifted for {candidate!r}")
    if not server.is_literal_network_reference_command("echo https://example.invalid/path"):
        raise RuntimeError("literal-network echo command stopped being classified as data-only")
    if server.is_literal_network_reference_command("curl https://example.invalid/path"):
        raise RuntimeError("network-capable curl command was misclassified as literal-only")

    with tempfile.TemporaryDirectory(prefix="coding-tools-command-policy-") as temporary:
        policy_workspace = Path(temporary)
        policy_runtime = server.Runtime(policy_workspace, enable_view_image=False, permission_mode="safe")
        try:
            policy_cases = [
                ("active_user", "echo hi", {"execution_context": "active_user"}, "interactive_session"),
                ("sensitive_env", "echo hi", {"env": {"LD_PRELOAD": "./hook.so"}}, "sensitive_env"),
                ("inline_script", "python -c 'print(1)'", {}, server.INLINE_SCRIPT_PERMISSION),
                ("shell_expansion", "echo $(whoami)", {}, "shell_expansion"),
                ("destructive_command", "git reset --hard HEAD", {}, "destructive_command"),
                ("network", "curl https://example.invalid/path", {}, "network"),
                ("filesystem_escape", "cat ../outside.txt", {}, "filesystem_escape"),
            ]
            for label, command, arguments, permission in policy_cases:
                try:
                    policy_runtime._check_command_policy(command, arguments)
                except server.ToolFailure as exc:
                    if exc.code != "PERMISSION_REQUIRED" or exc.category != "permission":
                        raise RuntimeError(f"{label} command-policy error contract drifted") from exc
                    if exc.details.get("permission") != permission:
                        raise RuntimeError(f"{label} command-policy permission detail drifted") from exc
                else:
                    raise RuntimeError(f"{label} command-policy gate stopped rejecting the operation")

            policy_runtime._check_command_policy("echo https://example.invalid/path", {})
        finally:
            policy_runtime.close()
