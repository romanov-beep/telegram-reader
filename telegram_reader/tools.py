"""Allowlisted, read-only TDLib tools for messages and requested photos."""
from __future__ import annotations

import base64
import json
import re
import time
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from types import MappingProxyType

MAX_RESULT_BYTES = 48 * 1024
MAX_PHOTO_PREVIEW_BYTES = 192 * 1024
MAX_PHOTO_ORIGINAL_BYTES = 1024 * 1024
_PREVIEW_EDGE_PIXELS = 800
_PHOTO_CLEANUP_SECONDS = 3
_PHOTO_RECHECK_SECONDS = 2
_PHOTO_LOCK = Lock()
_TOOL_DEADLINE = ContextVar("telegram_tool_deadline", default=None)
TOOL_TIMEOUT_SECONDS = 25
_ID = re.compile(r"-?[1-9][0-9]*\Z")
_PAGE_NOTE = "One bounded TDLib page; may be partial or locally cached. No exhaustive-history guarantee."


class ToolError(Exception):
    def __init__(self, code, *, retry_after=None):
        super().__init__(code)
        self.retry_after = retry_after if type(retry_after) is int and 1 <= retry_after <= 1209600 else None


def _id(value, *, positive=False):
    if not isinstance(value, str) or len(value) > 20 or not _ID.fullmatch(value):
        raise ToolError("invalid_arguments")
    number = int(value)
    if not -(2**63) <= number < 2**63 or (positive and number <= 0):
        raise ToolError("invalid_arguments")
    return number


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("invalid date")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Timezone required")
    return result.astimezone(timezone.utc)


def _wrap(result, error=False):
    response = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}], "structuredContent": result}
    if error:
        response["isError"] = True
    return response


def _result_size(result):
    # ASCII JSON is one byte per character. Retain the conservative wire budget.
    return len(json.dumps(_wrap(result), ensure_ascii=True))


def _fits(result):
    return _result_size(result) <= MAX_RESULT_BYTES


def _value_size(value):
    """Bytes contributed by a value to both representations in _wrap."""
    # Cursors change on every row. Null and decimal IDs need no JSON escaping.
    if value is None:
        return 8
    if isinstance(value, str) and _ID.fullmatch(value):
        return 2 * len(value) + 6
    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(json.dumps(compact, ensure_ascii=True)) - 2 + len(json.dumps(value, ensure_ascii=True))


class _ResultBudget:
    """Size a growing list in linear work without retaining serialized rows.

    Callers must use append/update for mutations while this budget is active.
    A list separator costs one byte in the text JSON and two in structured JSON.
    """
    def __init__(self, result, list_key):
        self.result = result
        self.items = result[list_key]
        self._size = None

    @property
    def size(self):
        if self._size is None:
            self._size = _result_size(self.result)
        return self._size

    def _changed_size(self, changes):
        return self.size + sum(_value_size(value) - _value_size(self.result[key])
                               for key, value in changes.items())

    def update(self, **changes):
        size = self._changed_size(changes)
        if size > MAX_RESULT_BYTES:
            return False
        self.result.update(changes)
        self._size = size
        return True

    def append(self, item, **changes):
        # The first row uses one full measurement, avoiding fixed overhead for
        # single-row pages. Later rows serialize only their own contribution.
        if self._size is None:
            original = {key: self.result[key] for key in changes}
            self.items.append(item)
            self.result.update(changes)
            size = _result_size(self.result)
            if size > MAX_RESULT_BYTES:
                self.items.pop()
                self.result.update(original)
                return False
            self._size = size
            return True
        size = self._changed_size(changes) + _value_size(item) + (3 if self.items else 0)
        if size > MAX_RESULT_BYTES:
            return False
        self.items.append(item)
        self.result.update(changes)
        self._size = size
        return True


def _photo_rendition(content, variant, max_bytes):
    photo = content.get("photo")
    sizes = photo.get("sizes") if isinstance(photo, dict) else None
    if not isinstance(sizes, list):
        raise ToolError("photo_unavailable")

    candidates = []
    for size in sizes:
        if not isinstance(size, dict):
            continue
        width, height, file = size.get("width"), size.get("height"), size.get("photo")
        if not isinstance(file, dict) or file.get("@type") != "file" or not isinstance(file.get("local"), dict):
            continue
        if not all(type(value) is int and value > 0 for value in (width, height, file.get("id"))):
            continue
        if type(file.get("size")) is not int or file["size"] < 0:
            continue
        candidates.append((width, height, file))
    if not candidates:
        raise ToolError("photo_unavailable")

    def area(candidate):
        return candidate[0] * candidate[1], candidate[2]["size"]

    if variant == "original":
        return max(candidates, key=area)
    fitting = [candidate for candidate in candidates if candidate[2]["size"] <= max_bytes]
    if not fitting:
        raise ToolError("photo_too_large")
    small = [candidate for candidate in fitting if max(candidate[:2]) <= _PREVIEW_EDGE_PIXELS]
    if small:
        return max(small, key=area)
    return min(fitting, key=area)


def _build_descriptors():
    identifier = {"type": "string", "pattern": r"^-?[1-9][0-9]*$", "description": "Decimal signed 64-bit ID, encoded as a string."}
    message_id = {"type": "string", "pattern": r"^[1-9][0-9]*$"}
    limit = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}
    text_limit = {"type": "integer", "minimum": 0, "maximum": 4000, "default": 4000,
                  "description": "Maximum text/caption characters per message. Use short previews, then get_message for selected messages."}
    definitions = [
        ("status", "Check connection and access mode for diagnosis or when status is requested. Call the requested read tool directly for ordinary reads.", {}, []),
        ("list_chats", "List consented chats; excludes secret chats. Selected mode supports next_after_chat_id. All-chat/folder mode returns one bounded loaded TDLib page, partial with no exhaustive guarantee.", {"limit": limit, "after_chat_id": identifier, "folder_id": message_id}, []),
        ("search_chats", "Find loaded Telegram chats by title; returns metadata only for currently consented non-secret chats. One bounded page, no exhaustive guarantee.", {"query": {"type": "string", "minLength": 1, "maxLength": 256}, "limit": limit}, ["query"]),
        ("list_folders", "List latest Telegram chat-folder metadata from TDLib updates; an empty snapshot can also mean no update received yet. Membership is read dynamically with list_chats folder_id.", {}, []),
        ("read_history", "Read one bounded page in one consented chat. " + _PAGE_NOTE + " Continue with next_before_message_id; filtered messages can produce an empty page with a cursor.", {"chat_id": identifier, "limit": limit, "before_message_id": message_id, "max_text_chars": text_limit}, ["chat_id"]),
        ("search_messages", "Search one consented chat only. " + _PAGE_NOTE, {"chat_id": identifier, "query": {"type": "string", "minLength": 1, "maxLength": 256}, "limit": limit, "before_message_id": message_id, "max_text_chars": text_limit}, ["chat_id", "query"]),
        ("get_message", "Read one message within the chat's consent time window; excludes expiring content. For a photo with known IDs, call telegram_get_photo directly.", {"chat_id": identifier, "message_id": message_id}, ["chat_id", "message_id"]),
        ("get_photo", "Read the photo attached to one consented message. Call directly with the known chat_id and message_id. Preview returns an MCP image. If the user requests the original, select original on the first call to export the largest JPEG rendition Telegram provides, up to 1 MiB.", {"chat_id": identifier, "message_id": message_id, "variant": {"type": "string", "enum": ["preview", "original"], "default": "preview"}}, ["chat_id", "message_id"]),
    ]
    schemes = [{"type": "oauth2", "scopes": ["telegram.read"]}]
    return tuple({"name": "telegram_" + name, "description": description,
                  "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                  "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                  "securitySchemes": schemes, "_meta": {"securitySchemes": schemes}}
                 for name, description, properties, required in definitions)


_DESCRIPTORS = _build_descriptors()
_TOOL_ARGUMENTS = MappingProxyType({
    item["name"]: (frozenset(item["inputSchema"]["properties"]), frozenset(item["inputSchema"]["required"]))
    for item in _DESCRIPTORS
})


class ReadTools:
    def __init__(self, client, config: dict):
        self.client = client
        self.config = config

    @staticmethod
    def descriptors(*, stdio=False):
        # Callers receive their own nested schemas and security metadata.
        descriptors = [deepcopy(item) for item in _DESCRIPTORS]
        if stdio:
            for descriptor in descriptors:
                descriptor["securitySchemes"] = [{"type": "noauth"}]
                descriptor["_meta"]["securitySchemes"] = [{"type": "noauth"}]
        return descriptors

    def _validate(self, name, args):
        keys = _TOOL_ARGUMENTS.get(name) if isinstance(name, str) else None
        if keys is None:
            raise ToolError("unknown_tool")
        properties, required = keys
        if not isinstance(args, dict) or not args.keys() <= properties or not args.keys() >= required:
            raise ToolError("invalid_arguments")
        for key, value in args.items():
            if key in {"chat_id", "after_chat_id"}:
                _id(value)
            elif key in {"message_id", "before_message_id", "folder_id"}:
                _id(value, positive=True)
            elif key == "variant":
                if value not in ("preview", "original"):
                    raise ToolError("invalid_arguments")
            elif key == "limit":
                if type(value) is not int or not 1 <= value <= 50:
                    raise ToolError("invalid_arguments")
            elif key == "max_text_chars":
                if type(value) is not int or not 0 <= value <= 4000:
                    raise ToolError("invalid_arguments")
            elif key == "query":
                if not isinstance(value, str) or not value.strip() or not 1 <= len(value) <= 256:
                    raise ToolError("invalid_arguments")

    def _grant(self, chat_id):
        try:
            _id(chat_id)
            grants = self.config.get("allowed_chats", {})
            grant = self.config["all_chats_grant"] if self.config.get("chat_access") in ("all", "channels") else grants[chat_id]
            if not isinstance(grant["consent_note"], str) or not grant["consent_note"].strip():
                raise ValueError()
            start, end = _utc(grant["not_before"]), _utc(grant["expires_at"])
            if not start <= datetime.now(timezone.utc) < end:
                raise ValueError()
            return start.timestamp(), end.timestamp()
        except (KeyError, TypeError, ValueError, ToolError, AttributeError):
            raise ToolError("chat_not_authorized") from None

    def _remaining(self):
        deadline = _TOOL_DEADLINE.get()
        remaining = TOOL_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise ToolError("tool_timeout")
        return remaining

    def _request(self, method, *, _reserve_seconds=0, **params):
        remaining = self._remaining() - _reserve_seconds
        if remaining <= 0:
            raise ToolError("tool_timeout")
        try:
            result = self.client.request(method, _timeout=remaining, **params)
        except Exception as error:
            if getattr(error, "code", None) == 429:
                raise ToolError("telegram_rate_limited", retry_after=getattr(error, "retry_after", None)) from None
            if getattr(error, "code", None) == 504:
                raise ToolError("telegram_timeout") from None
            if getattr(error, "code", None) == 503:
                raise ToolError("telegram_unavailable") from None
            if getattr(error, "code", None) == 401:
                raise ToolError("telegram_login_required") from None
            raise ToolError("telegram_request_failed") from None
        self._remaining()
        if isinstance(result, dict) and result.get("@type") == "error" and result.get("code") == 429:
            raise ToolError("telegram_rate_limited")
        if isinstance(result, dict) and result.get("@type") == "error" and result.get("code") == 504:
            raise ToolError("telegram_timeout")
        if isinstance(result, dict) and result.get("@type") == "error" and result.get("code") == 503:
            raise ToolError("telegram_unavailable")
        if isinstance(result, dict) and result.get("@type") == "error" and result.get("code") == 401:
            raise ToolError("telegram_login_required")
        if not isinstance(result, dict) or result.get("@type") == "error":
            raise ToolError("telegram_request_failed")
        return result

    def _chat(self, chat_id):
        self._grant(chat_id)
        chat = self._request("getChat", chat_id=_id(chat_id))
        self._grant(chat_id)
        if chat.get("has_protected_content") is True or str(chat.get("id")) != chat_id or not isinstance(chat.get("type"), dict) or chat["type"].get("@type") == "chatTypeSecret":
            raise ToolError("chat_not_authorized")
        if self.config.get("chat_access") == "channels":
            chat_type = chat["type"]
            if chat_type.get("@type") != "chatTypeSupergroup" or chat_type.get("is_channel") is not True:
                raise ToolError("chat_not_authorized")
        return chat

    def _message(self, message, chat_id, max_text_chars=4000):
        start, end = self._grant(chat_id)
        if not isinstance(message, dict) or str(message.get("chat_id")) != chat_id:
            return None
        if message.get("can_be_saved") is False:
            return None
        date = message.get("date")
        if type(date) is not int or not start <= date < end:
            return None
        content = message.get("content", {})
        if not isinstance(content, dict):
            return None
        for obj in (message, content):
            if obj.get("self_destruct_type") or any(obj.get(key) for key in ("ttl", "ttl_expires_in", "self_destruct_in", "auto_delete_in")):
                return None
        try:
            message_id = str(message["id"])
            _id(message_id, positive=True)
            date_string = datetime.fromtimestamp(date, timezone.utc).isoformat().replace("+00:00", "Z")
        except (KeyError, ToolError, ValueError, OverflowError, OSError):
            return None
        kind = content.get("@type")
        if not isinstance(kind, str) or not re.fullmatch(r"message[A-Za-z0-9]+", kind):
            kind = "unknown"
        result = {"chat_id": chat_id, "message_id": message_id, "date": date_string, "content_type": kind}
        sender = message.get("sender_id", {})
        if isinstance(sender, dict):
            sender_type = sender.get("@type")
            field = {"messageSenderUser": "user_id", "messageSenderChat": "chat_id"}.get(sender_type)
            if field:
                try:
                    sender_id = str(sender[field])
                    _id(sender_id)
                    result["sender"] = {"type": sender_type, "id": sender_id}
                except (KeyError, ToolError):
                    pass
        formatted = content.get("text") if kind == "messageText" else content.get("caption")
        text = formatted.get("text") if isinstance(formatted, dict) else None
        if isinstance(text, str):
            result["text"] = text[:max_text_chars]
            result["text_truncated"] = len(text) > max_text_chars
        return result

    def _status(self):
        # Only emit known state names; auth data and arbitrary error strings are private.
        state = getattr(self.client, "authorization_state", None)
        if isinstance(state, dict):
            state = state.get("@type")
        allowed = {"authorizationStateWaitTdlibParameters", "authorizationStateWaitPhoneNumber", "authorizationStateWaitPremiumPurchase", "authorizationStateWaitEmailAddress", "authorizationStateWaitEmailCode", "authorizationStateWaitCode", "authorizationStateWaitOtherDeviceConfirmation", "authorizationStateWaitRegistration", "authorizationStateWaitPassword", "authorizationStateReady", "authorizationStateLoggingOut", "authorizationStateClosing", "authorizationStateClosed"}
        return {"authorization_state": state if isinstance(state, str) and state in allowed else "unknown", "ready": state == "authorizationStateReady", "read_only": True, "access_mode": self.config.get("chat_access", "selected")}

    def call(self, name: str, arguments: dict):
        deadline_token = _TOOL_DEADLINE.set(time.monotonic() + TOOL_TIMEOUT_SECONDS)
        try:
            self._validate(name, arguments)
            if name == "telegram_status":
                return _wrap(self._status())
            if name == "telegram_list_folders":
                return _wrap(self._list_folders())
            if name == "telegram_search_chats":
                return _wrap(self._list_dynamic_chats(arguments, search=True))
            if name == "telegram_list_chats":
                result = self._list_chats(arguments)
            else:
                chat_id = arguments["chat_id"]
                self._grant(chat_id)
                self._chat(chat_id)
                if name == "telegram_get_photo":
                    return self._get_photo(chat_id, arguments["message_id"], arguments.get("variant", "preview"))
                if name == "telegram_get_message":
                    raw = self._request("getMessage", chat_id=_id(chat_id), message_id=_id(arguments["message_id"], positive=True))
                    message = self._message(raw, chat_id)
                    if message is None or message["message_id"] != arguments["message_id"]:
                        raise ToolError("message_not_available")
                    result = {"message": message}
                    self._shrink(result, message)
                else:
                    result = self._page(name, arguments)
            return _wrap(result)
        except ToolError as error:
            result = {"error": str(error)}
            if error.retry_after is not None:
                result["retry_after_seconds"] = error.retry_after
            return _wrap(result, True)
        except Exception:
            return _wrap({"error": "telegram_request_failed"}, True)
        finally:
            _TOOL_DEADLINE.reset(deadline_token)

    def _get_photo(self, chat_id, message_id, variant):
        # A single TDLib client owns the cache, including across SDK workers.
        # Serialize downloads and cleanup so an overlapping reader can't
        # remove the same cached file.
        if not _PHOTO_LOCK.acquire(blocking=False):
            raise ToolError("photo_busy")
        try:
            content = self._authorized_photo(chat_id, message_id)

            max_bytes = MAX_PHOTO_ORIGINAL_BYTES if variant == "original" else MAX_PHOTO_PREVIEW_BYTES
            width, height, file = _photo_rendition(content, variant, max_bytes)
            data, size = self._photo_bytes(file, max_bytes)
            if file["local"].get("is_downloading_completed") is not True:
                self._chat(chat_id)
                current = self._authorized_photo(chat_id, message_id)
                _, _, current_file = _photo_rendition(current, variant, max_bytes)
                if current_file["id"] != file["id"]:
                    raise ToolError("message_not_available")
            self._grant(chat_id)
            self._remaining()
            filename = f"telegram-photo-{message_id}.jpg"
            result = _wrap({"chat_id": chat_id, "message_id": message_id, "variant": variant,
                            "filename": filename, "mime_type": "image/jpeg",
                            "width": width, "height": height, "size_bytes": size})
            if variant == "preview":
                item = {"type": "image", "mimeType": "image/jpeg", "data": data}
            else:
                item = {"type": "resource", "resource": {
                    "uri": f"telegram-photo://message/{chat_id}/{message_id}/{filename}",
                    "mimeType": "image/jpeg", "blob": data,
                }}
            result["content"].append(item)
            return result
        finally:
            _PHOTO_LOCK.release()

    def _authorized_photo(self, chat_id, message_id):
        raw = self._request("getMessage", chat_id=_id(chat_id), message_id=_id(message_id, positive=True))
        message = self._message(raw, chat_id, max_text_chars=0)
        if (message is None or message["message_id"] != message_id
                or raw.get("can_be_saved") is not True or raw.get("ephemeral_content") is not None):
            raise ToolError("message_not_available")
        content = raw["content"]
        if content.get("@type") != "messagePhoto":
            raise ToolError("message_has_no_photo")
        if content.get("is_secret") is not False:
            raise ToolError("message_not_available")
        return content

    def _photo_bytes(self, file, max_bytes):
        if file["size"] > max_bytes:
            raise ToolError("photo_too_large")
        file_id = file["id"]
        local = file["local"]
        if local.get("is_downloading_completed") is True:
            return self._read_photo_bytes(file, max_bytes)
        if local.get("is_downloading_active") is True or local.get("can_be_downloaded") is not True:
            raise ToolError("photo_unavailable")

        owned_cache_entry = not any(local.get(key) for key in ("path", "downloaded_size", "downloaded_prefix_size"))
        complete = False
        reserve = _PHOTO_CLEANUP_SECONDS + _PHOTO_RECHECK_SECONDS
        try:
            file = self._request("downloadFile", file_id=file_id, priority=4, offset=0,
                                 limit=max_bytes + 1, synchronous=True, _reserve_seconds=reserve)
            if (file.get("@type") != "file" or file.get("id") != file_id
                    or not isinstance(file.get("local"), dict)):
                raise ToolError("photo_unavailable")
            complete = file["local"].get("is_downloading_completed") is True
            return self._read_photo_bytes(file, max_bytes, reserve_seconds=reserve)
        finally:
            # Wait for TDLib to cancel before removing a partial cache entry.
            # Keep cache entries that were present before this call.
            if not complete:
                self._request("cancelDownloadFile", file_id=file_id, only_if_pending=False,
                              _reserve_seconds=_PHOTO_CLEANUP_SECONDS / 2)
            if owned_cache_entry:
                self._request("deleteFile", file_id=file_id,
                              _reserve_seconds=_PHOTO_RECHECK_SECONDS if complete else 0)

    def _read_photo_bytes(self, file, max_bytes, *, reserve_seconds=0):
        size = file.get("size")
        local = file["local"]
        prefix = local.get("downloaded_prefix_size")
        if ((type(size) is int and size > max_bytes)
                or (type(prefix) is int and prefix > max_bytes)):
            raise ToolError("photo_too_large")
        if (local.get("is_downloading_completed") is not True
                or type(size) is not int or size <= 0):
            raise ToolError("photo_unavailable")
        response = self._request("readFilePart", file_id=file["id"], offset=0, count=size,
                                 _reserve_seconds=reserve_seconds)
        data = response.get("data")
        if response.get("@type") != "data" or not isinstance(data, str) or len(data) != 4 * ((size + 2) // 3):
            raise ToolError("photo_unavailable")
        try:
            jpeg = base64.b64decode(data, validate=True)
        except (TypeError, ValueError):
            raise ToolError("photo_unavailable") from None
        if len(jpeg) != size or not (jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")):
            raise ToolError("photo_unavailable")
        return data, size

    def _shrink(self, result, message):
        while not _fits(result) and message.get("text"):
            message["text"] = message["text"][:len(message["text"]) // 2]
            message["text_truncated"] = True
        if not _fits(result):
            raise ToolError("result_too_large")

    def _list_folders(self):
        try:
            self.client.wait_ready(timeout=self._remaining())
            folders = self.client.list_folders(_timeout=self._remaining())
        except Exception:
            raise ToolError("folders_unavailable") from None
        self._remaining()
        if not isinstance(folders, list):
            raise ToolError("folders_unavailable")
        result = {"folders": [], "partial": True}
        budget = _ResultBudget(result, "folders")
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            folder_id = str(folder.get("id"))
            try:
                _id(folder_id, positive=True)
            except ToolError:
                continue
            title = folder.get("name", "")
            if isinstance(title, dict):
                title = title.get("text", "")
                if isinstance(title, dict):
                    title = title.get("text", "")
            item = {"folder_id": folder_id, "title": title[:256] if isinstance(title, str) else ""}
            if not budget.append(item):
                break
        return result

    def _list_dynamic_chats(self, args, search=False):
        if "after_chat_id" in args:
            raise ToolError("invalid_arguments")
        # Validate live broad consent before any account-wide TDLib request.
        if self.config.get("chat_access") in ("all", "channels"):
            self._grant("1")
        chat_list = {"@type": "chatListMain"}
        if "folder_id" in args:
            folder_id = _id(args["folder_id"], positive=True)
            if folder_id >= 2**31:
                raise ToolError("invalid_arguments")
            chat_list = {"@type": "chatListFolder", "chat_folder_id": folder_id}
        if search:
            raw = self._request("searchChats", query=args["query"], limit=args.get("limit", 20))
        else:
            raw = self._request("getChats", chat_list=chat_list, limit=args.get("limit", 20))
        if self.config.get("chat_access") in ("all", "channels"):
            self._grant("1")
        ids = raw.get("chat_ids", [])
        if not isinstance(ids, list):
            raise ToolError("telegram_request_failed")
        result = {"chats": [], "next_after_chat_id": None, "partial": True, "note": "One bounded loaded TDLib chat page; no exhaustive-discovery guarantee."}
        budget = _ResultBudget(result, "chats")
        for value in ids[:args.get("limit", 20)]:
            self._remaining()
            chat_id = str(value)
            try:
                chat = self._chat(chat_id)
            except ToolError as error:
                if str(error) in {"telegram_rate_limited", "telegram_timeout", "telegram_unavailable", "telegram_login_required", "tool_timeout"}:
                    raise
                continue
            item = {"chat_id": chat_id, "title": chat.get("title", "")[:256] if isinstance(chat.get("title", ""), str) else "", "type": chat["type"].get("@type", "unknown")}
            if not budget.append(item):
                break
        return result

    def _list_chats(self, args):
        if self.config.get("chat_access") in ("all", "channels"):
            return self._list_dynamic_chats(args)
        if "folder_id" in args:
            return self._list_dynamic_chats(args)
        grants = self.config.get("allowed_chats", {})
        if not isinstance(grants, dict):
            grants = {}
        ids = []
        for chat_id in grants:
            try:
                self._grant(chat_id)
                ids.append(chat_id)
            except ToolError:
                pass
        ids.sort(key=int)
        after = args.get("after_chat_id")
        if after is not None:
            self._grant(after)
            ids = [value for value in ids if int(value) > int(after)]
        result = {"chats": [], "next_after_chat_id": None, "partial": True}
        budget = _ResultBudget(result, "chats")
        for index, chat_id in enumerate(ids[:50]):
            self._remaining()
            cursor = chat_id if index + 1 < len(ids) else None
            try:
                chat = self._chat(chat_id)
            except ToolError as error:
                if str(error) in {"telegram_rate_limited", "telegram_timeout", "telegram_unavailable", "telegram_login_required", "tool_timeout"}:
                    raise
                if not budget.update(next_after_chat_id=cursor):
                    break
                continue
            item = {"chat_id": chat_id, "title": chat.get("title", "")[:256] if isinstance(chat.get("title", ""), str) else "", "type": chat["type"].get("@type", "unknown")}
            if not budget.append(item, next_after_chat_id=cursor):
                if index == 0:
                    budget.update(next_after_chat_id=after)
                break
            if len(result["chats"]) == args.get("limit", 20):
                break
        return result

    def _page(self, name, args):
        chat_id = args["chat_id"]
        anchor = args.get("before_message_id")
        params = {"chat_id": _id(chat_id), "from_message_id": _id(anchor, positive=True) if anchor else 0, "offset": 0, "limit": args.get("limit", 20) + (1 if anchor else 0)}
        if name == "telegram_search_messages":
            params.update(query=args["query"], filter={"@type": "searchMessagesFilterEmpty"})
            raw = self._request("searchChatMessages", **params)
        else:
            raw = self._request("getChatHistory", **params, only_local=False)
        self._grant(chat_id)
        rows = raw.get("messages", [])
        if not isinstance(rows, list):
            raise ToolError("telegram_request_failed")
        result = {"chat_id": chat_id, "messages": [], "next_before_message_id": None, "partial": True, "note": _PAGE_NOTE}
        budget = _ResultBudget(result, "messages")
        previous_id = _id(anchor, positive=True) if anchor else None
        for row in rows[:params["limit"]]:
            self._remaining()
            # A foreign/malformed row must never move this chat's cursor.
            if not isinstance(row, dict) or str(row.get("chat_id")) != chat_id:
                continue
            try:
                row_id = str(row["id"])
                number = _id(row_id, positive=True)
            except (KeyError, ToolError):
                continue
            # TDLib returns decreasing IDs; offset=0 may include the anchor.
            if anchor and number >= int(anchor):
                continue
            if previous_id is not None:
                if number == previous_id:
                    continue
                if number > previous_id:
                    raise ToolError("telegram_request_failed")
            message = self._message(row, chat_id, args.get("max_text_chars", 4000))
            if message is not None:
                if len(result["messages"]) >= args.get("limit", 20):
                    return result
                if not budget.append(message, next_before_message_id=row_id):
                    if result["messages"]:
                        return result
                    result["messages"].append(message)
                    result["next_before_message_id"] = row_id
                    self._shrink(result, message)
                    budget = _ResultBudget(result, "messages")
            elif not budget.update(next_before_message_id=row_id):
                return result
            previous_id = number
        return result
