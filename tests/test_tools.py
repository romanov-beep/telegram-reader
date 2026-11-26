import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from telegram_reader.tools import (
    MAX_PHOTO_ORIGINAL_BYTES, MAX_PHOTO_PREVIEW_BYTES, MAX_RESULT_BYTES,
    ReadTools, _PHOTO_LOCK, _ResultBudget, _wrap,
)

CHAT = "-1001234567890123"
NOW = datetime.now(timezone.utc)


def grant(**overrides):
    value = {"not_before": (NOW - timedelta(days=1)).isoformat(), "expires_at": (NOW + timedelta(days=1)).isoformat(), "consent_note": "User consent for this chat"}
    value.update(overrides)
    return value


def message(mid=9007199254740993, **overrides):
    value = {"id": mid, "chat_id": int(CHAT), "date": int(NOW.timestamp()), "sender_id": {"@type": "messageSenderUser", "user_id": 9007199254740993}, "content": {"@type": "messageText", "text": {"text": "hello"}}}
    value.update(overrides)
    return value


class FakeClient:
    authorization_state = {"@type": "authorizationStateReady", "token": "PRIVATE"}

    def __init__(self):
        self.calls = []
        self.responses = {}

    def wait_ready(self, timeout=10):
        return True

    def list_folders(self, _timeout=10):
        return [{"id": 7, "name": {"text": {"text": "Work"}}}]

    def request(self, method, **params):
        self.last_timeout = params.pop("_timeout", None)
        self.calls.append((method, params))
        response = self.responses.get(method)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        if method == "getChat":
            return {"id": params["chat_id"], "title": "Team", "type": {"@type": "chatTypeSupergroup"}}
        return {"messages": []}


class ReadToolsTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.config = {"allowed_chats": {CHAT: grant()}}
        self.tools = ReadTools(self.client, self.config)

    def call(self, name, **args):
        response = self.tools.call("telegram_" + name, args)
        self.assertEqual(json.loads(response["content"][0]["text"]), response["structuredContent"])
        return response

    def test_descriptors_expose_only_read_tools(self):
        descriptors = self.tools.descriptors()
        self.assertEqual({d["name"] for d in descriptors}, {"telegram_status", "telegram_list_chats", "telegram_read_history", "telegram_search_messages", "telegram_get_message", "telegram_get_photo", "telegram_list_folders", "telegram_search_chats"})
        for descriptor in descriptors:
            self.assertFalse(descriptor["inputSchema"]["additionalProperties"])
            self.assertTrue(descriptor["annotations"]["readOnlyHint"])
            self.assertFalse(descriptor["annotations"]["destructiveHint"])
            self.assertFalse(descriptor["annotations"]["openWorldHint"])
            self.assertEqual(descriptor["securitySchemes"], descriptor["_meta"]["securitySchemes"])

    def test_catalog_copies_are_isolated_across_transports_and_from_validation(self):
        http = {item["name"]: item for item in self.tools.descriptors()}
        stdio = {item["name"]: item for item in self.tools.descriptors(stdio=True)}
        http_photo = http["telegram_get_photo"]
        stdio_photo = stdio["telegram_get_photo"]
        http_photo["inputSchema"]["required"].clear()
        http_photo["securitySchemes"][0]["scopes"].clear()
        stdio_photo["_meta"]["securitySchemes"].clear()

        self.assertEqual(stdio_photo["inputSchema"]["required"], ["chat_id", "message_id"])
        self.assertEqual(stdio_photo["securitySchemes"], [{"type": "noauth"}])
        http_photo = next(item for item in self.tools.descriptors() if item["name"] == "telegram_get_photo")
        self.assertEqual(http_photo["securitySchemes"], [{"type": "oauth2", "scopes": ["telegram.read"]}])
        self.assertIn("on the first call", http_photo["description"])
        self.assertEqual(self.call("get_photo")["structuredContent"], {"error": "invalid_arguments"})
        self.assertEqual(self.call("send_message")["structuredContent"], {"error": "unknown_tool"})
        self.assertEqual(self.client.calls, [])

    def test_default_deny_before_any_td_request(self):
        denied = ReadTools(self.client, {}).call("telegram_read_history", {"chat_id": CHAT})
        self.assertTrue(denied["isError"])
        self.assertEqual(self.client.calls, [])

    def test_invalid_inputs_never_reach_tdlib(self):
        cases = [("read_history", {"chat_id": CHAT, "limit": True}), ("read_history", {"chat_id": CHAT, "limit": 51}), ("read_history", {"chat_id": int(CHAT)}), ("read_history", {"chat_id": CHAT, "before_message_id": "0"}), ("get_message", {"chat_id": CHAT, "message_id": "9223372036854775808"}), ("search_messages", {"chat_id": CHAT, "query": " "}), ("search_messages", {"chat_id": CHAT, "query": "a" * 257}), ("read_history", {"chat_id": CHAT, "download": True}), ("search_messages", {"query": "hello"})]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                self.assertTrue(self.call(name, **args)["isError"])
        self.assertEqual(self.client.calls, [])

    def test_expired_future_and_invalid_grants_deny(self):
        for value in [grant(expires_at=(NOW - timedelta(seconds=1)).isoformat()), grant(not_before=(NOW + timedelta(hours=1)).isoformat()), grant(consent_note=" "), grant(not_before="2020-01-01T00:00:00"), grant(expires_at="invalid")]:
            self.config["allowed_chats"][CHAT] = value
            self.assertTrue(self.call("read_history", chat_id=CHAT)["isError"])
        self.assertEqual(self.client.calls, [])

    def test_list_does_not_discover_other_chats(self):
        self.config["allowed_chats"]["42"] = grant(expires_at=(NOW - timedelta(days=1)).isoformat())
        result = self.call("list_chats")["structuredContent"]
        self.assertEqual([c["chat_id"] for c in result["chats"]], [CHAT])
        self.assertEqual(self.client.calls, [("getChat", {"chat_id": int(CHAT)})])

    def test_secret_chats_block_all_message_reads(self):
        self.client.responses["getChat"] = {"id": int(CHAT), "type": {"@type": "chatTypeSecret"}}
        self.assertTrue(self.call("read_history", chat_id=CHAT)["isError"])
        self.assertEqual(self.call("list_chats")["structuredContent"]["chats"], [])
        self.assertTrue(all(method == "getChat" for method, _ in self.client.calls))

    def test_history_filters_dates_ttl_and_cross_chat(self):
        start = int(NOW.timestamp()) - 10
        end = int(NOW.timestamp()) + 10
        self.config["allowed_chats"][CHAT] = grant(not_before=datetime.fromtimestamp(start, timezone.utc).isoformat(), expires_at=datetime.fromtimestamp(end, timezone.utc).isoformat())
        rows = [message(10, date=start - 1), message(9, date=start), message(8, date=end), message(7, self_destruct_type={"@type": "messageSelfDestructTypeTimer", "self_destruct_time": 10}), message(6, ttl=2), message(5, chat_id=42), message(4, auto_delete_in=10)]
        self.client.responses["getChatHistory"] = {"messages": rows}
        with patch("telegram_reader.tools.datetime", wraps=datetime) as tool_datetime:
            tool_datetime.now.return_value = NOW
            result = self.call("read_history", chat_id=CHAT)["structuredContent"]
        self.assertEqual([m["message_id"] for m in result["messages"]], ["9"])
        self.assertEqual(result["next_before_message_id"], "4")
        self.assertTrue(result["partial"])

    def test_history_excludes_anchor_and_keeps_original_filtered_cursor(self):
        self.client.responses["getChatHistory"] = {"messages": [message(100), message(99), message(98, ttl=10)]}
        result = self.call("read_history", chat_id=CHAT, before_message_id="100")["structuredContent"]
        self.assertEqual([m["message_id"] for m in result["messages"]], ["99"])
        self.assertEqual(result["next_before_message_id"], "98")
        self.assertEqual(self.client.calls[-1][1]["from_message_id"], 100)
        self.assertEqual(self.client.calls[-1][1]["offset"], 0)

    def test_search_is_single_chat_and_filters(self):
        self.client.responses["searchChatMessages"] = {"messages": [message(100, ttl=2), message(99)]}
        result = self.call("search_messages", chat_id=CHAT, query="needle")["structuredContent"]
        self.assertEqual(result["messages"][0]["message_id"], "99")
        method, params = self.client.calls[-1]
        self.assertEqual(method, "searchChatMessages")
        self.assertEqual(params["chat_id"], int(CHAT))
        self.assertEqual(params["query"], "needle")

    def test_get_message_preserves_ids_and_sanitizes_media(self):
        self.client.responses["getMessage"] = message(content={"@type": "messagePhoto", "caption": {"text": "caption"}, "photo": {"secret": "PRIVATE", "local": {"path": "/PRIVATE"}}})
        result = self.call("get_message", chat_id=CHAT, message_id="9007199254740993")["structuredContent"]
        self.assertEqual(result["message"]["message_id"], "9007199254740993")
        self.assertEqual(result["message"]["sender"]["id"], "9007199254740993")
        self.assertEqual(result["message"]["text"], "caption")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertTrue(result["message"]["date"].endswith("Z"))

    def test_get_message_requires_returned_id_and_window(self):
        for row in [message(2), message(1, date=1), message(1, ttl=5)]:
            self.client.responses["getMessage"] = row
            self.assertTrue(self.call("get_message", chat_id=CHAT, message_id="1")["isError"])

    def test_status_and_errors_do_not_leak_secrets(self):
        self.assertEqual(self.call("status")["structuredContent"]["authorization_state"], "authorizationStateReady")
        self.assertEqual(self.client.calls, [])
        self.client.responses["getChat"] = RuntimeError("PRIVATE auth token and path")
        result = self.call("read_history", chat_id=CHAT)
        self.assertTrue(result["isError"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.client.responses["getChat"] = {"@type": "error", "message": "PRIVATE"}
        self.assertNotIn("PRIVATE", json.dumps(self.call("read_history", chat_id=CHAT)))

    def test_output_budget_cursor_does_not_skip_unseen_messages(self):
        text = "😀" * 6000
        self.client.responses["getChatHistory"] = {"messages": [message(mid, content={"@type": "messageText", "text": {"text": text}}) for mid in range(100, 50, -1)]}
        response = self.call("read_history", chat_id=CHAT, limit=50)
        result = response["structuredContent"]
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=True).encode()), MAX_RESULT_BYTES)
        self.assertGreater(len(result["messages"]), 0)
        self.assertLess(len(result["messages"]), 50)
        self.assertEqual(result["next_before_message_id"], result["messages"][-1]["message_id"])
        self.assertTrue(all(m["text_truncated"] for m in result["messages"]))

    def test_all_mode_dynamic_folder_and_arbitrary_chat_read(self):
        self.config.update(chat_access="all", all_chats_grant=grant())
        self.client.responses["getChats"] = {"chat_ids": [42]}
        result = self.call("list_chats", folder_id="7")["structuredContent"]
        self.assertEqual(result["chats"][0]["chat_id"], "42")
        self.assertEqual(self.client.calls[0], ("getChats", {"chat_list": {"@type": "chatListFolder", "chat_folder_id": 7}, "limit": 20}))
        self.client.responses["getMessage"] = message(100, chat_id=42)
        self.assertEqual(self.call("get_message", chat_id="42", message_id="100")["structuredContent"]["message"]["chat_id"], "42")

    def test_expired_all_consent_blocks_discovery_before_td(self):
        self.config.update(chat_access="all", all_chats_grant=grant(expires_at=(NOW - timedelta(seconds=1)).isoformat()))
        self.assertTrue(self.call("list_chats")["isError"])
        self.assertTrue(self.call("search_chats", query="Work")["isError"])
        self.assertEqual(self.client.calls, [])

    def test_selected_folder_and_chat_search_intersect_grants(self):
        self.client.responses["getChats"] = {"chat_ids": [42, int(CHAT)]}
        result = self.call("list_chats", folder_id="7")["structuredContent"]
        self.assertEqual([c["chat_id"] for c in result["chats"]], [CHAT])
        self.assertNotIn(("getChat", {"chat_id": 42}), self.client.calls)
        self.client.responses["searchChats"] = {"chat_ids": [42, int(CHAT)]}
        result = self.call("search_chats", query="Work")["structuredContent"]
        self.assertEqual([c["chat_id"] for c in result["chats"]], [CHAT])
        self.assertNotIn(("getChat", {"chat_id": 42}), self.client.calls)

    def test_folder_metadata_is_from_adapter(self):
        result = self.call("list_folders")["structuredContent"]
        self.assertEqual(result["folders"], [{"folder_id": "7", "title": "Work"}])
        self.assertEqual(self.client.calls, [])

    def test_anchor_limit_one_requests_extra_and_makes_progress(self):
        for name, method, extra in [("read_history", "getChatHistory", {}), ("search_messages", "searchChatMessages", {"query": "hello"})]:
            with self.subTest(name=name):
                self.client.responses[method] = {"messages": [message(100), message(99)]}
                result = self.call(name, chat_id=CHAT, before_message_id="100", limit=1, **extra)["structuredContent"]
                self.assertEqual([m["message_id"] for m in result["messages"]], ["99"])
                self.assertEqual(result["next_before_message_id"], "99")
                self.assertEqual(self.client.calls[-1][1]["limit"], 2)

    def test_protected_chat_and_unsaveable_message_are_excluded(self):
        self.client.responses["getChat"] = {"id": int(CHAT), "type": {"@type": "chatTypeSupergroup"}, "has_protected_content": True}
        self.assertTrue(self.call("read_history", chat_id=CHAT)["isError"])
        self.assertTrue(all(method == "getChat" for method, _ in self.client.calls))
        self.client.responses.pop("getChat")
        self.client.responses["getChatHistory"] = {"messages": [message(100, can_be_saved=False), message(99)]}
        result = self.call("read_history", chat_id=CHAT)["structuredContent"]
        self.assertEqual([m["message_id"] for m in result["messages"]], ["99"])

    def test_tool_deadline_is_passed_and_expiry_blocks_next_request(self):
        with patch("telegram_reader.tools.time.monotonic", side_effect=[100, 101, 126]):
            response = self.call("read_history", chat_id=CHAT)
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["error"], "tool_timeout")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.last_timeout, 24)

    def test_selected_chat_attempts_are_bounded(self):
        self.config["allowed_chats"] = {str(i): grant() for i in range(1, 101)}
        self.client.responses["getChat"] = {"id": 1, "type": {"@type": "chatTypeSecret"}}
        result = self.call("list_chats")["structuredContent"]
        self.assertEqual(len(self.client.calls), 50)
        self.assertEqual(result["next_after_chat_id"], "50")

    def test_get_message_budget_truncates_explicitly(self):
        self.client.responses["getMessage"] = message(content={"@type": "messageText", "text": {"text": "😀" * 100000}})
        response = self.call("get_message", chat_id=CHAT, message_id="9007199254740993")
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=True).encode()), MAX_RESULT_BYTES)
        self.assertTrue(response["structuredContent"]["message"]["text_truncated"])


    def test_incremental_budget_matches_complete_wire_encoding(self):
        result = {"messages": [], "cursor": None, "note": "Unicode: текст 😀"}
        budget = _ResultBudget(result, "messages")
        items = [{"text": value, "nested": [None, True, 42, {"quote": '\"\\\n'}]}
                 for value in ["", "ASCII", "Привет", "😀", "\x00\t\r", '"\\', "e\u0301", "\ud800"]]
        for index, item in enumerate(items):
            self.assertTrue(budget.append(item, cursor=str(1000 - index)))
            self.assertEqual(budget.size, len(json.dumps(_wrap(result), ensure_ascii=True).encode("utf-8")))
        for cursor in [None, "9223372036854775807", "-1001234567890123", "1"]:
            self.assertTrue(budget.update(cursor=cursor))
            self.assertEqual(budget.size, len(json.dumps(_wrap(result), ensure_ascii=True).encode("utf-8")))

    def test_incremental_budget_boundary_and_failed_append_are_exact(self):
        item = {"text": 'текст 😀 "quoted" \\ \n'}
        expected = {"messages": [item], "cursor": "42"}
        exact = len(json.dumps(_wrap(expected), ensure_ascii=True))
        for cap, accepted in [(exact - 1, False), (exact, True)]:
            with self.subTest(cap=cap), patch("telegram_reader.tools.MAX_RESULT_BYTES", cap):
                result = {"messages": [], "cursor": None}
                budget = _ResultBudget(result, "messages")
                self.assertEqual(budget.append(item, cursor="42"), accepted)
                self.assertEqual(result, expected if accepted else {"messages": [], "cursor": None})
                self.assertEqual(budget.size, len(json.dumps(_wrap(result), ensure_ascii=True)))

    def test_cross_chat_rows_do_not_move_history_or_search_cursor(self):
        for name, method, extra in [("read_history", "getChatHistory", {}), ("search_messages", "searchChatMessages", {"query": "hello"})]:
            with self.subTest(name=name):
                self.client.responses[method] = {"messages": [message(100), message(1, chat_id=42)]}
                result = self.call(name, chat_id=CHAT, **extra)["structuredContent"]
                self.assertEqual([row["message_id"] for row in result["messages"]], ["100"])
                self.assertEqual(result["next_before_message_id"], "100")

    def test_duplicate_rows_do_not_duplicate_output_and_unordered_rows_fail(self):
        self.client.responses["getChatHistory"] = {"messages": [message(100), message(100), message(99)]}
        result = self.call("read_history", chat_id=CHAT)["structuredContent"]
        self.assertEqual([row["message_id"] for row in result["messages"]], ["100", "99"])
        self.assertEqual(result["next_before_message_id"], "99")
        self.client.responses["getChatHistory"] = {"messages": [message(99), message(100)]}
        response = self.call("read_history", chat_id=CHAT)
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"], {"error": "telegram_request_failed"})

    def test_byte_limit_keeps_filtered_cursor_before_unreturned_row(self):
        self.client.responses["getChatHistory"] = {"messages": [message(100), message(99, ttl=2), message(98, content={"@type": "messageText", "text": {"text": "😀" * 4000}})]}
        result = self.call("read_history", chat_id=CHAT)["structuredContent"]
        self.assertEqual([row["message_id"] for row in result["messages"]], ["100"])
        self.assertEqual(result["next_before_message_id"], "99")

    def test_selected_chat_budget_continuation_does_not_skip_unreturned_chat(self):
        self.config["allowed_chats"] = {str(i): grant() for i in range(1, 4)}
        first_page = {"chats": [{"chat_id": "1", "title": "Team", "type": "chatTypeSupergroup"}], "next_after_chat_id": "1", "partial": True}
        exact = len(json.dumps(_wrap(first_page), ensure_ascii=True))
        with patch("telegram_reader.tools.MAX_RESULT_BYTES", exact):
            first = self.call("list_chats")["structuredContent"]
            second = self.call("list_chats", after_chat_id=first["next_after_chat_id"])["structuredContent"]
        self.assertEqual(first, first_page)
        self.assertEqual([row["chat_id"] for row in second["chats"]], ["2"])
        self.assertEqual(second["next_after_chat_id"], "2")

    def test_rate_limits_are_safe_errors_and_stop_metadata_requests(self):
        class RateLimited(Exception):
            code = 429

        self.config["allowed_chats"]["42"] = grant()
        for error in [RateLimited("PRIVATE"), {"@type": "error", "code": 429, "message": "PRIVATE"}]:
            for dynamic in [False, True]:
                with self.subTest(error=type(error), dynamic=dynamic):
                    self.client.calls.clear()
                    self.client.responses["getChat"] = error
                    self.client.responses["searchChats"] = {"chat_ids": [int(CHAT), 42]}
                    response = self.call("search_chats", query="hello") if dynamic else self.call("list_chats")
                    self.assertTrue(response["isError"])
                    self.assertEqual(response["structuredContent"], {"error": "telegram_rate_limited"})
                    self.assertEqual(sum(method == "getChat" for method, _ in self.client.calls), 1)
                    self.assertNotIn("PRIVATE", json.dumps(response))

    def test_expiry_during_metadata_request_blocks_message_read(self):
        original = self.client.request
        def expire_after_request(method, **params):
            response = original(method, **params)
            self.config["allowed_chats"][CHAT]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
            return response
        with patch.object(self.client, "request", side_effect=expire_after_request):
            response = self.call("read_history", chat_id=CHAT)
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"], {"error": "chat_not_authorized"})
        self.assertEqual([method for method, _ in self.client.calls], ["getChat"])


    def test_rate_limit_retry_delay_is_bounded_and_never_leaks_text(self):
        class RateLimited(Exception):
            code = 429

        for delay, expected in [(5, 5), (1209600, 1209600), (True, None), ("PRIVATE", None), (0, None), (1209601, None)]:
            with self.subTest(delay=delay):
                error = RateLimited("PRIVATE")
                error.retry_after = delay
                self.client.responses["getChat"] = error
                result = self.call("read_history", chat_id=CHAT)["structuredContent"]
                self.assertEqual(result["error"], "telegram_rate_limited")
                self.assertEqual(result.get("retry_after_seconds"), expected)
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_native_timeout_is_not_an_empty_chat_list(self):
        class TimedOut(Exception):
            code = 504

        for error in [TimedOut("PRIVATE"), {"@type": "error", "code": 504, "message": "PRIVATE"}]:
            with self.subTest(error=type(error)):
                self.client.responses["getChat"] = error
                response = self.call("list_chats")
                self.assertTrue(response["isError"])
                self.assertEqual(response["structuredContent"], {"error": "telegram_timeout"})


    def test_text_previews_preserve_unicode_cursor_and_full_message_access(self):
        body = "😀Привет" * 100
        self.client.responses["getMessage"] = message(99, content={"@type": "messageText", "text": {"text": body}})
        for name, method, extra in [("read_history", "getChatHistory", {}), ("search_messages", "searchChatMessages", {"query": "hello"})]:
            self.client.responses[method] = {"messages": [message(mid, content={"@type": "messageText", "text": {"text": body}}) for mid in [100, 99, 98]]}
            for cap in [0, 2, 4000]:
                with self.subTest(name=name, cap=cap):
                    result = self.call(name, chat_id=CHAT, before_message_id="100", limit=1, max_text_chars=cap, **extra)["structuredContent"]
                    self.assertEqual([row["message_id"] for row in result["messages"]], ["99"])
                    self.assertEqual(result["messages"][0]["text"], body[:cap])
                    self.assertEqual(result["messages"][0]["text_truncated"], len(body) > cap)
                    self.assertEqual(result["next_before_message_id"], "99")
            default = self.call(name, chat_id=CHAT, **extra)
            explicit_default = self.call(name, chat_id=CHAT, max_text_chars=4000, **extra)
            self.assertEqual(default, explicit_default)
        full = self.call("get_message", chat_id=CHAT, message_id="99")["structuredContent"]["message"]
        self.assertEqual(full["text"], body)
        self.assertFalse(full["text_truncated"])

    def test_text_preview_validation_rejects_invalid_values_before_tdlib(self):
        for value in [True, False, -1, 4001, "240", None, 1.5]:
            for name, extra in [("read_history", {}), ("search_messages", {"query": "hello"})]:
                with self.subTest(value=value, name=name):
                    response = self.call(name, chat_id=CHAT, max_text_chars=value, **extra)
                    self.assertTrue(response["isError"])
                    self.assertEqual(response["structuredContent"], {"error": "invalid_arguments"})
        self.assertTrue(self.call("get_message", chat_id=CHAT, message_id="99", max_text_chars=240)["isError"])
        self.assertEqual(self.client.calls, [])

    def test_short_previews_reduce_payload_and_keep_more_rows(self):
        self.client.responses["getChatHistory"] = {"messages": [message(mid, content={"@type": "messageText", "text": {"text": "😀" * 4000}}) for mid in range(100, 80, -1)]}
        default = self.call("read_history", chat_id=CHAT, limit=20)
        preview = self.call("read_history", chat_id=CHAT, limit=20, max_text_chars=32)
        rows = preview["structuredContent"]["messages"]
        self.assertEqual(len(rows), 20)
        self.assertLess(len(default["structuredContent"]["messages"]), 20)
        self.assertTrue(all(row["text_truncated"] for row in rows))
        self.assertEqual(preview["structuredContent"]["next_before_message_id"], "81")
        self.assertLess(len(json.dumps(preview, ensure_ascii=True)), len(json.dumps(default, ensure_ascii=True)))


    def test_session_and_service_failures_are_not_successful_empty_lists(self):
        self.config["allowed_chats"]["42"] = grant()
        self.config["all_chats_grant"] = grant()
        self.client.responses["getChats"] = {"chat_ids": [int(CHAT), 42]}
        for code, expected in [(503, "telegram_unavailable"), (401, "telegram_login_required")]:
            exception = RuntimeError("PRIVATE session details")
            exception.code = code
            for error in [exception, {"@type": "error", "code": code, "message": "PRIVATE session details"}]:
                for mode in ["selected", "all"]:
                    with self.subTest(code=code, error=type(error), mode=mode):
                        self.config["chat_access"] = mode
                        self.client.calls.clear()
                        self.client.responses["getChat"] = error
                        response = self.call("list_chats")
                        self.assertTrue(response["isError"])
                        self.assertEqual(response["structuredContent"], {"error": expected})
                        self.assertEqual(sum(method == "getChat" for method, _ in self.client.calls), 1)
                        self.assertNotIn("PRIVATE", json.dumps(response))


class PhotoToolsTest(unittest.TestCase):
    JPEG = (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
        "AQEBAQEBAQEBAQEBAQH/wAALCAABAAEBAREA/8QAJgABAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAA"
        "PwA//9k="
    )
    FILE_BYTES = 155

    @staticmethod
    def rendition(file_id, width, height, size=0, **local):
        return {"width": width, "height": height, "photo": {
            "@type": "file", "id": file_id, "size": size,
            "local": {"can_be_downloaded": True, "path": "", **local},
        }}

    def setUp(self):
        self.client = FakeClient()
        self.config = {"allowed_chats": {CHAT: grant()}}
        self.tools = ReadTools(self.client, self.config)
        self.medium = self.rendition(47, 640, 480)
        self.large = self.rendition(92, 1280, 960)
        self.msg = message(123, can_be_saved=True, content={
            "@type": "messagePhoto", "is_secret": False,
            "photo": {"sizes": [self.large, self.rendition(33, 90, 67), self.medium]},
        })
        self.client.responses.update(
            getMessage=self.msg, readFilePart={"@type": "data", "data": self.JPEG},
            cancelDownloadFile={"@type": "ok"}, deleteFile={"@type": "ok"},
        )

    def downloaded(self, file_id, size=FILE_BYTES):
        return self.rendition(file_id, 1, 1, size, path="/private/tdlib-cache",
                              is_downloading_completed=True)["photo"]

    def call(self, **args):
        return self.tools.call("telegram_get_photo", {"chat_id": CHAT, "message_id": "123", **args})

    def test_retrieval_variants_keep_jpeg_bytes_and_mcp_type(self):
        from mcp.types import CallToolResult

        for variant, file_id, width, maximum in (
            ("preview", 47, 640, MAX_PHOTO_PREVIEW_BYTES),
            ("original", 92, 1280, MAX_PHOTO_ORIGINAL_BYTES),
        ):
            with self.subTest(variant=variant):
                self.client.responses["downloadFile"] = self.downloaded(file_id)
                self.client.calls.clear()
                raw = self.call(**({"variant": "original"} if variant == "original" else {}))
                result = CallToolResult.model_validate(raw)
                self.assertFalse(result.is_error)
                metadata = result.structured_content
                self.assertEqual(metadata["width"], width)
                self.assertEqual(metadata["size_bytes"], self.FILE_BYTES)
                self.assertEqual(metadata["filename"], "telegram-photo-123.jpg")
                block = result.content[1]
                self.assertEqual(block.type, "image" if variant == "preview" else "resource")
                self.assertEqual(block.data if variant == "preview" else block.resource.blob, self.JPEG)
                self.assertNotIn("/private/tdlib-cache", json.dumps(raw))
                self.assertNotIn("file_id", json.dumps(raw))
                self.assertEqual(self.client.calls[2:5], [
                    ("downloadFile", {"file_id": file_id, "priority": 4, "offset": 0, "limit": maximum + 1, "synchronous": True}),
                    ("readFilePart", {"file_id": file_id, "offset": 0, "count": self.FILE_BYTES}),
                    ("deleteFile", {"file_id": file_id}),
                ])

    def test_no_native_media_call_for_excluded_content(self):
        exclusion = (
            {"can_be_saved": False}, {"can_be_saved": None}, {"id": 456},
            {"chat_id": 444}, {"date": 1},
            {"self_destruct_type": {"@type": "messageSelfDestructTypeImmediately"}, "self_destruct_in": 0},
            {"auto_delete_in": 100.0}, {"ephemeral_content": {"@type": "ephemeralMessageContent"}},
            {"content": {**self.msg["content"], "is_secret": True}},
            {"content": {"@type": "messageDocument"}},
        )
        for override in exclusion:
            with self.subTest(override=override):
                self.client.responses["getMessage"] = {**self.msg, **override}
                self.client.calls.clear()
                self.assertTrue(self.call()["isError"])
                self.assertEqual([method for method, _ in self.client.calls], ["getChat", "getMessage"])

        for fields in ({"has_protected_content": True}, {"type": {"@type": "chatTypeSecret"}}):
            self.client.responses["getChat"] = {"id": int(CHAT), "type": {"@type": "chatTypeSupergroup"}, **fields}
            self.client.calls.clear()
            self.assertEqual(self.call()["structuredContent"]["error"], "chat_not_authorized")
            self.assertEqual([method for method, _ in self.client.calls], ["getChat"])

    def test_revoked_grant_and_untrusted_file_id_or_variant(self):
        self.assertEqual(self.call(file_id=47)["structuredContent"]["error"], "invalid_arguments")
        self.assertEqual(self.call(variant=["preview"])["structuredContent"]["error"], "invalid_arguments")
        self.config["allowed_chats"].clear()
        self.assertEqual(self.call()["structuredContent"]["error"], "chat_not_authorized")
        self.assertEqual(self.client.calls, [])

    def test_original_does_not_fall_back_when_largest_is_oversized(self):
        self.large["photo"]["size"] = MAX_PHOTO_ORIGINAL_BYTES + 1
        self.assertEqual(self.call(variant="original")["structuredContent"]["error"], "photo_too_large")
        self.assertEqual([method for method, _ in self.client.calls], ["getChat", "getMessage"])

    def test_unknown_size_stops_at_limit_and_removes_incomplete_cache(self):
        self.client.responses["downloadFile"] = self.rendition(
            47, 640, 480, downloaded_prefix_size=MAX_PHOTO_PREVIEW_BYTES + 1
        )["photo"]
        self.assertEqual(self.call()["structuredContent"]["error"], "photo_too_large")
        self.assertEqual([method for method, _ in self.client.calls][-3:],
                         ["downloadFile", "cancelDownloadFile", "deleteFile"])

    def test_unknown_size_original_does_not_fall_back_after_download(self):
        self.client.responses["downloadFile"] = self.rendition(
            92, 1280, 960, downloaded_prefix_size=MAX_PHOTO_ORIGINAL_BYTES + 1
        )["photo"]
        result = self.call(variant="original")
        self.assertEqual(result["structuredContent"]["error"], "photo_too_large")
        self.assertEqual([block["type"] for block in result["content"]], ["text"])
        downloads = [params for method, params in self.client.calls if method == "downloadFile"]
        self.assertEqual([params["file_id"] for params in downloads], [92])
        self.assertEqual([method for method, _ in self.client.calls][-2:],
                         ["cancelDownloadFile", "deleteFile"])

    def test_timeout_cancels_and_cleans_up_before_next_photo(self):
        self.client.responses["downloadFile"] = {"@type": "error", "code": 504}
        self.assertEqual(self.call()["structuredContent"]["error"], "telegram_timeout")
        self.assertEqual([method for method, _ in self.client.calls][-3:],
                         ["downloadFile", "cancelDownloadFile", "deleteFile"])
        self.client.responses["downloadFile"] = self.downloaded(47)
        self.assertFalse(self.call().get("isError", False))

    def test_cached_photo_not_deleted_and_corrupt_bytes_not_returned(self):
        self.medium["photo"] = self.downloaded(47)
        self.assertFalse(self.call().get("isError", False))
        self.assertEqual([method for method, _ in self.client.calls], ["getChat", "getMessage", "readFilePart"])
        self.client.responses["readFilePart"]["data"] = "!" * len(self.JPEG)
        error = self.call()
        self.assertEqual(error["structuredContent"]["error"], "photo_unavailable")
        self.assertEqual([block["type"] for block in error["content"]], ["text"])

    def test_preexisting_partial_cache_is_retained(self):
        self.medium["photo"]["local"].update(downloaded_size=10, downloaded_prefix_size=10)
        self.client.responses["downloadFile"] = self.downloaded(47)
        self.assertFalse(self.call().get("isError", False))
        self.assertNotIn("deleteFile", [method for method, _ in self.client.calls])

    def test_refuses_to_return_a_photo_protected_during_download(self):
        self.client.responses["downloadFile"] = self.downloaded(47)
        request = self.client.request

        def protect_message_after_read(method, **params):
            result = request(method, **params)
            if method == "readFilePart":
                self.client.responses["getMessage"] = {**self.msg, "can_be_saved": False}
            return result

        with patch.object(self.client, "request", side_effect=protect_message_after_read):
            result = self.call()
        self.assertEqual(result["structuredContent"]["error"], "message_not_available")
        self.assertEqual([block["type"] for block in result["content"]], ["text"])

    def test_overlapping_call_cannot_touch_cache(self):
        with _PHOTO_LOCK:
            self.assertEqual(self.call()["structuredContent"]["error"], "photo_busy")
        self.assertEqual([method for method, _ in self.client.calls], ["getChat"])


if __name__ == "__main__":
    unittest.main()
