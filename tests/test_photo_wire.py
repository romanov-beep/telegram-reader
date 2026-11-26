"""Synthetic photos through the actual SDK stdio wire; no Telegram connection."""
import base64
from pathlib import Path
import sys
import unittest

from mcp import Client, StdioServerParameters
from mcp.types import EmbeddedResource, ImageContent

import test_tools as fixtures

PHOTO_STDIO_SCRIPT = r"""
import sys
sys.path.insert(0, "tests")
import anyio
from test_tools import PhotoToolsTest
from telegram_reader.server import create_server, run_stdio
fixture = PhotoToolsTest()
fixture.setUp()
fixture.medium["photo"] = fixture.downloaded(47)
fixture.large["photo"] = fixture.downloaded(92)
anyio.run(run_stdio, create_server(fixture.tools))
"""


class PhotoWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_and_original_preserve_types_and_jpeg_over_stdio(self):
        root = Path(__file__).resolve().parent.parent
        parameters = StdioServerParameters(command=sys.executable,
                                           args=["-c", PHOTO_STDIO_SCRIPT], cwd=str(root))
        for mode in ("legacy", "2026-07-28"):
            async with Client(parameters, mode=mode) as session:
                for variant, expected_type in (("original", EmbeddedResource), ("preview", ImageContent)):
                    with self.subTest(mode=mode, variant=variant):
                        result = await session.call_tool("telegram_get_photo", {
                            "chat_id": fixtures.CHAT, "message_id": "123", "variant": variant,
                        })
                        self.assertFalse(result.is_error)
                        block = result.content[1]
                        self.assertIsInstance(block, expected_type)
                        payload = block.data if variant == "preview" else block.resource.blob
                        self.assertEqual(base64.b64decode(payload),
                                         base64.b64decode(fixtures.PhotoToolsTest.JPEG))
                        self.assertEqual(result.structured_content["width"],
                                         640 if variant == "preview" else 1280)
                        self.assertEqual(result.structured_content["size_bytes"], fixtures.PhotoToolsTest.FILE_BYTES)
                catalog = await session.list_tools()
                self.assertIn("telegram_get_photo", [tool.name for tool in catalog.tools])
                denied = await session.call_tool("telegram_get_photo", {
                    "chat_id": "1", "message_id": "123",
                })
                self.assertTrue(denied.is_error)
                self.assertEqual(denied.structured_content["error"], "chat_not_authorized")
                self.assertTrue(all(block.type == "text" for block in denied.content))

    async def test_chat_widget_exports_only_through_authorized_photo_tool(self):
        root = Path(__file__).resolve().parent.parent
        parameters = StdioServerParameters(command=sys.executable,
                                           args=["-c", PHOTO_STDIO_SCRIPT], cwd=str(root))
        async with Client(parameters, mode="legacy") as session:
            catalog = await session.list_tools()
            widget = next(tool for tool in catalog.tools if tool.name == "telegram_show_photo")
            uri = widget.meta["ui"]["resourceUri"]
            resources = await session.list_resources()
            self.assertIn(uri, [str(item.uri) for item in resources.resources])
            resource = (await session.read_resource(uri)).contents[0]
            self.assertEqual(resource.mime_type, "text/html;profile=mcp-app")
            self.assertIn("ui/notifications/tool-result", resource.text)
            for variant in ("preview", "original"):
                result = await session.call_tool(widget.name, {
                    "chat_id": fixtures.CHAT, "message_id": "123", "variant": variant,
                })
                self.assertFalse(result.is_error)
                self.assertTrue(all(item.type == "text" for item in result.content))
                self.assertEqual(result.meta["telegramPhotoJpeg"], fixtures.PhotoToolsTest.JPEG)
                self.assertEqual(result.structured_content["variant"], variant)
            denied = await session.call_tool(widget.name, {
                "chat_id": "1", "message_id": "123", "variant": "original",
            })
            self.assertTrue(denied.is_error)
            self.assertNotIn("telegramPhotoJpeg", denied.meta or {})
