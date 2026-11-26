"""Deployment configuration, deliberately separate from MCP tool arguments."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ConfigError("Дата должна быть строкой ISO 8601 с часовым поясом.")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError("Некорректная дата ISO 8601.") from None
    if result.tzinfo is None:
        raise ConfigError("Укажите часовой пояс даты, например Z.")
    return result.astimezone(dt.timezone.utc)


def validate(config, require_telegram=False):
    if not isinstance(config, dict):
        raise ConfigError("Конфигурация должна быть JSON-объектом.")
    public = config.get("public_url", "")
    if not isinstance(public, str):
        raise ConfigError("public_url должен быть HTTPS origin.")
    try:
        url = urlsplit(public)
        port = url.port
    except ValueError:
        raise ConfigError("Некорректный public_url.") from None
    local = url.hostname in ("127.0.0.1", "localhost", "::1")
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path or (url.scheme != "https" and not (local and url.scheme == "http"))):
        raise ConfigError("public_url: HTTPS origin без пути и завершающего /; HTTP только loopback.")
    if not isinstance(config.get("port", 8080), int) or isinstance(config.get("port"), bool) or not 1 <= config.get("port", 8080) <= 65535:
        raise ConfigError("port должен быть целым числом 1..65535.")
    for name in ("owner_secret_sha256", "desktop_token_sha256"):
        value = config.get(name, "")
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ConfigError(f"{name}: выполните init для создания ключей.")
    for name in ("state_directory", "database_directory", "files_directory", "tdjson_path"):
        value = config.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ConfigError(f"{name} должен быть абсолютным путём.")
    key = config.get("database_encryption_key", "")
    if not isinstance(key, str) or len(key) < 32:
        raise ConfigError("database_encryption_key должен содержать случайный секрет длиной от 32 символов.")
    clients = config.get("oauth_clients", [])
    if not isinstance(clients, list) or len(clients) > 10:
        raise ConfigError("oauth_clients должен быть списком до 10 клиентов.")
    seen = set()
    for client in clients:
        if not isinstance(client, dict) or not isinstance(client.get("client_id"), str) or not 1 <= len(client["client_id"]) <= 200 or client["client_id"] in seen:
            raise ConfigError("OAuth client_id должен быть непустым и уникальным.")
        seen.add(client["client_id"])
        redirects = client.get("redirect_uris")
        if not isinstance(redirects, list) or not redirects or len(redirects) > 10:
            raise ConfigError("OAuth-клиенту нужен точный redirect_uris allowlist.")
        for redirect in redirects:
            if not isinstance(redirect, str) or len(redirect) > 2048:
                raise ConfigError("Некорректный OAuth redirect URI.")
            try:
                redir = urlsplit(redirect)
                _ = redir.port
            except ValueError:
                raise ConfigError("Некорректный OAuth redirect URI.") from None
            if (not redir.hostname or redir.username or redir.password or redir.fragment
                    or (redir.scheme != "https" and not (redir.scheme == "http" and redir.hostname in ("127.0.0.1", "localhost", "::1")))
                    or "*" in redirect or any(ord(c) < 33 for c in redirect)):
                raise ConfigError("OAuth redirect URI: точный HTTPS URL, HTTP только loopback.")
        secret = client.get("client_secret_sha256")
        if secret is not None and (not isinstance(secret, str) or not re.fullmatch(r"[0-9a-f]{64}", secret)):
            raise ConfigError("Некорректный client_secret_sha256.")
    origins = config.get("allowed_origins", [])
    if not isinstance(origins, list) or len(origins) > 10:
        raise ConfigError("allowed_origins должен быть списком точных HTTPS origins.")
    for origin in origins:
        if not isinstance(origin, str):
            raise ConfigError("Некорректный allowed_origins.")
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ConfigError("allowed_origins: только точные HTTPS origins.")
    if config.get("chat_access", "selected") not in ("selected", "all", "channels"):
        raise ConfigError("chat_access должен быть selected, all или channels.")
    grants = config.get("allowed_chats", {})
    if not isinstance(grants, dict) or len(grants) > 100:
        raise ConfigError("allowed_chats должен быть объектом до 100 чатов.")
    for chat_id in grants:
        if not isinstance(chat_id, str) or not re.fullmatch(r"-?[1-9][0-9]{0,18}", chat_id) or not -(2**63) <= int(chat_id) < 2**63:
            raise ConfigError("Некорректный chat_id.")
    policies = list(grants.values())
    if config.get("chat_access") in ("all", "channels"):
        policies.append(config.get("all_chats_grant"))
    for grant in policies:
        if not isinstance(grant, dict) or not isinstance(grant.get("consent_note"), str) or not grant["consent_note"].strip():
            raise ConfigError("Для разрешения чата нужны даты и consent_note.")
        if timestamp(grant.get("not_before")) >= timestamp(grant.get("expires_at")):
            raise ConfigError("expires_at должен быть позже not_before.")
    if require_telegram:
        api_id = config.get("api_id")
        if not isinstance(api_id, int) or isinstance(api_id, bool) or api_id <= 0 or not isinstance(config.get("api_hash"), str) or not re.fullmatch(r"[a-fA-F0-9]{32}", config["api_hash"]):
            raise ConfigError("Перед login задайте свои api_id и api_hash из my.telegram.org.")
    return config


def load(path, require_telegram=False):
    path = Path(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ConfigError("Файл конфигурации должен иметь права 600 (или 400).")
        if info.st_size > 131072:
            raise ConfigError("Слишком большой файл конфигурации.")
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ConfigError("Не удалось прочитать JSON-конфигурацию.") from None
    return validate(config, require_telegram)


def save(path, config, *, create=False):
    validate(config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if create and path.exists():
        raise ConfigError("Конфигурация уже существует; перезапись отменена.")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temp, path)
            os.unlink(temp)
        else:
            os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
