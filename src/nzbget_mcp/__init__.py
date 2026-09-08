"""FastMCP server wrapping the NZBGet JSON-RPC API."""

from .server import build_server, run

__all__ = ["build_server", "run"]
__version__ = "0.1.0"
