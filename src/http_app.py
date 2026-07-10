"""
http_app.py — schlanker, stateless WSGI-Adapter fuer den MCP-Server.

Warum kein FastMCP-eigener HTTP-Transport: dessen Streamable-HTTP-App ist ASGI
und braucht einen laufenden Lifespan/Session-Manager. Auf dem WSGI-basierten
Shared-Host (LiteSpeed/Passenger) laesst sich das nicht zuverlaessig starten.
Da unsere Tools zustandslos sind (jeder Aufruf ist unabhaengig), genuegt ein
minimaler WSGI-Adapter, der die JSON-RPC-Methoden von MCP direkt bedient:
  - initialize        -> Server-Info + Capabilities
  - tools/list        -> Werkzeugliste (aus FastMCP)
  - tools/call        -> Werkzeug ausfuehren
  - ping / notifications

Das ist protokollkonform fuer Streamable-HTTP-Clients im JSON-Response-Modus und
haengt nicht an FastMCP-Interna ausser den stabilen list_tools()/call_tool().
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path

from server import mcp  # FastMCP-Instanz mit allen registrierten Tools

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "berner-recht", "version": "1.0.0"}

# Landing-Page + Assets (GET), MCP-Endpunkt (POST /mcp) laufen auf derselben App.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _run(coro):
    return asyncio.run(coro)


def _dispatch(method: str, params: dict | None, req_id):
    params = params or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        tools = _run(mcp.list_tools())
        return {"tools": [t.model_dump(exclude_none=True, by_alias=True) for t in tools]}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = _run(mcp.call_tool(name, args))
        except Exception as exc:  # Tool-Fehler als isError-Ergebnis, nicht als Protokollfehler
            return {"content": [{"type": "text", "text": f"Fehler: {exc}"}], "isError": True}
        content, structured = result if isinstance(result, tuple) else (result, None)
        blocks = [c.model_dump(exclude_none=True, by_alias=True) for c in content]
        out = {"content": blocks, "isError": False}
        if isinstance(structured, dict):
            out["structuredContent"] = structured
        return out
    raise _MethodNotFound(method)


class _MethodNotFound(Exception):
    pass


def _json(start_response, obj, status="200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def _serve_static(path_info: str, start_response):
    """Landing-Page (GET /) und Assets (GET /assets/...) ausliefern."""
    rel = (path_info or "/").lstrip("/") or "index.html"
    if rel in ("", "mcp", "health"):
        rel = "index.html"
    target = (STATIC_DIR / rel).resolve()
    # Path-Traversal verhindern; unbekannte Pfade -> Landing-Page (SPA-artig)
    if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
        target = STATIC_DIR / "index.html"
    data = target.read_bytes()
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/"):
        ctype += "; charset=utf-8"
    headers = [("Content-Type", ctype), ("Content-Length", str(len(data)))]
    if rel.startswith("assets/"):
        headers.append(("Cache-Control", "public, max-age=86400"))
    start_response("200 OK", headers)
    return [data]


def application(environ, start_response):
    """WSGI-Einstieg (Passenger ruft dieses Callable auf)."""
    method_http = environ.get("REQUEST_METHOD", "GET")

    # GET -> statische Landing-Page / Assets
    if method_http == "GET":
        return _serve_static(environ.get("PATH_INFO", "/"), start_response)

    if method_http != "POST":
        return _json(start_response, {"error": "method not allowed"}, "405 Method Not Allowed")

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
    except Exception:
        return _json(start_response, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}})

    req_id = payload.get("id")
    rpc_method = payload.get("method")

    # Notifications (ohne id, z. B. notifications/initialized) -> 202, kein Body
    if req_id is None and isinstance(rpc_method, str) and rpc_method.startswith("notifications/"):
        start_response("202 Accepted", [("Content-Type", "application/json")])
        return [b""]

    try:
        result = _dispatch(rpc_method, payload.get("params"), req_id)
    except _MethodNotFound as exc:
        return _json(start_response, {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32601, "message": f"Method not found: {exc}"}})
    except Exception as exc:
        return _json(start_response, {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32603, "message": str(exc)}})

    return _json(start_response, {"jsonrpc": "2.0", "id": req_id, "result": result})
