from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import human_help_mcp as hh  # noqa: E402


class HumanHelpMcpTests(unittest.TestCase):
    def test_initialize_and_list_one_tool(self) -> None:
        response, initialized = hh.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": hh.PROTOCOL_VERSION},
            },
            False,
        )
        self.assertTrue(initialized)
        self.assertEqual(response["result"]["serverInfo"]["name"], "human-help-mcp")

        response, initialized = hh.dispatch(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            initialized,
        )
        tools = response["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["human_help_me"])

    def test_chat_only_never_requires_windows_broker(self) -> None:
        payload = hh.human_help_tool(
            {
                "reason": "need_decision",
                "request": "Choose A or B.",
                "delivery": "chat_only",
            }
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "human_action_required")
        self.assertEqual(payload["delivery"], "chat")
        self.assertEqual(payload["agent_action"], "ask_user_visibly")

    def test_tool_call_returns_structured_content(self) -> None:
        response, initialized = hh.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": hh.PROTOCOL_VERSION},
            },
            False,
        )
        self.assertTrue(initialized)

        response, _ = hh.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "human_help_me",
                    "arguments": {
                        "reason": "need_information",
                        "request": "What value should I use?",
                        "delivery": "chat_only",
                    },
                },
            },
            initialized,
        )
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "human_action_required",
        )
        decoded = json.loads(result["content"][0]["text"])
        self.assertEqual(decoded, result["structuredContent"])

    def test_invalid_arguments_return_tool_error(self) -> None:
        response, initialized = hh.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": hh.PROTOCOL_VERSION},
            },
            False,
        )
        self.assertTrue(initialized)

        response, _ = hh.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "human_help_me",
                    "arguments": {
                        "reason": "banana",
                        "request": "Nope.",
                        "delivery": "chat_only",
                    },
                },
            },
            initialized,
        )
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["ok"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "INVALID_ARGUMENT",
        )


if __name__ == "__main__":
    unittest.main()
