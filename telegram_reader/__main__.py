"""Local owner commands. Account credentials never travel through MCP."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import zipfile

from .config import ConfigError, digest, load, save, timestamp


class LivePolicyTools:
    """Re-read local policy per request so revocations need no daemon restart."""

    def __init__(self, client, config_path, *, stdio=False):
        self.client, self.config_path = client, config_path
        self.stdio = stdio

    def descriptors(self):
        from .tools import ReadTools
        return ReadTools.descriptors(stdio=self.stdio)

    def call(self, name, arguments):
        from .tools import ReadTools
        try:
            config = load(self.config_path)
        except ConfigError:
            return {"isError": True, "content": [{"type": "text", "text": "Локальная политика недоступна; доступ закрыт."}]}
        return ReadTools(self.client, config).call(name, arguments)


def init(args):
    directory = Path(args.state_directory).absolute()
    owner = secrets.token_urlsafe(32)
    desktop = secrets.token_urlsafe(32)
    config = {
        "public_url": args.public_url.rstrip("/"), "bind_host": "127.0.0.1", "port": 8080,
        "allowed_origins": ["https://chatgpt.com"],
        "tdjson_path": str(Path(args.tdjson).absolute()), "api_id": 0, "api_hash": "",
        "database_directory": str(directory / "td"), "files_directory": str(directory / "files"),
        "state_directory": str(directory), "database_encryption_key": secrets.token_urlsafe(32),
        "owner_secret_sha256": digest(owner), "desktop_token_sha256": digest(desktop),
        "oauth_clients": [], "chat_access": "selected", "allowed_chats": {},
        "request_timeout": 20,
    }
    save(args.config, config, create=True)
    print("Конфигурация создана с закрытым доступом к чатам. Добавьте api_id/api_hash локально.")
    print("Сохраните ключи в менеджере паролей: они показаны только сейчас.")
    print("Ключ подтверждения OAuth (вводится в браузере, не пароль Telegram): " + owner)
    print("Desktop bearer token (переменная окружения TG_READER_TOKEN): " + desktop)


def policy(args):
    config = load(args.config)
    if args.command in ("grant-chat", "grant-all", "grant-channels"):
        if timestamp(args.expires_at) <= dt.datetime.now(dt.timezone.utc):
            raise ConfigError("Дата окончания разрешения должна быть в будущем.")
        grant = {"not_before": args.not_before, "expires_at": args.expires_at, "consent_note": args.consent_note}
        if args.command in ("grant-all", "grant-channels"):
            config["chat_access"] = "channels" if args.command == "grant-channels" else "all"
            config["all_chats_grant"] = grant
        else:
            config.setdefault("allowed_chats", {})[args.chat_id] = grant
    elif args.command == "revoke-chat":
        if config.get("chat_access") in ("all", "channels"):
            raise ConfigError("В режиме all сначала выполните access-selected; затем задайте разрешённые чаты.")
        config.setdefault("allowed_chats", {}).pop(args.chat_id, None)
    elif args.command == "access-selected":
        config["chat_access"] = "selected"
        config.pop("all_chats_grant", None)
    elif args.command == "revoke-all":
        config["chat_access"] = "selected"
        config.pop("all_chats_grant", None)
        config["allowed_chats"] = {}
    save(args.config, config)
    print("Политика сохранена; новые вызовы инструментов используют её сразу.")


def oauth_client(args):
    config = load(args.config)
    client = {"client_id": args.client_id, "redirect_uris": args.redirect_uri}
    secret = None
    if args.confidential:
        secret = secrets.token_urlsafe(32)
        client["client_secret_sha256"] = digest(secret)
    config["oauth_clients"] = [c for c in config.get("oauth_clients", []) if c["client_id"] != args.client_id] + [client]
    save(args.config, config)
    print("OAuth-клиент сохранён. Перезапустите службу перед подключением.")
    if secret:
        print("Client secret (сохраните сейчас): " + secret)


def package(args):
    """Generate a remote package only once its actual endpoint is known."""
    config = load(args.config)
    if not config["public_url"].startswith("https://"):
        raise ConfigError("Для web-пакета нужен реальный HTTPS public_url.")
    if not any(c["client_id"] == args.client_id for c in config["oauth_clients"]):
        raise ConfigError("Сначала зарегистрируйте OAuth client_id командой add-oauth-client.")
    root = Path(__file__).resolve().parent.parent
    endpoint = config["public_url"] + "/mcp"
    mcp = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json", "mcpServers": {
        "telegram-reader": {"type": "streamable-http", "url": endpoint, "extensions": {"com.openai": {
            "auth": {"type": "oauth", "client": {"mode": "provided", "clientId": args.client_id},
                     "baseScopes": ["telegram.read"], "defaultScopes": ["telegram.read"]}}}}}}
    output = Path(args.output).absolute()
    if output.exists():
        raise ConfigError("Архив уже существует; выберите новый --output.")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "plugin.json", "plugin.json")
        for directory in ("skills", "docs"):
            for path in sorted((root / directory).rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(root))
        archive.writestr("mcp.json", json.dumps(mcp, ensure_ascii=False, indent=2) + "\n")
    print("Приватный remote-пакет создан: " + str(output))


def run(args):
    config = load(args.config, require_telegram=True)
    from .tdlib import TdClient
    with_client = TdClient(config)
    try:
        if args.command == "login":
            with_client.login()
            print("Вход выполнен. Коды и пароль не сохранены в конфигурации.")
            return
        from .server import create_server, run_http, run_stdio
        server = create_server(LivePolicyTools(with_client, args.config, stdio=args.command == "stdio"))
        if args.command == "stdio":
            import anyio
            anyio.run(run_stdio, server)
        else:
            from .auth import Auth
            auth = Auth(config)
            def stop(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)
            print("MCP-сервер запущен на loopback; используйте HTTPS reverse proxy.", file=sys.stderr)
            try:
                run_http(config, server, auth)
            except KeyboardInterrupt:
                pass
            finally:
                if hasattr(auth, "close"):
                    auth.close()
    finally:
        with_client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Unofficial Telegram Reader — локальные команды владельца")
    parser.add_argument("--config", default=os.environ.get("TG_READER_CONFIG", "/etc/telegram-reader/config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init", help="Создать конфигурацию и случайные ключи, без подключения")
    create.add_argument("--public-url", required=True)
    create.add_argument("--state-directory", default="/var/lib/telegram-reader")
    create.add_argument("--tdjson", default="/usr/local/lib/libtdjson.so")
    for command in ("serve", "stdio", "login", "check-config", "access-selected", "revoke-all"):
        commands.add_parser(command)
    for command in ("grant-chat", "grant-all", "grant-channels"):
        grant = commands.add_parser(command)
        if command == "grant-chat":
            grant.add_argument("--chat-id", required=True)
        grant.add_argument("--not-before", required=True)
        grant.add_argument("--expires-at", required=True)
        grant.add_argument("--consent-note", required=True, help="Основание и границы доступа; не подтверждает разрешение Telegram автоматически")
    revoke = commands.add_parser("revoke-chat")
    revoke.add_argument("--chat-id", required=True)
    client = commands.add_parser("add-oauth-client")
    client.add_argument("--client-id", required=True)
    client.add_argument("--redirect-uri", action="append", required=True)
    client.add_argument("--confidential", action="store_true")
    pack = commands.add_parser("package-remote")
    pack.add_argument("--client-id", required=True)
    pack.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init(args)
        elif args.command in ("serve", "stdio", "login"):
            run(args)
        elif args.command == "check-config":
            load(args.config)
            print("Конфигурация корректна. Сеть, TDLib и аккаунт этой проверкой не проверяются.")
        elif args.command == "add-oauth-client":
            oauth_client(args)
        elif args.command == "package-remote":
            package(args)
        else:
            policy(args)
        return 0
    except (ConfigError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # TDLib can include credentials in an upstream diagnostic; do not echo it.
        print("Операция не выполнена. Проверьте конфигурацию, TDLib и локальную авторизацию.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
