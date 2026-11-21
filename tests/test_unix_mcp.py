import asyncio
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession
from mcp.server.stdio import stdio_server

from nonfluence.mcp_server import make_server
from nonfluence.store import Store
from nonfluence.unix_mcp import SocketOutput, serve_unix


class UnixMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_clients_share_a_worker_and_disconnect_independently(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            store = Store(folder / "pages.sqlite", "personal", write=True)
            socket = folder / "mcp.sock"
            service = asyncio.create_task(serve_unix(make_server(store, None), socket))
            try:
                async with asyncio.timeout(10):
                    while not socket.exists():
                        if service.done():
                            service.result()
                        await asyncio.sleep(0.01)
                    self.assertEqual(stat.S_IMODE(socket.stat().st_mode), 0o600)

                    async def call(operation, arguments):
                        reader, writer = await asyncio.open_unix_connection(socket)
                        async with stdio_server(stdin=reader, stdout=SocketOutput(writer)) as streams:
                            try:
                                async with ClientSession(*streams) as client:
                                    await client.initialize()
                                    result = await client.call_tool(operation, arguments)
                                    self.assertFalse(result.isError, result.content)
                                    return result.structuredContent
                            finally:
                                writer.close()
                                await writer.wait_closed()

                    pages = await asyncio.gather(
                        call("create_page", {"title": "One", "markdown": "First"}),
                        call("create_page", {"title": "Two", "markdown": "Second"}),
                    )
                    read = await call("read_page", {"id": pages[1]["id"]})
                    self.assertEqual(read["markdown"], "Second")
            finally:
                service.cancel()
                await asyncio.gather(service, return_exceptions=True)

    async def test_refuses_a_public_socket_directory(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            folder.chmod(0o755)
            with self.assertRaises(PermissionError):
                await serve_unix(None, folder / "mcp.sock")
