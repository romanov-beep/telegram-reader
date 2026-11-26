"""Deterministic algorithmic guard; benchmark CLI lives in scripts/."""
import json
import unittest
from unittest.mock import patch

from telegram_reader import tools
from test_tools import CHAT, FakeClient, grant, message


def history_call(module, count=50, text="synthetic text"):
    client = FakeClient()
    client.responses["getChatHistory"] = {"messages": [
        message(mid, content={"@type": "messageText", "text": {"text": text}})
        for mid in range(1000, 1000 - count, -1)
    ]}
    reader = module.ReadTools(client, {"allowed_chats": {CHAT: grant()}})
    def call():
        client.calls.clear()
        return reader.call("telegram_read_history", {"chat_id": CHAT, "limit": count})
    return call


def serialization_work(call):
    original = json.dumps
    characters = 0
    def count(value, *args, **kwargs):
        nonlocal characters
        encoded = original(value, *args, **kwargs)
        characters += len(encoded)
        return encoded
    with patch.object(json, "dumps", side_effect=count):
        result = call()
    return characters, result


class ReadToolPerformanceTest(unittest.TestCase):
    def test_serialization_work_grows_linearly_with_page_length(self):
        small_work, small = serialization_work(history_call(tools, 25))
        large_work, large = serialization_work(history_call(tools, 50))
        self.assertEqual(len(small["structuredContent"]["messages"]), 25)
        self.assertEqual(len(large["structuredContent"]["messages"]), 50)
        # Deterministic algorithmic guard; no wall-clock threshold in the suite.
        self.assertLess(large_work, small_work * 2.2)



if __name__ == "__main__":
    unittest.main()
