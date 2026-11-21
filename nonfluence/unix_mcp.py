"""Reuse the stdio MCP protocol on a private Unix socket."""

import asyncio
import os
import stat

from anyio import BrokenResourceError
from mcp.server.stdio import stdio_server


class SocketOutput:
    def __init__(self, writer):
        self.writer = writer

    async def write(self, text):
        self.writer.write(text.encode("utf-8"))

    async def flush(self):
        await self.writer.drain()


async def serve_unix(server, path):
    directory = path.parent.stat()
    if directory.st_uid != os.geteuid() or directory.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError("The MCP socket directory must be private to the server account.")
    # FastMCP uses this same SDK server for its built-in stdio transport.
    protocol = server._mcp_server
    initialization = protocol.create_initialization_options()

    async def handle_connection(reader, writer):
        try:
            async with stdio_server(stdin=reader, stdout=SocketOutput(writer)) as (incoming, outgoing):
                await protocol.run(incoming, outgoing, initialization)
        except* (ConnectionError, BrokenResourceError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    listener = await asyncio.start_unix_server(handle_connection, path, limit=16 * 1024 * 1024)
    try:
        path.chmod(0o600)
        async with listener:
            await listener.serve_forever()
    finally:
        path.unlink(missing_ok=True)
