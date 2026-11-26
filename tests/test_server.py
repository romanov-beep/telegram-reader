"""SDK transport and HTTP security tests, also runnable on native Windows."""
import io
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from telegram_reader import __version__
from telegram_reader.__main__ import LivePolicyTools
from telegram_reader.auth import AuthError
from telegram_reader.auth import Auth as RealAuth
from telegram_reader.config import ConfigError
from telegram_reader.server import BoundedStdin, MAX_BODY, MAX_CONCURRENT, create_http_app, create_server
from telegram_reader.tools import ReadTools


class Tools:
    def __init__(self):
        self.reader = ReadTools(object(), {})

    def descriptors(self):
        return self.reader.descriptors()

    def call(self, name, args):
        if args.get("fail"):
            raise RuntimeError("PRIVATE session value")
        return self.reader.call(name, args)


class Auth:
    def verify_bearer(self, token):
        return token == "good"

    def metadata(self):
        return {"issuer": "https://reader.example"}

    def protected_metadata(self):
        return {"resource": "https://reader.example/mcp"}

    def authorize_start(self, params):
        if params.get("client_id") != "client":
            raise AuthError("invalid_request", "Unknown client")
        return {"ticket": "ticket", "cookie": "csrf", "client_id": '<script>alert(1)</script>'}

    def authorize_finish(self, ticket, cookie, owner_secret):
        if (ticket, cookie, owner_secret) != ("ticket", "csrf", "owner"):
            raise AuthError("access_denied", "Denied", 403)
        return "https://client.example/cb?code=example"

    def grant(self, params, basic_header=None):
        if params != {"grant_type": "authorization_code"} or basic_header != "Basic example":
            raise AuthError("invalid_client", "Denied", 401)
        return {"access_token": "good", "token_type": "Bearer"}

    def revoke(self, params, basic_header=None):
        return {}


CONFIG = {"public_url": "https://reader.example", "port": 8080,
          "allowed_origins": ["https://chatgpt.com"]}
LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
CLIENT_INFO = {"name": "test", "version": "1"}
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
    "io.modelcontextprotocol/clientCapabilities": {},
}
STDIO_SCRIPT = """import anyio
import telegram_reader.__main__ as cli
from telegram_reader.server import create_server, run_stdio
# Replace only file loading: exercise the real stdio catalog/profile adapter.
cli.load = lambda path: {}
anyio.run(run_stdio, create_server(cli.LivePolicyTools(object(), 'unused', stdio=True)))
"""


def app():
    return create_http_app(CONFIG, create_server(Tools()), Auth())


def exchange_stdio(phases):
    """Keep the subprocess open until each expected response has been read."""
    root = Path(__file__).resolve().parent.parent
    with subprocess.Popen([sys.executable, "-c", STDIO_SCRIPT], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=root) as process:
        timer = threading.Timer(10, process.kill)
        timer.start()
        output = []
        try:
            for records, reply_id in phases:
                for record in records:
                    process.stdin.write((record if isinstance(record, str) else json.dumps(record)) + "\n")
                process.stdin.flush()
                while line := process.stdout.readline():
                    output.append(line)
                    if json.loads(line).get("id") == reply_id:
                        break
            process.stdin.close()
            process.wait(timeout=5)
            return process.returncode, "".join(output), process.stderr.read()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app(), base_url="https://reader.example"))

    def rpc(self, method, params=None, headers=None, notification=False):
        value = {"jsonrpc": "2.0", "method": method}
        if not notification:
            value["id"] = 1
        if params is not None:
            value["params"] = params
        return self.client.post("/mcp", json=value, headers={
            "Authorization": "Bearer good", "Accept": "application/json, text/event-stream", **(headers or {})})

    def test_initialize_notification_and_sdk_versions(self):
        result = self.rpc("initialize", {"protocolVersion": LEGACY_VERSION, "capabilities": {},
                                         "clientInfo": CLIENT_INFO})
        self.assertEqual(result.status_code, 200)
        initialized = result.json()["result"]
        self.assertEqual(initialized["protocolVersion"], LEGACY_VERSION)
        self.assertEqual(initialized["serverInfo"]["name"], "unofficial-telegram-reader")
        self.assertEqual(initialized["serverInfo"]["version"], __version__)
        self.assert_read_instructions(initialized["instructions"])
        self.assertNotIn("mcp-session-id", result.headers)
        self.assertEqual(result.headers["cache-control"], "no-store")
        result = self.rpc("notifications/initialized", notification=True)
        self.assertEqual((result.status_code, result.content), (202, b""))
        self.assertEqual(self.rpc("ping").status_code, 200)
        self.assertEqual(self.rpc("ping", headers={"MCP-Protocol-Version": "bad"}).status_code, 400)
        self.assertEqual(self.rpc("ping", headers={"Accept": "application/json"}).status_code, 200)
        self.assertEqual(self.rpc("ping", headers={"Accept": "text/plain"}).status_code, 406)

    def assert_read_instructions(self, instructions):
        self.assertIn("Use telegram_status for diagnosis", instructions)
        self.assertIn("without a prior telegram_get_message", instructions)
        self.assertIn("use variant=original on the first call", instructions)

    def test_modern_calls_and_discovery_without_initialize(self):
        headers = {"MCP-Protocol-Version": MODERN_VERSION}
        for name, arguments in (("telegram_status", {}), ("telegram_read_history", {"chat_id": "1"})):
            with self.subTest(name=name):
                response = self.rpc("tools/call", {"_meta": MODERN_META, "name": name, "arguments": arguments},
                                    headers={**headers, "Mcp-Method": "tools/call", "Mcp-Name": name})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("mcp-session-id", response.headers)
                result = response.json()["result"]
                if name == "telegram_status":
                    self.assertTrue(result["structuredContent"]["read_only"])
                else:
                    self.assertTrue(result["isError"])
                    self.assertEqual(result["structuredContent"]["error"], "chat_not_authorized")
        response = self.rpc("server/discover", {"_meta": MODERN_META},
                            headers={**headers, "Mcp-Method": "server/discover"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("mcp-session-id", response.headers)
        discovered = response.json()["result"]
        self.assertIn(MODERN_VERSION, discovered["supportedVersions"])
        self.assertEqual(discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
                         "unofficial-telegram-reader")
        self.assert_read_instructions(discovered["instructions"])
        catalog = self.rpc("tools/list", {"_meta": MODERN_META},
                           headers={**headers, "Mcp-Method": "tools/list"}).json()["result"]
        self.assertEqual(len(catalog["tools"]), 8)

    def test_read_catalog_result_and_safe_failures(self):
        catalog = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual(len(catalog), 8)
        for item, expected in zip(catalog, Tools().descriptors()):
            self.assertEqual(item["name"], expected["name"])
            self.assertEqual(item["inputSchema"], expected["inputSchema"])
            self.assertEqual(item["securitySchemes"], expected["securitySchemes"])
            self.assertEqual(item["_meta"], expected["_meta"])
            self.assertTrue(item["annotations"]["readOnlyHint"])
            for key, value in expected["annotations"].items():
                self.assertEqual(item["annotations"][key], value)
        result = self.rpc("tools/call", {"name": "telegram_status", "arguments": {}}).json()["result"]
        self.assertTrue(result["structuredContent"]["read_only"])
        result = self.rpc("tools/call", {"name": "telegram_status", "arguments": {"fail": "PRIVATE"}})
        self.assertTrue(result.json()["result"]["isError"])
        self.assertNotIn("PRIVATE", result.text)
        for method in ("resources/list", "prompts/list"):
            self.assertEqual(self.rpc(method).json()["result"][method.split('/')[0]], [])
        result = self.rpc("tools/call", {"name": "telegram_send_message", "arguments": {}})
        self.assertTrue(result.json()["result"]["isError"])

    def test_auth_host_origin_and_metadata(self):
        result = self.client.post("/mcp", json={})
        self.assertEqual(result.status_code, 401)
        self.assertIn("/.well-known/oauth-protected-resource/mcp", result.headers["www-authenticate"])
        for path in ("/healthz", "/.well-known/oauth-protected-resource/mcp",
                     "/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/healthz", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://evil.example"}).status_code, 403)
        for method in ("GET", "DELETE"):
            result = self.client.request(method, "/mcp", headers={"Authorization": "Bearer good"})
            self.assertEqual(result.status_code, 405)
            self.assertEqual(result.headers["allow"], "POST")
        result = self.client.options("/mcp", headers={"Origin": "https://chatgpt.com"})
        self.assertEqual(result.headers["access-control-allow-origin"], "https://chatgpt.com")
        with self.assertRaises(ValueError):
            create_http_app({**CONFIG, "bind_host": "0.0.0.0"}, create_server(Tools()), Auth())

    def test_body_limits_duplicates_and_private_malformed_errors(self):
        headers = {"Authorization": "Bearer good", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        for content in ('{"x":1,"x":2}', '{"x":NaN}', '{bad}',
                        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"telegram_status","arguments":{"x":"\\ud800 PRIVATE"}}}',
                        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"telegram_status","arguments":{"x":1e999}}}',
                        '{"jsonrpc":"2.0","id":true,"method":"ping"}',
                        '{"jsonrpc":"2.0","id":null,"method":"ping"}',
                        '{"jsonrpc":"2.0","id":1.5,"method":"ping"}',
                        '{"jsonrpc":"2.0","id":1,"method":"ping","result":{"secret":"PRIVATE"}}',
                        '{"jsonrpc":"PRIVATE","id":1,"method":"ping"}',
                        '{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":{"secret":"PRIVATE"}}}',
                        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"PRIVATE","arguments":[]}}'):
            captured = io.StringIO()
            handler = logging.StreamHandler(captured)
            logging.getLogger().addHandler(handler)
            try:
                result = self.client.post("/mcp", content=content, headers=headers)
            finally:
                logging.getLogger().removeHandler(handler)
            self.assertEqual(result.status_code, 400)
            self.assertNotIn("PRIVATE", result.text + captured.getvalue())
        self.assertEqual(self.client.post("/mcp", content=b'x' * (MAX_BODY + 1), headers=headers).status_code, 413)
        for length in ("-1", "abc"):
            self.assertEqual(self.client.post("/mcp", content=b"", headers={**headers, "Content-Length": length}).status_code, 400)
        request = self.client.build_request("POST", "/mcp", content=b"", headers=headers)
        del request.headers["content-length"]
        self.assertEqual(self.client.send(request).status_code, 411)
        for extra, expected in (([("Content-Length", "0"), ("Content-Length", "0")], 400),
                                ([("Transfer-Encoding", "chunked")], 400),
                                ([("Authorization", "Bearer good"), ("Authorization", "Bearer good")], 400)):
            base = [(key, value) for key, value in headers.items() if key.lower() not in {k.lower() for k, _ in extra}]
            self.assertEqual(self.client.post("/mcp", content=b"", headers=base + extra).status_code, expected)

    def test_oauth_forms_csrf_and_escaping(self):
        result = self.client.get("/authorize?client_id=client")
        self.assertEqual(result.status_code, 200)
        self.assertIn("Secure; HttpOnly; SameSite=Lax; Path=/", result.headers["set-cookie"])
        self.assertNotIn("form-action", result.headers["content-security-policy"])
        self.assertNotIn("<script>", result.text)
        self.assertIn("&lt;script&gt;", result.text)
        self.assertEqual(self.client.get("/authorize?client_id=client&client_id=client").status_code, 400)
        form = urlencode({"ticket": "ticket", "owner_secret": "owner"})
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": "__Host-mcp-consent=csrf"}
        self.assertEqual(self.client.post("/authorize", content=form, headers=headers).status_code, 403)
        headers["Origin"] = "https://reader.example"
        result = self.client.post("/authorize", content=form, headers=headers, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"], "https://client.example/cb?code=example")
        headers["Authorization"] = "Basic example"
        self.assertEqual(self.client.post("/token", content="grant_type=authorization_code", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/token", content="grant_type=a&grant_type=b", headers=headers).status_code, 400)
        self.assertEqual(self.client.post("/revoke", content="token=good", headers=headers).status_code, 200)

    @unittest.skipUnless(os.name == "posix", "OAuth storage deliberately requires POSIX file protections")
    def test_real_oauth_http_pkce_refresh_and_revoke(self):
        with tempfile.TemporaryDirectory() as directory:
            digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
            auth = RealAuth({**CONFIG, "state_directory": directory,
                             "owner_secret_sha256": digest("owner"),
                             "oauth_clients": [{"client_id": "client", "redirect_uris": ["https://client.example/cb"]}]})
            try:
                with TestClient(create_http_app(CONFIG, create_server(Tools()), auth),
                                base_url="https://reader.example") as client:
                    verifier = "v" * 43
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                    resource = "https://reader.example/mcp"
                    result = client.get("/authorize", params={"client_id": "client", "redirect_uri": "https://client.example/cb",
                                        "resource": resource, "scope": "telegram.read", "response_type": "code",
                                        "code_challenge_method": "S256", "code_challenge": challenge, "state": "opaque"})
                    self.assertEqual(result.status_code, 200)
                    import re
                    ticket = re.search(r'name="ticket" value="([^"]+)"', result.text).group(1)
                    result = client.post("/authorize", data={"ticket": ticket, "owner_secret": "owner"},
                                         headers={"Origin": "https://reader.example"}, follow_redirects=False)
                    self.assertEqual(result.status_code, 303)
                    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
                    token = client.post("/token", data={"grant_type": "authorization_code", "client_id": "client",
                                        "code": code, "code_verifier": verifier, "resource": resource,
                                        "redirect_uri": "https://client.example/cb"}).json()
                    def status(access):
                        return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                           headers={"Authorization": "Bearer " + access,
                                                    "Accept": "application/json, text/event-stream"}).status_code
                    self.assertEqual(status(token["access_token"]), 200)
                    rotated = client.post("/token", data={"grant_type": "refresh_token", "client_id": "client",
                                           "refresh_token": token["refresh_token"], "resource": resource}).json()
                    self.assertEqual(status(rotated["access_token"]), 200)
                    self.assertEqual(client.post("/revoke", data={"client_id": "client", "token": rotated["refresh_token"]}).status_code, 200)
                    self.assertEqual(status(rotated["access_token"]), 401)
            finally:
                auth.db.close()


class SDKClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_and_repeated_calls_load_policy_once_without_rebuilding_catalog(self):
        tools = LivePolicyTools(object(), "unused")
        with patch("telegram_reader.__main__.load", side_effect=[
            {"chat_access": "all"},
            {"chat_access": "selected"},
            ConfigError("PRIVATE config detail"),
        ]) as load_policy, patch.object(ReadTools, "descriptors", wraps=ReadTools.descriptors) as descriptors:
            async with Client(create_server(tools), mode=MODERN_VERSION) as session:
                for index, mode in enumerate(("all", "selected"), start=1):
                    result = await session.call_tool("telegram_status")
                    self.assertEqual(result.structured_content["access_mode"], mode)
                    self.assertEqual(load_policy.call_count, index)
                result = await session.call_tool("telegram_send_message")
                self.assertTrue(result.is_error)
                self.assertEqual(result.content[0].text, "Tool request failed")
                self.assertEqual(load_policy.call_count, 2)
                result = await session.call_tool("telegram_read_history", {"chat_id": "1"})
                self.assertTrue(result.is_error)
                self.assertNotIn("PRIVATE", result.content[0].text)
                self.assertEqual(load_policy.call_count, 3)
                self.assertEqual(len((await session.list_tools()).tools), 8)
                self.assertEqual(load_policy.call_count, 3)
        descriptors.assert_called_once_with(stdio=False)

    async def check_session(self, session, *, stdio=False):
        self.assertEqual(session.server_info.name, "unofficial-telegram-reader")
        catalog = await session.list_tools()
        self.assertEqual(len(catalog.tools), len(Tools().descriptors()))
        schemes = [{"type": "noauth"}] if stdio else [{"type": "oauth2", "scopes": ["telegram.read"]}]
        for tool in catalog.tools:
            descriptor = tool.model_dump(by_alias=True)
            self.assertEqual(descriptor["_meta"]["securitySchemes"], schemes)
        result = await session.call_tool("telegram_status", {})
        self.assertTrue(result.structured_content["read_only"])
        result = await session.call_tool("telegram_read_history", {"chat_id": "1"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "chat_not_authorized")

    async def test_official_client_http_roundtrip(self):
        application = app()
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application),
                                          headers={"Authorization": "Bearer good"}) as client:
                for mode, version in (("auto", MODERN_VERSION), ("legacy", LEGACY_VERSION)):
                    transport = streamable_http_client("https://reader.example/mcp", http_client=client)
                    async with Client(transport, mode=mode) as session:
                        self.assertEqual(session.protocol_version, version)
                        await self.check_session(session)

    async def test_official_client_stdio_roundtrip(self):
        root = Path(__file__).resolve().parent.parent
        parameters = StdioServerParameters(command=sys.executable, args=["-c", STDIO_SCRIPT], cwd=str(root))
        for mode, version in (("auto", MODERN_VERSION), ("legacy", LEGACY_VERSION)):
            async with Client(parameters, mode=mode) as session:
                self.assertEqual(session.protocol_version, version)
                await self.check_session(session, stdio=True)
        async with Client(parameters, mode=MODERN_VERSION) as session:
            status = await session.call_tool("telegram_status")
            self.assertTrue(status.structured_content["read_only"])

    async def test_worker_admission_rejects_excess_and_recovers(self):
        # Use the official SDK memory transport to test admission independently
        # of the HTTP middleware's separate active-request limit.
        gate = threading.Event()
        lock = threading.Lock()
        entered = 0
        class SlowTools(Tools):
            def call(self, name, args):
                nonlocal entered
                with lock:
                    entered += 1
                gate.wait(10)
                return super().call(name, args)
        async with Client(create_server(SlowTools()), mode=MODERN_VERSION) as session:
            await session.list_tools()
            results = []
            async def request():
                results.append(await session.call_tool("telegram_status", {}))
            async with anyio.create_task_group() as group:
                try:
                    for _ in range(MAX_CONCURRENT):
                        group.start_soon(request)
                    with anyio.fail_after(5):
                        while entered != MAX_CONCURRENT:
                            await anyio.sleep(0.01)
                    with anyio.fail_after(2):
                        rejected = await session.call_tool("telegram_status", {})
                    self.assertTrue(rejected.is_error)
                    self.assertEqual(entered, MAX_CONCURRENT)
                finally:
                    gate.set()
            self.assertTrue(all(not result.is_error for result in results))
            self.assertFalse((await session.call_tool("telegram_status", {})).is_error)

    async def test_http_concurrency_rejects_excess_and_recovers(self):
        gate = threading.Event()
        lock = threading.Lock()
        entered = 0
        class SlowTools(Tools):
            def call(self, name, args):
                nonlocal entered
                with lock:
                    entered += 1
                gate.wait(10)
                return super().call(name, args)
        application = create_http_app(CONFIG, create_server(SlowTools()), Auth())
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application),
                                          base_url="https://reader.example") as client:
                statuses = []
                async def request():
                    result = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": "telegram_status", "arguments": {}}},
                                               headers={"Authorization": "Bearer good", "Accept": "application/json"})
                    statuses.append(result.status_code)
                async with anyio.create_task_group() as group:
                    try:
                        for _ in range(MAX_CONCURRENT):
                            group.start_soon(request)
                        with anyio.fail_after(5):
                            while entered != MAX_CONCURRENT:
                                await anyio.sleep(0.01)
                        self.assertEqual((await client.get("/healthz")).status_code, 503)
                    finally:
                        gate.set()
                self.assertEqual(statuses, [200] * MAX_CONCURRENT)
                self.assertEqual((await client.get("/healthz")).status_code, 200)

    async def test_stdio_lifecycle_errors_do_not_log_private_notification(self):
        progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": 1, "progress": 0, "message": "PRIVATE"}}
        invalid_call = {"jsonrpc": "2.0", "id": "invalid", "method": "tools/call",
                        "params": {"name": {"secret": "PRIVATE"}}}
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": LEGACY_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}}
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        phases = [
            ([progress, invalid_call, "x" * (MAX_BODY + 10), initialize], 1),
            ([initialized, list_tools], 2),
        ]
        code, stdout, stderr = await anyio.to_thread.run_sync(exchange_stdio, phases)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("PRIVATE", stdout + stderr)
        responses = {row.get("id"): row for row in map(json.loads, stdout.splitlines())}
        self.assertEqual(responses[1]["result"]["protocolVersion"], LEGACY_VERSION)
        self.assertEqual(len(responses[2]["result"]["tools"]), 8)

    async def test_modern_stdio_accepts_direct_calls_and_recovers_after_invalid_input(self):
        first_call = {"jsonrpc": "2.0", "id": "first-call", "method": "tools/call",
                      "params": {"_meta": MODERN_META, "name": "telegram_status", "arguments": {}}}
        invalid_call = {**first_call, "id": "invalid",
                        "params": {"_meta": MODERN_META, "name": {"secret": "PRIVATE"}}}
        discover = {"jsonrpc": "2.0", "id": "discover", "method": "server/discover",
                    "params": {"_meta": MODERN_META, "untrusted": "PRIVATE"}}
        phases = [
            ([first_call], "first-call"),
            ([invalid_call, "x" * (MAX_BODY + 10), discover], "discover"),
        ]
        code, stdout, stderr = await anyio.to_thread.run_sync(exchange_stdio, phases)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("PRIVATE", stdout + stderr)
        responses = {row.get("id"): row for row in map(json.loads, stdout.splitlines())}
        self.assertTrue(responses["first-call"]["result"]["structuredContent"]["read_only"])
        self.assertIn(MODERN_VERSION, responses["discover"]["result"]["supportedVersions"])

    async def test_shared_stdio_interleaves_legacy_and_modern_requests_in_either_order(self):
        initialize = {"jsonrpc": "2.0", "id": "initialize", "method": "initialize",
                      "params": {"protocolVersion": LEGACY_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}}
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        discover = {"jsonrpc": "2.0", "id": "discover", "method": "server/discover",
                    "params": {"_meta": MODERN_META}}
        invalid_call = {"jsonrpc": "2.0", "id": "invalid", "method": "tools/call",
                        "params": {"_meta": MODERN_META, "name": {"secret": "PRIVATE"}}}
        progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": 1, "progress": 0, "message": "PRIVATE"}}

        def tool_request(era, request_id, name="telegram_status", arguments=None):
            params = {"name": name, "arguments": arguments or {}}
            if era == "modern":
                params["_meta"] = MODERN_META
            return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}

        initial_phases = {
            "legacy": [([initialize], "initialize"),
                       ([initialized, tool_request("legacy", "legacy-first")], "legacy-first")],
            "modern": [([discover], "discover"), ([tool_request("modern", "modern-first")], "modern-first")],
        }
        for first, second in (("legacy", "modern"), ("modern", "legacy")):
            with self.subTest(first=first):
                phases = initial_phases[first] + initial_phases[second]
                for era in (first, second):
                    repeated_id = f"{era}-repeated"
                    denied_id = f"{era}-denied"
                    phases.append(([progress, invalid_call, tool_request(era, repeated_id)], repeated_id))
                    denied = tool_request(era, denied_id, "telegram_read_history", {"chat_id": "1"})
                    phases.append(([denied], denied_id))
                code, stdout, stderr = await anyio.to_thread.run_sync(exchange_stdio, phases)
                self.assertEqual(code, 0, stderr)
                self.assertNotIn("PRIVATE", stdout)
                self.assertNotIn("PRIVATE", stderr)
                responses = {row.get("id"): row for row in map(json.loads, stdout.splitlines())}
                self.assertEqual(responses["initialize"]["result"]["protocolVersion"], LEGACY_VERSION)
                self.assertIn(MODERN_VERSION, responses["discover"]["result"]["supportedVersions"])
                for era in (first, second):
                    for call in ("first", "repeated"):
                        status = responses[f"{era}-{call}"]["result"]
                        self.assertFalse(status.get("isError", False))
                        self.assertTrue(status["structuredContent"]["read_only"])
                    denied = responses[f"{era}-denied"]["result"]
                    self.assertTrue(denied["isError"])
                    self.assertEqual(denied["structuredContent"]["error"], "chat_not_authorized")

    async def test_bounded_stdio_rejects_large_or_private_invalid_input(self):
        for raw in (b'x' * (MAX_BODY + 1), b'{"secret":NaN}\n', b'{"x":1,"x":2}\n',
                    b'\xff\n', b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n',
                    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"telegram_status","arguments":{"x":"\\ud800 PRIVATE"}}}\n',
                    b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":{"secret":"PRIVATE"}}}\n'):
            self.assertEqual(await BoundedStdin(io.BytesIO(raw)).__anext__(), "null\n")


if __name__ == "__main__":
    unittest.main()
