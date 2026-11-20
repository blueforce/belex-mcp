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
import ipaddress
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

# ---- Tageskontingent pro IP und Kostenklasse ------------------------------ #
# Das Minutenfenster oben bremst Haemmern, aber nicht den Dauerlaeufer: ein
# Crawler mit rund 126 Aufrufen pro Tag bleibt muehelos unter 30 pro Minute.
# Genau das war am 19./20.11.2026 zu sehen, als der Endpunkt von fremden
# Crawlern entdeckt wurde. Darum ein zweites, langsames Fenster.
#
# Getrennt nach Kosten, weil nur ein Teil der Werkzeuge nach aussen geht:
#   teuer  = vg_*     -> live in die Elasticsearch von entscheidsuche.ch, der
#                        Infrastruktur eines gemeinnuetzigen Vereins. Diese Last
#                        traegt nicht unser Server, sondern deren. vg_index_info
#                        kostet dort sogar zwei Abfragen pro Aufruf.
#   billig = belex_*  -> lokale SQLite, kostet praktisch nichts.
#
# Dimensioniert an der gemessenen echten Nutzung (Zugriffslog 31.10.-20.11.2026),
# nicht an einem Bauchwert. Werkzeugaufrufe pro IP und Tag:
#   Claude-User (echte Nutzung, verteilt auf rund 70 Anthropic-IPs):
#       Maximum 4, p95 3, Median 1 ueber 442 IP-Tage
#   codex-mcp-client (echte Nutzung von Schweizer Anschluessen):  Maximum 10
#   Python-urllib/3.12 (neuer Crawler):                           32
#   undici, agent-index, ResearchClient (neue Crawler):           2 bis 2
# Ein Tageslimit von 200, wie es naheliegt, haette keinen davon erwischt.
# Nur Python-urllib lag ueber 25 — und genau dieser Client wechselt die Adresse
# innerhalb seines Praefixes (::5e0:75 und ::5e1:f am selben Tag), weshalb der
# Zaehler bei IPv6 am /64 haengt und nicht an der Einzeladresse.
_TEURE_TOOLS = frozenset({"vg_search", "vg_get_by_nummer", "vg_index_info"})
_QUOTA = {
    # 25 ist mehr als das Sechsfache des hoechsten echten Tageswerts einer IP
    # und laesst Raum fuer weiteres Wachstum (der Verkehr hat sich seit dem
    # 17.11. bereits auf das 2,6-Fache erhoeht). Die Crawler liegen darueber.
    "teuer": 25,
    # Reine Notbremse gegen Dauerhaemmern auf die lokale SQLite. Hier schadet
    # Menge nur dem eigenen CPU-Kontingent, darum weit gefasst.
    #
    # Offen gesagt: dieser Wert haette den Crawler vom 20.11.2026 NICHT gestoppt
    # (36 billige Aufrufe aus seinem /64). Tiefer geht aber nicht sinnvoll, denn
    # die Landing-Page bucht selbst auf dieses Kontingent: jeder Seitenaufruf
    # kostet einen Aufruf (belex_index_info) und jede Demo-Suche einen weiteren
    # (belex_search). Eine Besucherin mit 15 Suchen liegt damit schon bei rund
    # 30, die hoechste beobachtete Einzelzahl echter Nutzung war 16. Zwischen
    # echter Nutzung und diesem Crawler liegt auf der billigen Seite also keine
    # saubere Grenze — und 36 Abfragen gegen eine lokale SQLite tun auch nicht
    # weh. Die scharfe Bremse gehoert dorthin, wo es fremde Last erzeugt: oben.
    "billig": 300,
}


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
        # Eigene Tabelle fuer das Tagesfenster. Bewusst NICHT in 'hits': dort
        # raeumt jeder Aufruf alles weg, was aelter als 60 Sekunden ist.
        conn.execute("CREATE TABLE IF NOT EXISTS quota("
                     "ip TEXT, tag TEXT, grp TEXT, n INTEGER, "
                     "PRIMARY KEY(ip, tag, grp))")
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


def _quota_key(ip: str) -> str:
    """Zaehlschluessel fuer das Tagesfenster.

    Bei IPv4 die Adresse selbst, bei IPv6 das /64. Ein IPv6-Client hat in der
    Regel ein ganzes /64 zur Verfuegung und kann darin beliebig wechseln: der
    gemessene Crawler tat das am 20.11.2026 innerhalb eines Tages
    (2a13:9500:1e4:12e9::5e0:75 und ::5e1:f, dazu ::41f:16 am Vortag). Pro
    Einzeladresse gezaehlt haette er sein Kontingent gratis vervielfacht.

    Das Minutenfenster bleibt bewusst auf der Einzeladresse: dort geht es um
    Haemmern aus einer Quelle, nicht um ein Tagesbudget.
    """
    try:
        adresse = ipaddress.ip_address(ip)
    except ValueError:
        return ip  # '?' oder etwas Unerwartetes: unveraendert als Schluessel
    if adresse.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return ip


def _quota_ok(ip: str, gruppe: str) -> bool:
    """False, wenn die IP ihr Tageskontingent dieser Kostenklasse erreicht hat.

    Fail-open bei DB-Fehler: eine klemmende SQLite darf echte Nutzung nicht
    aussperren, das Minutenfenster greift ohnehin weiter.
    """
    grenze = _QUOTA.get(gruppe)
    if not grenze:
        return True
    tag = time.strftime("%Y-%m-%d", time.gmtime())  # UTC, damit der Tag eindeutig ist
    schluessel = _quota_key(ip)
    try:
        _rate_ensure()
        conn = _rate_conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO quota(ip, tag, grp, n) VALUES(?,?,?,0)",
                (schluessel, tag, gruppe))
            if cur.rowcount:
                # Erster Aufruf dieser IP heute: guter Moment zum Aufraeumen.
                # Dadurch bleibt die Tabelle bei wenigen Zeilen, ohne dass
                # jeder einzelne Aufruf eine Loeschoperation kostet.
                conn.execute("DELETE FROM quota WHERE tag < ?", (tag,))
            row = conn.execute("SELECT n FROM quota WHERE ip=? AND tag=? AND grp=?",
                               (schluessel, tag, gruppe)).fetchone()
            if row and row[0] >= grenze:
                if row[0] == grenze:  # genau einmal pro Schluessel/Tag/Gruppe
                    # Bewusst OHNE Adresse: stderr.log ist im gehosteten Betrieb
                    # oeffentlich abrufbar (gemessen 20.11.2026, GET /stderr.log
                    # -> HTTP 200). Die grobe Herkunft genuegt, um spaeter
                    # nachzusehen, ob je echte Nutzung getroffen wurde.
                    herkunft = "Anthropic" if ip.startswith("160.79.106.") else "fremd"
                    print(f"[belex] Kontingent '{gruppe}' erschoepft ({tag}, {herkunft})",
                          file=sys.stderr)
                    conn.execute("UPDATE quota SET n = n + 1 WHERE ip=? AND tag=? AND grp=?",
                                 (schluessel, tag, gruppe))
                conn.commit()
                return False
            conn.execute("UPDATE quota SET n = n + 1 WHERE ip=? AND tag=? AND grp=?",
                         (schluessel, tag, gruppe))
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[belex] Tageskontingent nicht pruefbar: {exc!r}", file=sys.stderr)
        return True


def _quota_antwort(gruppe: str) -> dict:
    """Kontingent erschoepft: normales tools/call-Ergebnis, kein HTTP-Fehler.

    Absichtlich HTTP 200 mit isError statt 429: ein unerwarteter Statuscode hat
    im August 2026 bei mcp-remote einen Reconnect-Sturm ausgeloest (1,66 Mio.
    Requests). Ein Textfehler im Ergebnis reicht die Begruendung sauber an die
    Nutzerin durch, statt den Client in eine Schleife zu schicken.
    """
    if gruppe == "teuer":
        text = (f"Tageskontingent fuer die Urteilssuche erreicht ({_QUOTA['teuer']} "
                "Aufrufe pro Tag und Absenderadresse). Die Urteile kommen live von "
                "entscheidsuche.ch, der Infrastruktur eines gemeinnuetzigen Vereins; "
                "das Kontingent haelt die Last dort fair. Die Gesetze (BSG) bleiben "
                "uneingeschraenkt nutzbar. Ab morgen ist das Kontingent wieder frei.")
    else:
        text = (f"Tageskontingent erreicht ({_QUOTA['billig']} Werkzeugaufrufe pro Tag "
                "und Absenderadresse). Ab morgen ist es wieder frei.")
    return {"content": [{"type": "text", "text": text}], "isError": True}


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


def _text(start_response, body: str, status: str, extra=None):
    """Kleine Klartext-Antwort (fuer 405/429) statt einer 40-KB-Seite."""
    data = body.encode("utf-8")
    headers = [("Content-Type", "text/plain; charset=utf-8"),
               ("Content-Length", str(len(data)))]
    if extra:
        headers += extra
    start_response(status, headers)
    return [data]


def _serve_static(path_info: str, start_response):
    """Landing-Page (GET /) und Assets (GET /assets/...) ausliefern.

    Unbekannte Pfade -> echte 404 (KEIN SPA-Catch-all). Wichtig: sonst
    liefern /.well-known/oauth-* und /register HTTP 200 (Landing-Page),
    woraufhin der claude.ai-Connector-Flow faelschlich einen OAuth-Server
    annimmt und die Dynamic Client Registration scheitert.
    """
    rel = (path_info or "/").lstrip("/") or "index.html"
    target = (STATIC_DIR / rel).resolve()
    # Path-Traversal verhindern (echte Pfad-Relation, kein String-Prefix);
    # unbekannte/ungueltige Pfade -> 404 statt Landing-Page
    if not target.is_relative_to(STATIC_DIR) or not target.is_file():
        return _json(start_response,
                     {"error": "not found"}, "404 Not Found")
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

    # GET -> 405 auf /mcp, sonst statische Landing-Page / Assets
    if method_http == "GET":
        path = environ.get("PATH_INFO", "/")
        # MCP-Spec (Streamable HTTP): Bietet der Server auf GET keinen SSE-Stream
        # an, MUSS er 405 antworten. Vorher lieferte /mcp hier die Landing-Page mit
        # HTTP 200; Clients wie mcp-remote werteten das als Fehler und verbanden im
        # Sekundentakt neu -> 1.66 Mio. GET-Requests und ~66 GB Traffic im Aug 2026.
        if path in _MCP_PATHS:
            return _text(start_response,
                         "Method Not Allowed. Dieser Endpunkt spricht MCP ueber POST.\n",
                         "405 Method Not Allowed",
                         [("Allow", "POST, OPTIONS")] + _CORS)
        if path in ("/health", "/health/"):
            return _json(start_response, {"status": "ok"})
        # Rate-Limit neu auch fuer GET: frueher lief GET vollstaendig daran vorbei.
        if not _rate_ok(_client_ip(environ)):
            return _text(start_response, "Too Many Requests\n",
                         "429 Too Many Requests",
                         [("Retry-After", str(_RATE_WINDOW))])
        return _serve_static(path, start_response)

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

    # Tageskontingent: greift nur bei tools/call, getrennt nach Kostenklasse.
    # initialize, tools/list und ping bleiben frei, damit das Protokoll-Handshake
    # echter Clients nie am Kontingent scheitert.
    if rpc_method == "tools/call":
        p = payload.get("params")
        tool = p.get("name") if isinstance(p, dict) else None
        gruppe = "teuer" if tool in _TEURE_TOOLS else "billig"
        if not _quota_ok(_client_ip(environ), gruppe):
            return _json(start_response, {"jsonrpc": "2.0", "id": req_id,
                         "result": _quota_antwort(gruppe)})

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
