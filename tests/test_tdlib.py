"""Offline tests: official C ABI is replaced, no Telegram credentials/account used."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import weakref

from telegram_reader.tdlib import TdClient, TdError


class CFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class FakeLibrary:
    def __init__(self, auth="authorizationStateReady"):
        self.events = queue.Queue()
        self.sent = []
        self.auth = auth
        self.respond = True
        self.reverse = False
        self.held = []
        self.receive_timeouts = []
        self.receive_threads = set()
        self.td_create_client_id = CFunction(lambda: 77)
        self.td_execute = CFunction(lambda payload: b'{"@type":"ok"}')
        self.td_send = CFunction(self.send)
        self.td_receive = CFunction(self.receive)

    def emit(self, value):
        self.events.put(json.dumps({"@client_id": 77, **value}).encode())

    def send(self, client_id, payload):
        request = json.loads(payload)
        self.sent.append(request)
        method = request["@type"]
        if method == "close":
            self.emit({"@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationStateClosed"}})
            return
        if method == "getAuthorizationState":
            self.emit({"@type": self.auth, "@extra": request["@extra"]})
            return
        if not self.respond:
            return
        if self.reverse:
            self.held.append(request)
            if len(self.held) == 2:
                for item in reversed(self.held):
                    self.emit({"@type": "chat", "id": item["chat_id"], "@extra": item["@extra"]})
            return
        self.emit({"@type": "ok", "@extra": request["@extra"]})

    def receive(self, timeout):
        self.receive_timeouts.append(timeout)
        self.receive_threads.add(threading.get_ident())
        try:
            result = self.events.get(timeout=timeout)
            if isinstance(result, Exception):
                raise result
            return result
        except queue.Empty:
            return None


class TdClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dict(tdjson_path="fake.so", api_id=123, api_hash="secret-hash",
                           database_directory=str(Path(self.temp.name) / "db"),
                           files_directory=str(Path(self.temp.name) / "files"),
                           database_encryption_key="секрет", request_timeout=0.4)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            try:
                client.close()
            except TdError:
                # Fake native library owns no actual databases after a simulated fault.
                client._closed = True
                client._receiver.join(timeout=1)
                client._release()
        self.temp.cleanup()

    def client(self, fake=None):
        fake = fake or FakeLibrary()
        with patch("telegram_reader.tdlib.ctypes.CDLL", return_value=fake):
            client = TdClient(self.config)
        self.clients.append(client)
        return client, fake

    def wait_for(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertTrue(predicate())

    def test_out_of_order_responses_are_correlated(self):
        client, fake = self.client()
        client.wait_ready()
        fake.reverse = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.request, "getChat", chat_id=1)
            second = pool.submit(client.request, "getChat", chat_id=2)
            self.assertEqual(first.result()["id"], 1)
            self.assertEqual(second.result()["id"], 2)

    def test_timeout_removes_pending_request(self):
        client, fake = self.client()
        client.wait_ready()
        fake.respond = False
        with self.assertRaises(TdError) as raised:
            client.request("getChat", chat_id=1)
        self.assertEqual(raised.exception.code, 504)
        self.assertFalse(client._pending)
        self.assertEqual(list(client._abandoned.values()), ["getChat"])

    def test_timeouts_do_not_release_native_capacity_until_late_response(self):
        client, fake = self.client()
        client.wait_ready()
        fake.respond = False
        for chat_id in range(client.MAX_PENDING * 2):
            if len(client._abandoned) == client.MAX_PENDING:
                break
            with self.assertRaises(TdError) as raised:
                client.request("getChat", _timeout=0.05, chat_id=chat_id + 1)
            self.assertEqual(raised.exception.code, 504)
        self.assertFalse(client._pending)
        self.assertEqual(len(client._abandoned), client.MAX_PENDING)
        sent = len(fake.sent)
        with self.assertRaises(TdError) as raised:
            client.request("getChat", chat_id=99)
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(len(fake.sent), sent)
        extra = fake.sent[-1]["@extra"]
        fake.emit({"@type": "chat", "id": 8, "@extra": extra})
        self.wait_for(lambda: extra not in client._abandoned)
        fake.respond = True
        self.assertEqual(client.request("getChat", chat_id=99)["@type"], "ok")
        self.assertEqual(set(client._abandoned.values()), {"getChat"})

    def test_native_rate_limit_is_immediate_does_not_retry_or_extend_cooldown(self):
        client, fake = self.client()
        client.wait_ready()
        send = fake.td_send

        def limited(client_id, payload):
            request = json.loads(payload)
            if request["@type"] == "getChat":
                fake.sent.append(request)
                fake.emit({"@type": "error", "code": 429,
                           "message": "Too Many Requests: retry after 9", "@extra": request["@extra"]})
            else:
                send(client_id, payload)

        fake.td_send = CFunction(limited)
        with self.assertRaises(TdError) as raised:
            client.request("getChat", chat_id=1)
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(raised.exception.retry_after, 9)
        self.assertEqual(raised.exception.message, "TDLib request failed")
        until = client._retry_until
        sent = len(fake.sent)
        started = time.monotonic()
        for _ in range(20):
            with self.assertRaises(TdError) as raised:
                client.request("getChat", chat_id=1)
            self.assertEqual(raised.exception.code, 429)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(len(fake.sent), sent)
        self.assertEqual(client._retry_until, until)
        self.assertFalse(client._pending)
        self.assertFalse(client._abandoned)
        fake.td_send = send
        with client._condition:
            client._retry_until = time.monotonic() - 1
        self.assertEqual(client.request("getChat", chat_id=1)["@type"], "ok")

    def test_late_rate_limit_still_sets_cooldown_and_close_bypasses_it(self):
        client, fake = self.client()
        client.wait_ready()
        fake.respond = False
        with self.assertRaises(TdError):
            client.request("getChatHistory", _timeout=0.005, chat_id=1)
        extra = fake.sent[-1]["@extra"]
        fake.emit({"@type": "error", "code": 429, "message": "Too Many Requests: retry after 3", "@extra": extra})
        self.wait_for(lambda: client._retry_until > time.monotonic())
        self.assertFalse(client._abandoned)
        sent = len(fake.sent)
        with self.assertRaises(TdError) as raised:
            client.request("getChat", chat_id=1)
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(len(fake.sent), sent)
        client.close()
        self.assertTrue(client._closed)

    def test_unmatched_or_other_client_rate_limit_does_not_change_state(self):
        client, fake = self.client()
        client.wait_ready()
        client._dispatch({"@client_id": 77, "@type": "error", "code": 429,
                          "message": "FLOOD_WAIT_9", "@extra": "unknown"})
        extra, future = client._submit("getChat", {"chat_id": 1})
        client._dispatch({"@client_id": 78, "@type": "error", "code": 429,
                          "message": "FLOOD_WAIT_9", "@extra": extra})
        self.assertEqual(future.result(timeout=1)["@type"], "ok")
        self.assertEqual(client._retry_until, 0)

    def test_request_deadline_is_rechecked_after_lock_contention(self):
        client, fake = self.client()
        client.wait_ready()
        attempted = threading.Event()
        original_timeout = client._request_timeout

        def timeout(value):
            result = original_timeout(value)
            attempted.set()
            return result

        sent = len(fake.sent)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with client._condition, patch.object(client, "_request_timeout", side_effect=timeout):
                request = pool.submit(client._submit, "getChat", {"chat_id": 1}, timeout=0.01)
                self.assertTrue(attempted.wait(timeout=1))
                time.sleep(0.03)
            with self.assertRaises(TdError) as raised:
                request.result(timeout=1)
        self.assertEqual(raised.exception.code, 504)
        self.assertEqual(len(fake.sent), sent)
        self.assertFalse(client._pending)

    def test_expired_unawaited_request_retains_only_native_slot(self):
        client, fake = self.client()
        client.wait_ready()
        fake.respond = False
        extra, future = client._submit("getChat", {"chat_id": 1}, timeout=0.005)
        self.wait_for(future.done, timeout=client.RECEIVE_IDLE_TIMEOUT + 0.5)
        self.assertEqual(future.exception().code, 504)
        self.assertFalse(client._pending)
        self.assertEqual(client._abandoned, {extra: "getChat"})

    def test_receiver_releases_last_completed_future_before_idle_wait(self):
        client, fake = self.client()
        client.wait_ready()
        extra, future = client._submit("getMessage", {"chat_id": 1, "message_id": 1})
        self.assertEqual(future.result(timeout=1)["@type"], "ok")
        reference = weakref.ref(future)
        del future
        self.wait_for(lambda: reference() is None)
        self.assertNotIn(extra, client._pending)

    def test_idle_receiver_uses_one_thread_and_one_second_wait(self):
        client, fake = self.client()
        client.wait_ready()
        self.wait_for(lambda: client.RECEIVE_IDLE_TIMEOUT in fake.receive_timeouts)
        self.assertEqual(fake.receive_threads, {client._receiver.ident})
        self.assertEqual(client.RECEIVE_IDLE_TIMEOUT, 1.0)

    def test_no_runtime_auth_writes_or_reserved_parameters(self):
        client, fake = self.client()
        for method in ("deleteMessages", "viewMessages", "openChat", "sendMessage", "setOption", "checkAuthenticationBotToken", "checkAuthenticationCode", "searchMessages", "getMessageLink", "searchPublicChat", "getMe", "getUser", "getSupergroup", "getBasicGroup"):
            with self.assertRaises(TdError) as raised:
                client.request(method)
            self.assertEqual(raised.exception.code, 403)
        with self.assertRaises(TdError):
            client.request("getChat", **{"@type": "sendMessage"})
        self.assertFalse(any(r["@type"] == "viewMessages" for r in fake.sent))

    def test_auto_parameters_are_flat_encrypted_key_is_base64(self):
        client, fake = self.client(FakeLibrary("authorizationStateWaitTdlibParameters"))
        deadline = time.monotonic() + 1
        while not any(item["@type"] == "setTdlibParameters" for item in fake.sent) and time.monotonic() < deadline:
            time.sleep(0.005)
        params = next(item for item in fake.sent if item["@type"] == "setTdlibParameters")
        self.assertNotIn("parameters", params)
        self.assertEqual(base64.b64decode(params["database_encryption_key"]).decode(), "секрет")
        for flag in ("use_test_dc", "use_file_database", "use_chat_info_database", "use_message_database", "use_secret_chats"):
            self.assertIs(params[flag], False)
        self.assertEqual(Path(self.config["database_directory"]).stat().st_mode & 0o777, 0o700)

    def test_existing_session_only(self):
        client, fake = self.client(FakeLibrary("authorizationStateWaitPhoneNumber"))
        with self.assertRaises(TdError) as raised:
            client.wait_ready()
        self.assertEqual(raised.exception.code, 401)
        self.assertEqual([item["@type"] for item in fake.sent], ["getAuthorizationState"])

    def test_request_timeout_override_is_local_and_cleans_pending(self):
        client, fake = self.client()
        client.wait_ready()
        fake.respond = False
        started = time.monotonic()
        with self.assertRaises(TdError) as raised:
            client.request("getChat", _timeout=0.02, chat_id=1)
        self.assertEqual(raised.exception.code, 504)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertNotIn("_timeout", fake.sent[-1])
        self.assertFalse(client._pending)
        self.assertEqual(list(client._abandoned.values()), ["getChat"])
        self.assertEqual(client._request_timeout(100), self.config["request_timeout"])
        for invalid in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(TdError):
                client.request("getChat", _timeout=invalid, chat_id=1)

    def test_bounded_pending_and_close_at_capacity(self):
        client, fake = self.client()
        client.wait_ready()
        fake.respond = False
        for _ in range(8):
            client._submit("getChat", {"chat_id": 1})
        with self.assertRaises(TdError) as raised:
            client._submit("getChat", {"chat_id": 1})
        self.assertEqual(raised.exception.code, 429)
        client.close()
        self.assertTrue(client._closed)
        self.assertFalse(client._pending)
        self.assertFalse(client._abandoned)

    def test_database_lock_excludes_another_process(self):
        client, _ = self.client()
        lock_path = str(Path(self.config["database_directory"]) / ".telegram-reader.lock")
        script = "import fcntl,sys; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)"
        blocked = subprocess.run([sys.executable, "-c", script, lock_path], capture_output=True)
        self.assertNotEqual(blocked.returncode, 0)
        client.close()
        available = subprocess.run([sys.executable, "-c", script, lock_path], capture_output=True)
        self.assertEqual(available.returncode, 0)

    def test_local_phone_code_password_login(self):
        fake = FakeLibrary("authorizationStateWaitPhoneNumber")
        original_send = fake.send
        def send(client_id, payload):
            original_send(client_id, payload)
            method = json.loads(payload)["@type"]
            next_state = {"setAuthenticationPhoneNumber": "authorizationStateWaitCode",
                          "checkAuthenticationCode": "authorizationStateWaitPassword",
                          "checkAuthenticationPassword": "authorizationStateReady"}.get(method)
            if next_state:
                fake.emit({"@type": "updateAuthorizationState", "authorization_state": {"@type": next_state}})
        fake.td_send = CFunction(send)
        client, _ = self.client(fake)
        with patch("telegram_reader.tdlib.os.isatty", return_value=True), patch("telegram_reader.tdlib.getpass.getpass", side_effect=["+12345", "12345", "password"]):
            self.assertTrue(client.login())
        self.assertTrue(client.wait_ready())
        methods = [item["@type"] for item in fake.sent]
        self.assertIn("checkAuthenticationPassword", methods)
        self.assertNotIn("checkAuthenticationBotToken", methods)

    def test_folder_updates_preserve_order_and_return_copies(self):
        client, fake = self.client()
        fake.emit({"@type": "updateChatFolders", "chat_folders": [
            {"id": 9, "name": {"text": {"text": "Work"}}, "unused": "drop"},
            {"id": 2, "name": {"text": {"text": "Personal"}}}]})
        deadline = time.monotonic() + 1
        while len(client.list_folders()) != 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        folders = client.list_folders()
        self.assertEqual([folder["id"] for folder in folders], [9, 2])
        self.assertNotIn("unused", folders[0])
        folders[0]["name"]["text"]["text"] = "Changed"
        self.assertEqual(client.list_folders()[0]["name"]["text"]["text"], "Work")
        fake.emit({"@type": "updateChatFolders", "chat_folders": []})
        deadline = time.monotonic() + 1
        while client.list_folders() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(client.list_folders(), [])

    def test_folder_snapshot_is_bounded_and_drops_nested_payloads(self):
        client, fake = self.client()
        client.wait_ready()
        fake.emit({"@type": "updateChatFolders", "chat_folders": [
            {"id": index + 1, "name": {"text": {"text": "я" * 1000,
                                                      "entities": [{"private": "drop"}]}},
             "icon": {"name": "x" * 1000, "private": "drop"},
             "color_id": 3, "is_shareable": True, "has_my_invite_links": False,
             "last_message": {"text": "private"}}
            for index in range(client.MAX_FOLDERS + 1)]})
        self.wait_for(lambda: len(client.list_folders()) == client.MAX_FOLDERS)
        folders = client.list_folders()
        self.assertEqual(len(folders[0]["name"]["text"]["text"]), 256)
        self.assertEqual(len(folders[0]["icon"]["name"]), 64)
        self.assertNotIn("private", json.dumps(folders))
        self.assertNotIn("last_message", folders[0])
        self.assertEqual(folders[0]["color_id"], 3)
        self.assertIs(folders[0]["is_shareable"], True)
        self.assertLess(len(json.dumps(folders, ensure_ascii=False).encode("utf-8")), 150_000)
        fake.emit({"@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationStateLoggingOut"}})
        self.wait_for(lambda: client.authorization_state == "authorizationStateLoggingOut")
        self.assertFalse(client._folders)

    def test_global_receiver_singleton(self):
        self.client()
        with self.assertRaises(TdError) as raised:
            TdClient(self.config)
        self.assertEqual(raised.exception.code, 409)

    def test_receiver_failure_fails_requests_without_hanging(self):
        client, fake = self.client()
        client.wait_ready()
        self.assertEqual(client.authorization_state, "authorizationStateReady")
        fake.respond = False
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(client.request, "getChat", chat_id=1)
            fake.events.put(ValueError("private native details"))
            with self.assertRaises(TdError) as raised:
                request.result(timeout=1)
        self.assertEqual(raised.exception.message, "TDLib receiver failed")
        self.assertIsNone(client.authorization_state)
        self.assertFalse(client._pending)
        self.assertIsNot(raised.exception, client._failure)

    def test_receiver_failure_does_not_accumulate_request_tracebacks(self):
        client, fake = self.client()
        client.wait_ready()
        fake.events.put(ValueError("private native details"))
        self.wait_for(lambda: client._failure is not None)
        errors = []
        for _ in range(5):
            with self.assertRaises(TdError) as raised:
                client.request("getChat", chat_id=1)
            errors.append(raised.exception)
        self.assertEqual(len({id(error) for error in errors}), len(errors))
        self.assertIsNone(client._failure.__traceback__)

    def test_error_text_sanitized_and_406_hidden(self):
        from telegram_reader.tdlib import _remote_error
        self.assertEqual(_remote_error({"code": 429, "message": "FLOOD_WAIT_9"}).message, "FLOOD_WAIT_9")
        self.assertEqual(_remote_error({"code": 400, "message": "secret-hash: 12345"}).message, "TDLib request failed")
        self.assertEqual(_remote_error({"code": 406, "message": "PRIVATE_ERROR"}).message, "TDLib request failed")

    def test_retry_after_parsing_is_strict_and_never_processes_406_text(self):
        from telegram_reader.tdlib import _remote_error
        for text, seconds in (("Too Many Requests: retry after 12", 12), ("FLOOD_WAIT_9", 9),
                              ("Too Many Requests: retry after 0", 1), ("private retry after 9", 1),
                              ("Too Many Requests: retry after 9999999999", 1_209_600),
                              ("Too Many Requests: retry after -1", 1), ("FLOOD_WAIT_" + "9" * 1000, 1)):
            self.assertEqual(_remote_error({"code": 429, "message": text}).retry_after, seconds)
        self.assertIsNone(_remote_error({"code": 400, "message": "FLOOD_WAIT_9"}).retry_after)

        class PrivateText:
            def __str__(self):
                raise AssertionError("406 text must not be processed")

        self.assertEqual(_remote_error({"code": 406, "message": PrivateText()}).message, "TDLib request failed")

    def test_missing_library_does_not_poison_process_lock(self):
        with patch("telegram_reader.tdlib.ctypes.CDLL", side_effect=OSError("not installed")):
            with self.assertRaises(TdError):
                TdClient(self.config)
        client, _ = self.client()
        self.assertTrue(client.wait_ready())


if __name__ == "__main__":
    unittest.main()
