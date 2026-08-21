from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import ToolFailure


HumanHelpRequester = Callable[..., dict[str, Any]]
ComputerUseRequester = Callable[..., dict[str, Any]]


def human_help_tool(
    args: dict[str, Any],
    *,
    request_human_help: HumanHelpRequester,
) -> dict[str, Any]:
    """Escalate one deliberately small blocking step to the human operator."""

    request = str(args.get("request") or "").strip()
    expected_result = str(args.get("expected_result") or "").strip()
    return_to_agent = str(args.get("return_to_agent") or "").strip()
    reason = str(args.get("reason") or "other")
    mode = str(args.get("mode") or "prefer_human")
    fallback = str(args.get("fallback") or "continue_best_effort")
    delivery = str(args.get("delivery") or "auto")
    timeout_seconds = int(args.get("timeout_seconds") or 60)

    if delivery != "chat_only":
        try:
            response = request_human_help(
                reason=reason,
                request=request,
                expected_result=expected_result,
                return_to_agent=return_to_agent,
                mode=mode,
                fallback=fallback,
                timeout_seconds=timeout_seconds,
            )
            outcome = str(response.get("outcome") or "unknown")
            if outcome in {"submitted", "done"}:
                return {
                    "ok": True,
                    "status": "human_completed",
                    "delivery": "desktop_qa",
                    "reason": reason,
                    "request": request,
                    "answer": str(response.get("answer") or ""),
                    "outcome": outcome,
                    "agent_action": "resume_from_human_result",
                }
            return {
                "ok": True,
                "status": "human_declined" if outcome == "skip" else "human_unavailable",
                "delivery": "desktop_qa",
                "reason": reason,
                "request": request,
                "answer": str(response.get("answer") or ""),
                "outcome": outcome,
                "agent_action": "continue_best_effort" if fallback == "continue_best_effort" else "wait_for_human",
                "agent_guidance": (
                    "The human skipped or did not answer. Continue with the best safe agent path; do not repeat the same human request immediately."
                    if fallback == "continue_best_effort"
                    else "The human did not complete this blocking step. Stop this branch until they explicitly return to it."
                ),
            }
        except ToolFailure as exc:
            # Desktop prompting is a convenience layer. If it is unavailable,
            # fall back to a model-visible handoff instead of failing the tool.
            desktop_error = {"code": exc.code, "message": exc.message}
    else:
        desktop_error = None

    return {
        "ok": True,
        "status": "human_action_required",
        "delivery": "chat",
        "visibility": "must_surface_to_user",
        "reason": reason,
        "mode": mode,
        "fallback": fallback,
        "request": request,
        "expected_result": expected_result,
        "return_to_agent": return_to_agent,
        "desktop_error": desktop_error,
        "agent_action": "ask_user_visibly",
        "agent_guidance": (
            "Immediately show this exact small request in the assistant's visible reply; never assume MCP tool calls/results are visible to the human. "
            "Tell them they may skip it and ask you to continue if fallback=continue_best_effort."
        ),
    }


def desktop_ui_action(
    args: dict[str, Any],
    *,
    browser_only: bool,
    request_computer_use: ComputerUseRequester,
) -> dict[str, Any]:
    action = str(args.get("action") or "inspect").strip().lower()
    text = str(args.get("text") or "")
    if browser_only and action == "navigate":
        text = str(args.get("url") or "").strip()
    response = request_computer_use(
        action=action,
        window_id=(int(args["window_id"]) if args.get("window_id") is not None else None),
        title=str(args.get("title") or ""),
        process_name=str(args.get("process_name") or ""),
        x=(int(args["x"]) if args.get("x") is not None else None),
        y=(int(args["y"]) if args.get("y") is not None else None),
        element_index=(int(args["element_index"]) if args.get("element_index") is not None else None),
        text=text,
        key=str(args.get("key") or ""),
        scroll_y=int(args.get("scroll_y") or 0),
        include_screenshot=bool(args.get("include_screenshot", True)),
        include_text=bool(args.get("include_text", True)),
        browser_only=browser_only,
        timeout_seconds=float(args.get("timeout_seconds") or 30),
    )
    response["surface"] = "browser" if browser_only else "windows"
    response["skill"] = (
        "coding-tools-mcp/skills/control-chrome/SKILL.md"
        if browser_only
        else "coding-tools-mcp/skills/computer-use/SKILL.md"
    )
    return response


__all__ = ["desktop_ui_action", "human_help_tool"]
