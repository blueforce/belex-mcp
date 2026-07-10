"""
berner-recht-mcp — MCP-Server fuer Berner Recht UND Gerichtspraxis in einem.

Kombiniert zwei Datenschichten unter einem Endpunkt:
  - Gesetze (BSG):        belex_source.py  -> lokale SQLite (tagesaktuell via BELEX-API)
  - Gerichtspraxis (VGer): vg_live.py      -> live aus der entscheidsuche.ch-Elasticsearch

Laeuft lokal (stdio, Claude Desktop) und gehostet (streamable-http, siehe wsgi.py).

WICHTIG: Rechtsinformation, KEINE Rechtsberatung. Jede Ausgabe nennt die
Fundstelle; verbindlich sind die amtlichen Quellen (belex.sites.be.ch bzw. das
Original des Gerichts). Im Zweifel juristisch pruefen lassen.
"""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

import belex_source
import vg_live

# stateless_http + json_response: jeder Tool-Call ist ein einzelner POST -> JSON
# (kein langlebiger SSE-Stream). Das passt zum WSGI/Passenger-Modell des Shared-
# Hosts und wird von stdio (lokal, Claude Desktop) ignoriert.
mcp = FastMCP("berner-recht", stateless_http=True, json_response=True)

DISCLAIMER = (
    "⚠️ Rechtsinformation, keine Rechtsberatung. Die Ausgabe kann unvollstaendig "
    "oder nicht mehr aktuell sein und ersetzt keine anwaltliche Pruefung. "
    "Verbindlich ist allein die amtliche Quelle — im Zweifel juristisch pruefen lassen."
)


# --------------------------------------------------------------------------- #
# Gesetze (Bernische Systematische Gesetzessammlung, BSG)
# --------------------------------------------------------------------------- #
@mcp.tool()
def belex_search(keyword: str, limit: int = 15) -> str:
    """Sucht Berner Erlasse (Gesetze/Verordnungen der BSG) per Stichwort.

    keyword: z. B. 'Baugesetz', 'Gewaesserraum', 'Datenschutz'. limit: max. Treffer.
    """
    hits = belex_source.search(keyword, limit=limit)
    if not hits:
        return f"Keine Gesetzes-Treffer fuer {keyword!r} in der BSG."
    out = [f"BSG {h.get('bsg','?')} — {h.get('title','')}"
           + (f" · Stand {h['as_of']}" if h.get("as_of") else "")
           for h in hits]
    return "\n".join(out) + f"\n\n{DISCLAIMER}"


@mcp.tool()
def belex_get_by_bsg(bsg_number: str, max_chars: int = 12000) -> str:
    """Holt einen Berner Erlass per BSG-Nummer (z. B. '721.0' = Baugesetz) mit Volltext."""
    doc = belex_source.get_by_bsg(bsg_number)
    if not doc:
        return f"Kein Erlass zu BSG {bsg_number!r} gefunden."
    text = doc.get("text", "")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...gekuerzt — Einzelartikel via belex_get_article...]"
    return (
        f"Titel: {doc.get('title','')}\n"
        f"BSG-Nummer: {doc.get('bsg','')}\n"
        f"Stand: {doc.get('as_of','—')}\n"
        f"Quelle: {doc.get('source_url','—')}\n"
        f"{DISCLAIMER}\n{'-'*60}\n{text}"
    )


@mcp.tool()
def belex_get_article(bsg_number: str, article: str) -> str:
    """Extrahiert einen einzelnen Artikel aus einem Berner Erlass (Heuristik)."""
    doc = belex_source.get_by_bsg(bsg_number)
    if not doc or not doc.get("text"):
        return f"Kein Erlass/Text zu BSG {bsg_number!r} gefunden."
    art = re.escape(article.strip())
    m = re.search(rf"(Art\.?\s*{art}\b.*?)(?=\n?\s*Art\.?\s*\d+[a-z]?\b)",
                  doc["text"], flags=re.S)
    if not m:
        return f"Art. {article} in BSG {bsg_number} nicht gefunden."
    snippet = re.sub(r"[ \t]+", " ", m.group(1)).strip()[:4000]
    return (f"BSG {doc['bsg']} · Stand {doc.get('as_of','—')}\n"
            f"Quelle: {doc.get('source_url','—')}\n{DISCLAIMER}\n{'-'*60}\n{snippet}")


# --------------------------------------------------------------------------- #
# Gerichtspraxis (Verwaltungsgericht Bern, live von entscheidsuche.ch)
# --------------------------------------------------------------------------- #
@mcp.tool()
def vg_search(keyword: str, limit: int = 15) -> str:
    """Sucht Urteile des Verwaltungsgerichts Bern per Stichwort im Volltext (live).

    keyword: z. B. 'Gewaesserraum', 'Baubewilligung Ausnuetzung'. limit: max. Treffer.
    Datenquelle: entscheidsuche.ch (tagesaktuell).
    """
    try:
        hits = vg_live.search(keyword, limit=limit)
    except Exception as exc:  # entscheidsuche nicht erreichbar
        return f"Urteilssuche momentan nicht verfuegbar (entscheidsuche.ch): {exc}"
    if not hits:
        return f"Keine VGer-Urteile fuer {keyword!r} gefunden."
    out = []
    for h in hits:
        zeile = f"VGE {h['gnr']} vom {h['datum']}"
        if h.get("betreff"):
            zeile += f" — {h['betreff'][:140]}"
        if h.get("snippet"):
            zeile += f"\n    [{h['snippet']}]"
        out.append(zeile)
    return "\n".join(out) + f"\n\n{DISCLAIMER}"


@mcp.tool()
def vg_get_by_nummer(geschaeftsnummer: str, max_chars: int = 12000) -> str:
    """Holt ein VGer-Bern-Urteil per Geschaeftsnummer (z. B. '100.2022.280') mit Volltext (live)."""
    try:
        doc = vg_live.get_by_nummer(geschaeftsnummer)
    except Exception as exc:
        return f"Urteilsabruf momentan nicht verfuegbar (entscheidsuche.ch): {exc}"
    if not doc:
        return f"Kein VGer-Urteil zu {geschaeftsnummer!r} gefunden."
    text = doc.get("text", "")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...gekuerzt...]"
    return (
        f"Urteil des Verwaltungsgerichts des Kantons Bern {doc.get('gnr','')} "
        f"vom {doc.get('datum','')}\n"
        f"Betreffend: {doc.get('betreff') or '—'}\n"
        f"Quelle (entscheidsuche.ch): {doc.get('quelle_url','—')}\n"
        f"{DISCLAIMER}\n{'-'*60}\n{text}"
    )


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@mcp.tool()
def belex_index_info() -> str:
    """Datenstand der Gesetze (BSG): Anzahl Erlasse, Quelle, Indexdatum."""
    meta = belex_source.get_meta()
    return (
        "Gesetze — Bernische Systematische Gesetzessammlung (BSG):\n"
        f"Erlasse im Index: {meta.get('anzahl_erlasse','?')}\n"
        f"Quelle: {meta.get('datenquelle','?')}\n"
        f"Indexiert am: {meta.get('indexiert_am','?')}\n"
        f"{DISCLAIMER}"
    )


@mcp.tool()
def vg_index_info() -> str:
    """Datenstand der Gerichtspraxis (VGer Bern): Anzahl, Quelle, neuester Entscheid (live)."""
    try:
        i = vg_live.info()
    except Exception as exc:
        return f"Datenstand momentan nicht verfuegbar (entscheidsuche.ch): {exc}"
    return (
        "Gerichtspraxis — Verwaltungsgericht des Kantons Bern:\n"
        f"Urteile durchsuchbar: {i['anzahl']} (live, alle Jahrgaenge)\n"
        f"Neuester Entscheid: {i['neuester']}\n"
        "Quelle: entscheidsuche.ch (tagesaktuell)\n"
        f"{DISCLAIMER}"
    )


if __name__ == "__main__":
    mcp.run()  # stdio-Transport (Claude Desktop)
