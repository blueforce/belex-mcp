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
import sqlite3
import sys
import threading
import time
from pathlib import Path

from server import mcp  # FastMCP-Instanz mit allen registrierten Tools

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "berner-recht", "version": "1.0.0"}

# Landing-Page + Assets (GET), MCP-Endpunkt (POST /mcp) laufen auf derselben App.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# JSON-RPC-Requests sind winzig; harte Obergrenze schuetzt vor Speicher-DoS
# durch grosse POST-Bodies am oeffentlichen Endpunkt.
_MAX_BODY = 262144  # 256 KB

# Nur dieser Pfad nimmt POST/MCP-Verkehr entgegen; alles andere -> 404.
_MCP_PATHS = ("/mcp", "/mcp/")

# ---- Rate-Limit (pro IP, Sliding Window, prozessuebergreifend via SQLite) --- #
# Schuetzt den offenen /mcp-Endpunkt vor Haemmern und haelt uns fair gegenueber
# der entscheidsuche.ch-Infrastruktur. LiteSpeed faehrt mehrere lswsgi-Prozesse,
# darum liegt der Zaehler in einer kleinen SQLite (WAL) statt im Prozessspeicher.
_RATE_LIMIT = 30       # max. Anfragen ...
_RATE_WINDOW = 60      # ... pro Sekunden je IP
_RATE_DB = Path(__file__).resolve().parent.parent / "data" / "rate.sqlite"
_rate_init_lock = threading.Lock()
_rate_ready = False
# Prozess-lokaler Fallback, falls SQLite klemmt (pro Worker, aber besser als kein Limit)
_rate_fallback_lock = threading.Lock()
_rate_fallback: dict[str, list[float]] = {}


def _client_ip(environ) -> str:
    # REMOTE_ADDR ist auf LiteSpeed/Passenger die echte Client-IP. X-Forwarded-For
    # wird bewusst NICHT vertraut: Der Header ist frei setzbar und liesse sich zur
    # Rate-Limit-Umgehung missbrauchen (kein vorgelagerter Proxy validiert ihn).
    return environ.get("REMOTE_ADDR") or "?"


def _rate_conn():
    conn = sqlite3.connect(str(_RATE_DB), timeout=3)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _rate_ensure():
    global _rate_ready
    if _rate_ready:
        return
    with _rate_init_lock:
        if _rate_ready:
            return
        _RATE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = _rate_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS hits(ip TEXT, ts REAL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hits ON hits(ip, ts)")
        conn.commit()
        conn.close()
        _rate_ready = True


def _rate_ok(ip: str) -> bool:
    """False, wenn die IP im Fenster ihr Limit erreicht hat. Fail-open bei DB-Fehler."""
    try:
        _rate_ensure()
        now = time.time()
        cutoff = now - _RATE_WINDOW
        conn = _rate_conn()
        try:
            conn.execute("DELETE FROM hits WHERE ts < ?", (cutoff,))  # haelt die Tabelle klein
            n = conn.execute("SELECT COUNT(*) FROM hits WHERE ip=? AND ts>?",
                             (ip, cutoff)).fetchone()[0]
            if n >= _RATE_LIMIT:
                conn.commit()
                return False
            conn.execute("INSERT INTO hits(ip, ts) VALUES(?,?)", (ip, now))
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return _rate_ok_memory(ip)  # DB klemmt -> prozess-lokaler Fallback statt fail-open


def _rate_ok_memory(ip: str) -> bool:
    """In-Memory-Fallback (pro Worker), nur wenn die SQLite nicht verfuegbar ist."""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    with _rate_fallback_lock:
        hits = _rate_fallback.setdefault(ip, [])
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= _RATE_LIMIT:
            return False
        hits.append(now)
        if len(_rate_fallback) > 5000:
            for k in [k for k, v in _rate_fallback.items() if not v or v[-1] < cutoff]:
                del _rate_fallback[k]
        return True


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
            print(f"[belex] tool {name!r} error: {exc!r}", file=sys.stderr)  # Details nur ins Log
            return {"content": [{"type": "text", "text": "Werkzeugausfuehrung fehlgeschlagen."}],
                    "isError": True}
        content, structured = result if isinstance(result, tuple) else (result, None)
        blocks = [c.model_dump(exclude_none=True, by_alias=True) for c in content]
        out = {"content": blocks, "isError": False}
        if isinstance(structured, dict):
            out["structuredContent"] = structured
        return out
    raise _MethodNotFound(method)


class _MethodNotFound(Exception):
    pass


# CORS: der /mcp-Endpunkt ist oeffentlich und read-only -> jede Origin darf ihn
# aufrufen (z. B. die Live-Demo der Landing-Page aus einer lokalen Umgebung).
_CORS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type"),
    ("Access-Control-Max-Age", "86400"),
]


def _json(start_response, obj, status="200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ] + _CORS)
    return [body]


def _serve_static(path_info: str, start_response):
    """Landing-Page (GET /) und Assets (GET /assets/...) ausliefern."""
    rel = (path_info or "/").lstrip("/") or "index.html"
    if rel in ("", "mcp", "health"):
        rel = "index.html"
    target = (STATIC_DIR / rel).resolve()
    # Path-Traversal verhindern (echte Pfad-Relation, kein String-Prefix);
    # unbekannte/ungueltige Pfade -> Landing-Page (SPA-artig)
    if not target.is_relative_to(STATIC_DIR) or not target.is_file():
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

    # CORS-Preflight fuer /mcp
    if method_http == "OPTIONS":
        start_response("204 No Content", [("Content-Length", "0")] + _CORS)
        return [b""]

    if method_http != "POST":
        return _json(start_response, {"error": "method not allowed"}, "405 Method Not Allowed")

    # POST nur auf dem MCP-Endpunkt entgegennehmen (kleinere Angriffsflaeche)
    if environ.get("PATH_INFO", "/") not in _MCP_PATHS:
        return _json(start_response, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32601, "message": "Not found"}}, "404 Not Found")

    # Rate-Limit pro IP (nur fuer die POST-/mcp-Aufrufe)
    if not _rate_ok(_client_ip(environ)):
        body = json.dumps(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32029, "message": "Rate limit erreicht — bitte kurz warten."}},
            ensure_ascii=False,
        ).encode("utf-8")
        start_response("429 Too Many Requests", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Retry-After", str(_RATE_WINDOW)),
        ] + _CORS)
        return [body]

    # Request-Body begrenzt einlesen (Schutz vor Speicher-DoS durch grosse POSTs)
    cl = environ.get("CONTENT_LENGTH") or ""
    length = int(cl) if cl.isdigit() else 0
    if length > _MAX_BODY:
        return _json(start_response, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Request too large"}},
                     "413 Payload Too Large")
    try:
        raw = environ["wsgi.input"].read(length or (_MAX_BODY + 1)) or b"{}"
        if len(raw) > _MAX_BODY:
            return _json(start_response, {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": "Request too large"}},
                         "413 Payload Too Large")
        payload = json.loads(raw)
    except Exception:
        return _json(start_response, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}})

    # JSON-RPC verlangt ein Objekt; [], null, "x" etc. sind ungueltig (kein 500 auf .get()).
    if not isinstance(payload, dict):
        return _json(start_response, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}})

    req_id = payload.get("id")
    rpc_method = payload.get("method")

    # Notifications (ohne id, z. B. notifications/initialized) -> 202, kein Body
    if req_id is None and isinstance(rpc_method, str) and rpc_method.startswith("notifications/"):
        start_response("202 Accepted", [("Content-Type", "application/json")])
        return [b""]

    try:
        result = _dispatch(rpc_method, payload.get("params"), req_id)
    except _MethodNotFound:
        return _json(start_response, {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32601, "message": "Method not found"}})
    except Exception as exc:
        print(f"[belex] internal error: {exc!r}", file=sys.stderr)  # Details nur ins Log
        return _json(start_response, {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32603, "message": "Internal error"}})

    return _json(start_response, {"jsonrpc": "2.0", "id": req_id, "result": result})
