"""Public cabinet entrypoint; implementation lives in cabinet_server."""
from .cabinet_page import PAGE
from .cabinet_server import serve

__all__ = ["PAGE", "serve"]
