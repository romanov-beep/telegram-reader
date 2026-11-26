import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from telegram_reader.__main__ import LivePolicyTools, main
from telegram_reader.config import ConfigError, load, save, validate


class ConfigCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            self.assertEqual(main(["--config", str(self.config), "init", "--public-url", "https://reader.example", "--state-directory", str(self.root / "state")]), 0)

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["--config", str(self.config), *args])

    def test_init_private_deny_and_no_plaintext_owner_tokens(self):
        config = load(self.config)
        self.assertEqual(config["chat_access"], "selected")
        self.assertEqual(config["allowed_chats"], {})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        for line in self.output.getvalue().splitlines()[-2:]:
            self.assertNotIn(line.split(": ")[-1], self.config.read_text())
        before = self.config.read_bytes()
        self.assertEqual(self.run_cli("init", "--public-url", "https://reader.example"), 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_policy_enable_all_revoke_and_hot_reload(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.run_cli("grant-all", "--not-before", start, "--expires-at", end, "--consent-note", "Owner command, limited context"), 0)
        tools = LivePolicyTools(object(), self.config)
        self.assertEqual(tools.call("telegram_status", {})["structuredContent"]["access_mode"], "all")
        self.assertEqual(self.run_cli("revoke-all"), 0)
        self.assertEqual(tools.call("telegram_status", {})["structuredContent"]["access_mode"], "selected")
        self.config.write_text("invalid")
        self.assertTrue(tools.call("telegram_read_history", {"chat_id": "1"})["isError"])

    def test_validate_rejects_wide_redirect_bad_window_and_public_url(self):
        base = load(self.config)
        for key, value in (("public_url", "http://outside.example"), ("public_url", "https://a.example/"), ("chat_access", "wide"), ("allowed_origins", ["*"])):
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                validate({**base, key: value})
        with self.assertRaises(ConfigError):
            validate({**base, "chat_access": "all", "all_chats_grant": {"not_before": "2027-01-02T00:00:00Z", "expires_at": "2027-01-01T00:00:00Z", "consent_note": "owner"}})
        with self.assertRaises(ConfigError):
            validate({**base, "oauth_clients": [{"client_id": "one", "redirect_uris": ["https://*.example/cb"]}]})
        self.config.chmod(0o644)
        with self.assertRaises(ConfigError):
            load(self.config)

    def test_channels_all_and_rollback_hot_reload_preserve_other_settings(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        window = ("--not-before", start, "--expires-at", end, "--consent-note", "Owner test permission")
        self.assertEqual(self.run_cli("grant-channels", *window), 0)
        before = load(self.config)
        tools = LivePolicyTools(object(), self.config)
        self.assertEqual(tools.call("telegram_status", {})["structuredContent"]["access_mode"], "channels")

        self.assertEqual(self.run_cli("grant-all", *window), 0)
        self.assertEqual(load(self.config), {**before, "chat_access": "all"})
        self.assertEqual(tools.call("telegram_status", {})["structuredContent"]["access_mode"], "all")

        self.assertEqual(self.run_cli("grant-channels", *window), 0)
        self.assertEqual(load(self.config), before)
        self.assertEqual(tools.call("telegram_status", {})["structuredContent"]["access_mode"], "channels")

    def test_stdio_catalog_preserves_all_policy_and_existing_credentials(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.run_cli("grant-all", "--not-before", start,
                                      "--expires-at", end, "--consent-note", "Owner test"), 0)
        before = self.config.read_bytes()
        tools = LivePolicyTools(object(), self.config, stdio=True)
        for descriptor in tools.descriptors():
            self.assertEqual(descriptor["securitySchemes"], [{"type": "noauth"}])
            self.assertEqual(descriptor["_meta"]["securitySchemes"], [{"type": "noauth"}])
            self.assertTrue(descriptor["annotations"]["readOnlyHint"])
        self.assertEqual(tools.call("telegram_status", {})["structuredContent"]["access_mode"], "all")
        self.assertEqual(self.config.read_bytes(), before)

    def test_remote_package_has_documents_no_secrets(self):
        self.assertEqual(self.run_cli("add-oauth-client", "--client-id", "chatgpt", "--redirect-uri", "https://chatgpt.com/connector_platform_oauth_redirect"), 0)
        archive = self.root / "remote.zip"
        self.assertEqual(self.run_cli("package-remote", "--client-id", "chatgpt", "--output", str(archive)), 0)
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            self.assertIn("skills/telegram-read/SKILL.md", names)
            self.assertIn("docs/ARCHITECTURE.md", names)
            self.assertNotIn("config.json", names)
            data = json.loads(package.read("mcp.json"))
            self.assertEqual(data["mcpServers"]["telegram-reader"]["url"], "https://reader.example/mcp")
            secret = load(self.config)["database_encryption_key"].encode()
            self.assertTrue(all(secret not in package.read(name) for name in names))


if __name__ == "__main__":
    unittest.main()
