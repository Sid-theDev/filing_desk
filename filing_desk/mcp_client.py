"""Call the Filing Desk MCP server from Python, the way any MCP client would.

    with MCPTools() as server:
        server.list_tool_names()      # ['get_fact', 'list_filings', 'read_section', 'search_filing']
        server.run_tool("get_fact", {"concept": "CommercialPaper", "period": "FY2024"})

It starts serve_mcp.py as a subprocess and talks to it over stdio. `ask.py --via-mcp` runs the agent
through it, so a question is answered end to end through the protocol while the server logs every
call to runs/mcp_calls.jsonl.

The MCP client library is async and the agent loop is not. A private event loop on a background
thread bridges the two, so callers just use run_tool().
"""

import asyncio
import json
import sys
import threading
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ROOT

TIMEOUT_SECONDS = 60


class MCPTools:
    """A live MCP session to the Filing Desk server with a plain, synchronous run_tool()."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.ready = threading.Event()
        self.session = None
        self.stop = None
        self.main = None

    def __enter__(self):
        self.thread.start()
        self.main = asyncio.run_coroutine_threadsafe(self.hold_session(), self.loop)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while not self.ready.wait(0.1):
            if self.main.done():
                self.main.result()  # raises whatever stopped the server from starting
                raise RuntimeError("The MCP session ended during start-up.")
            if time.monotonic() > deadline:
                raise TimeoutError("The MCP server did not start in time.")
        return self

    def __exit__(self, *exc_info):
        try:
            if self.stop is not None:
                self.loop.call_soon_threadsafe(self.stop.set)
            self.main.result(timeout=TIMEOUT_SECONDS)
        except Exception:  # shutting down is best effort; the subprocess dies with us anyway
            pass
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)

    async def hold_session(self):
        """Open the server process and session, then keep them open until stop is set."""
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "serve_mcp.py")], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.session = session
                self.stop = asyncio.Event()
                self.ready.set()
                await self.stop.wait()

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(TIMEOUT_SECONDS)

    def list_tool_names(self) -> list[str]:
        return sorted(tool.name for tool in self.call(self.session.list_tools()).tools)

    def run_tool(self, name: str, arguments: dict) -> dict:
        """Same contract as tools.run_tool: always a dict, errors as {'error': ...}."""
        result = self.call(self.session.call_tool(name, arguments))
        text = "".join(getattr(item, "text", "") for item in (getattr(result, "content", None) or []))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            payload = {"error": text or "The MCP server returned no content."}
        is_error = getattr(result, "is_error", False) or getattr(result, "isError", False)
        if is_error and "error" not in payload:
            payload = {"error": text}
        return payload
