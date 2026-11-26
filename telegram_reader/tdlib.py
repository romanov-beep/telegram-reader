"""Bounded, read-only stdlib binding to the official TDLib JSON C interface.

One client per process: td_receive is a process-global stream. No account, library,
or network work happens at import time. Authentication is local and explicit.
"""
from __future__ import annotations

import base64
import copy
from concurrent.futures import Future, TimeoutError as FutureTimeout
import ctypes
import ctypes.util
import fcntl
import getpass
import json
import math
import os
from pathlib import Path
import re
import threading
import time


class TdError(Exception):
    def __init__(self, code: int, message: str, *, retry_after: int | None = None):
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(message)


RUNTIME_METHODS = frozenset({
    "getChats", "getChat", "searchChats", "searchChatMessages", "getChatHistory",
    "getMessage", "getOption", "getChatFolder", "loadChats",
    "downloadFile", "readFilePart", "cancelDownloadFile", "deleteFile",
})
_LOCAL_PHOTO_METHODS = frozenset({"readFilePart", "cancelDownloadFile", "deleteFile"})
_LOGIN_METHODS = frozenset({
    "setAuthenticationPhoneNumber", "checkAuthenticationCode",
    "checkAuthenticationPassword", "setAuthenticationEmailAddress",
    "checkAuthenticationEmailCode",
})
_INTERNAL_METHODS = frozenset({"getAuthorizationState", "setTdlibParameters", "close"})
_PROCESS_LOCK = threading.Lock()


def _remote_error(value: dict) -> TdError:
    code = int(value.get("code", 500))
    # Error 406 must never be displayed. Do not propagate arbitrary server text,
    # which could contain request arguments, into MCP responses or CLI logs.
    if code == 406:
        return TdError(code, "TDLib request failed")
    message = str(value.get("message", ""))
    retry_after = None
    if code == 429:
        # Pinned TDLib NetQueryDelayer emits the first form. Accept the familiar
        # flood identifier too, but never expose arbitrary server text.
        match = re.fullmatch(r"(?:Too Many Requests: retry after |FLOOD_WAIT_)([0-9]{1,10})", message)
        retry_after = min(14 * 24 * 60 * 60, max(1, int(match[1]))) if match else 1
    safe = message if re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", message) else "TDLib request failed"
    return TdError(code, safe, retry_after=retry_after)


def _folder_metadata(folder):
    """Keep only bounded display metadata, never arbitrary nested TDLib data."""
    folder_id = folder.get("id")
    if type(folder_id) is not int or not 0 < folder_id < 2**31:
        return None
    name = folder.get("name", "")
    for _ in range(2):
        if isinstance(name, dict):
            name = name.get("text", "")
    result = {"id": folder_id, "name": {"text": {"text": name[:256] if isinstance(name, str) else ""}}}
    icon = folder.get("icon")
    if isinstance(icon, dict) and isinstance(icon.get("name"), str):
        result["icon"] = {"name": icon["name"][:64]}
    color = folder.get("color_id")
    if type(color) is int and -(2**31) <= color < 2**31:
        result["color_id"] = color
    for key in ("is_shareable", "has_my_invite_links"):
        if type(folder.get(key)) is bool:
            result[key] = folder[key]
    return result


class TdClient:
    MAX_PENDING = 8
    MAX_FOLDERS = 100
    RECEIVE_IDLE_TIMEOUT = 1.0

    def __init__(self, config: dict):
        self._config = dict(config)
        self._timeout = float(config.get("request_timeout", 20))
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            raise ValueError("request_timeout must be positive and finite")
        if not isinstance(config.get("api_id"), int) or isinstance(config.get("api_id"), bool) or config["api_id"] <= 0:
            raise ValueError("api_id must be a positive integer")
        if not isinstance(config.get("api_hash"), str) or not config["api_hash"]:
            raise ValueError("api_hash is required")
        if not isinstance(config.get("database_encryption_key", ""), str):
            raise ValueError("database_encryption_key must be a string")
        if not _PROCESS_LOCK.acquire(blocking=False):
            raise TdError(409, "Only one TDLib client is allowed per process")
        self._owns_process = True
        self._lock_file = None
        self._condition = threading.Condition(threading.RLock())
        self._pending = {}
        # A Python timeout cannot cancel TDLib's network retries. Retain only
        # request ID/method until its late response, so native work stays bounded.
        self._abandoned = {}
        self._retry_until = 0.0
        self._sequence = 0
        self._state = None
        self._folders = []
        self._failure = None
        self._closing = False
        self._closed = False
        self._parameters_sent = False
        try:
            for key in ("database_directory", "files_directory"):
                path = Path(config[key]).expanduser().absolute()
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("TDLib directories must be real directories")
                os.chmod(path, 0o700)
                self._config[key] = str(path)
            lock_path = Path(self._config["database_directory"]) / ".telegram-reader.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self._lock_file = os.fdopen(fd, "a")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TdError(409, "Telegram session is already in use") from None
            library = config.get("tdjson_path") or ctypes.util.find_library("tdjson")
            if not library:
                raise TdError(503, "Official libtdjson was not found; set tdjson_path")
            try:
                self._lib = ctypes.CDLL(str(library))
                for name, args, result in (
                    ("td_create_client_id", [], ctypes.c_int),
                    ("td_send", [ctypes.c_int, ctypes.c_char_p], None),
                    ("td_receive", [ctypes.c_double], ctypes.c_char_p),
                    ("td_execute", [ctypes.c_char_p], ctypes.c_char_p),
                ):
                    function = getattr(self._lib, name)
                    function.argtypes, function.restype = args, result
            except (OSError, AttributeError):
                raise TdError(503, "Unable to load the modern official libtdjson interface") from None
            # Before starting the global receiver, disable native logging entirely.
            result = self._lib.td_execute(b'{"@type":"setLogStream","log_stream":{"@type":"logStreamEmpty"}}')
            if not result or json.loads(result).get("@type") != "ok":
                raise TdError(503, "Unable to disable TDLib native logging")
            self._client_id = self._lib.td_create_client_id()
            if self._client_id <= 0:
                raise TdError(503, "Unable to create TDLib client")
            self._receiver = threading.Thread(target=self._receive, name="tdlib-receiver", daemon=True)
            self._receiver.start()
            self._submit("getAuthorizationState", {}, internal=True)
        except BaseException:
            # Initialization before client creation owns no native session.
            if not getattr(self, "_client_id", 0):
                self._release()
            else:
                try:
                    self.close()
                except TdError:
                    pass
            raise

    @property
    def authorization_state(self):
        with self._condition:
            return None if self._failure else self._state

    def list_folders(self, *, _timeout=None) -> list:
        """Return the latest ordered folder metadata; membership is queried live.

        An empty snapshot can mean no folders or no update received yet. Never
        derive membership from a folder's explicit included_chat_ids alone.
        """
        self.wait_ready(self._request_timeout(_timeout))
        with self._condition:
            return copy.deepcopy(self._folders)

    def _release(self):
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
        if self._owns_process:
            self._owns_process = False
            _PROCESS_LOCK.release()

    def _request_timeout(self, timeout):
        timeout = self._timeout if timeout is None else float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise TdError(504, "TDLib request deadline expired")
        return min(timeout, self._timeout)

    def _check_failure(self):
        if self._failure:
            # Raising a stored exception repeatedly grows its traceback and can
            # retain request frames. Keep only the failure description on self.
            raise TdError(self._failure.code, self._failure.message, retry_after=self._failure.retry_after)

    def _submit(self, method, params, *, internal=False, timeout=None, deadline=None):
        allowed = _INTERNAL_METHODS if internal else RUNTIME_METHODS | _LOGIN_METHODS
        if method not in allowed or any(str(key).startswith("@") for key in params):
            raise TdError(403, "TDLib method or reserved parameter is not allowed")
        if deadline is None:
            deadline = time.monotonic() + self._request_timeout(timeout)
        with self._condition:
            self._check_failure()
            if self._closed or (self._closing and method != "close"):
                raise TdError(503, "TDLib client is closed")
            now = time.monotonic()
            if now >= deadline:
                raise TdError(504, "TDLib request deadline expired")
            if method in RUNTIME_METHODS and method not in _LOCAL_PHOTO_METHODS and now < self._retry_until:
                raise TdError(429, "TDLib rate limit active", retry_after=math.ceil(self._retry_until - now))
            if len(self._pending) + len(self._abandoned) >= self.MAX_PENDING:
                raise TdError(429, "Too many pending TDLib requests")
            self._sequence += 1
            extra = str(self._sequence)
            payload = json.dumps({**params, "@type": method, "@extra": extra}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if time.monotonic() >= deadline:
                raise TdError(504, "TDLib request deadline expired")
            future = Future()
            self._pending[extra] = (future, method, deadline)
            try:
                self._lib.td_send(self._client_id, payload)
            except Exception:
                self._pending.pop(extra, None)
                raise TdError(503, "TDLib send failed") from None
            return extra, future

    def _request(self, method, *, _timeout=None, **params):
        deadline = time.monotonic() + self._request_timeout(_timeout)
        extra, future = self._submit(method, params, deadline=deadline)
        try:
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            with self._condition:
                self._expire_request(extra)
            raise TdError(504, "TDLib request timed out") from None

    def request(self, method: str, *, _timeout=None, **params) -> dict:
        """Execute a bounded read request; authentication is available only in login()."""
        if method not in RUNTIME_METHODS:
            raise TdError(403, "TDLib method is not allowed at runtime")
        deadline = time.monotonic() + self._request_timeout(_timeout)
        self.wait_ready(max(0, deadline - time.monotonic()))
        return self._request(method, _timeout=deadline - time.monotonic(), **params)

    def wait_ready(self, timeout=None):
        """Wait for an existing session. Never prompt or initiate a login."""
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        with self._condition:
            self._check_failure()
            if self._closed or self._closing:
                raise TdError(503, "TDLib client is closed")
            while self._state != "authorizationStateReady":
                self._check_failure()
                if self._closed or self._closing:
                    raise TdError(503, "TDLib client is closed")
                if self._state and self._state != "authorizationStateWaitTdlibParameters":
                    raise TdError(401, "Telegram login is required; run the local login command")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TdError(504, "Waiting for TDLib authorization timed out")
                self._condition.wait(remaining)
        return True

    def _parameters(self):
        c = self._config
        return dict(use_test_dc=False, database_directory=c["database_directory"],
                    files_directory=c["files_directory"],
                    database_encryption_key=base64.b64encode(c.get("database_encryption_key", "").encode()).decode("ascii"),
                    use_file_database=False, use_chat_info_database=False,
                    use_message_database=False, use_secret_chats=False,
                    api_id=c["api_id"], api_hash=c["api_hash"],
                    system_language_code="en", device_model="Telegram Reader",
                    system_version="Linux", application_version="1.0")

    def _set_state(self, state):
        self._state = state.get("@type")
        if self._state != "authorizationStateReady":
            self._folders.clear()
        if self._state == "authorizationStateWaitTdlibParameters" and not self._parameters_sent:
            self._parameters_sent = True
            self._submit("setTdlibParameters", self._parameters(), internal=True)
        elif self._state == "authorizationStateClosed":
            self._closed = True
            self._abort(TdError(503, "TDLib client is closed"))
            self._release()
        self._condition.notify_all()

    def _abort(self, error):
        for future, _, _ in self._pending.values():
            if not future.done():
                future.set_exception(TdError(error.code, error.message, retry_after=error.retry_after))
        self._pending.clear()
        self._abandoned.clear()
        self._folders.clear()
        self._condition.notify_all()

    def _expire_request(self, extra):
        pending = self._pending.pop(extra, None)
        if pending:
            future, method, _ = pending
            self._abandoned[extra] = method
            error = TdError(504, "TDLib request timed out")
            if not future.done():
                future.set_exception(error)
            if method in {"setTdlibParameters", "getAuthorizationState"}:
                raise error

    def _receive_timeout(self):
        with self._condition:
            now = time.monotonic()
            for extra, (_, _, deadline) in list(self._pending.items()):
                if now >= deadline:
                    self._expire_request(extra)
            return min([self.RECEIVE_IDLE_TIMEOUT] + [deadline - now for _, _, deadline in self._pending.values()])

    def _dispatch(self, value):
        # This short-lived scope must release completed Future/result references
        # before the next receive blocks. Messages are never an application cache.
        if not isinstance(value, dict):
            raise ValueError("Invalid TDLib response")
        if value.get("@client_id") != self._client_id:
            return
        with self._condition:
            extra = value.get("@extra")
            pending = self._pending.pop(extra, None) if isinstance(extra, str) else None
            abandoned = self._abandoned.pop(extra, None) if isinstance(extra, str) else None
            method = pending[1] if pending else abandoned
            error = _remote_error(value) if method and value.get("@type") == "error" else None
            if error and error.code == 429 and method in RUNTIME_METHODS:
                self._retry_until = max(self._retry_until, time.monotonic() + error.retry_after)
            if pending:
                future, method, deadline = pending
                if time.monotonic() >= deadline:
                    expired = TdError(504, "TDLib request timed out")
                    future.set_exception(expired)
                    if method in {"setTdlibParameters", "getAuthorizationState"}:
                        raise expired
                elif error:
                    future.set_exception(error)
                else:
                    future.set_result(value)
                if error and method in {"setTdlibParameters", "getAuthorizationState"}:
                    raise error
                if not error and method == "getAuthorizationState":
                    self._set_state(value)
            if value.get("@type") == "updateAuthorizationState":
                self._set_state(value["authorization_state"])
            elif value.get("@type") == "updateChatFolders":
                folders = value.get("chat_folders", [])
                if not isinstance(folders, list):
                    raise ValueError("Invalid TDLib folder update")
                self._folders = [metadata for folder in folders[:self.MAX_FOLDERS]
                                 if isinstance(folder, dict) and (metadata := _folder_metadata(folder)) is not None]

    def _receive(self):
        try:
            while not self._closed:
                # c_char_p copies native memory into Python bytes before the next call.
                raw = self._lib.td_receive(self._receive_timeout())
                if raw:
                    self._dispatch(json.loads(raw))
                raw = None
        except Exception as error:
            with self._condition:
                self._failure = (TdError(error.code, error.message, retry_after=error.retry_after)
                                 if isinstance(error, TdError) else TdError(503, "TDLib receiver failed"))
                self._abort(self._failure)
            # Keep the file/process locks: native TDLib may still hold its database.
            try:
                self._lib.td_send(self._client_id, b'{"@type":"close"}')
            except Exception:
                pass

    def login(self):
        """Interactive user login, intended only for a local terminal, never MCP."""
        if not os.isatty(0):
            raise TdError(400, "Login requires a local interactive terminal")
        handled_state = None
        state_deadline = time.monotonic() + self._timeout
        while True:
            with self._condition:
                self._check_failure()
                state = self._state
                if state == "authorizationStateReady":
                    return True
                if self._closed or self._closing:
                    raise TdError(503, "TDLib client is closed")
                if state in (None, "authorizationStateWaitTdlibParameters") or state == handled_state:
                    remaining = state_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TdError(504, "Waiting for authorization state timed out")
                    self._condition.wait(remaining)
                    continue
            if state == "authorizationStateWaitPhoneNumber":
                self._request("setAuthenticationPhoneNumber", phone_number=getpass.getpass("Phone number (+country code): "), settings=None)
            elif state == "authorizationStateWaitCode":
                self._request("checkAuthenticationCode", code=getpass.getpass("Telegram login code: "))
            elif state == "authorizationStateWaitPassword":
                self._request("checkAuthenticationPassword", password=getpass.getpass("Telegram 2FA password: "))
            elif state == "authorizationStateWaitEmailAddress":
                self._request("setAuthenticationEmailAddress", email_address=getpass.getpass("Login email address: "))
            elif state == "authorizationStateWaitEmailCode":
                self._request("checkAuthenticationEmailCode", code={"@type": "emailAddressAuthenticationCode", "code": getpass.getpass("Email login code: ")})
            else:
                raise TdError(401, "Unsupported Telegram authorization state; use an existing user account")
            handled_state = state
            state_deadline = time.monotonic() + self._timeout

    def close(self):
        """Wait for authorizationStateClosed, retaining locks if shutdown times out."""
        with self._condition:
            if self._closed:
                return
            self._check_failure()
            if not self._closing:
                self._closing = True
                # Shutdown has a reserved path even when all eight slots are full.
                self._lib.td_send(self._client_id, b'{"@type":"close"}')
            deadline = time.monotonic() + self._timeout
            while not self._closed:
                self._check_failure()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TdError(504, "TDLib shutdown timed out; session remains locked")
                self._condition.wait(remaining)
        self._receiver.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
