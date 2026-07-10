"""
vg_live.py — Live-Datenschicht fuer die Gerichtspraxis (Verwaltungsgericht Bern).

Anders als bei den Gesetzen wird hier NICHTS lokal indexiert. Die Urteile kommen
live aus der offenen Elasticsearch von entscheidsuche.ch (gemeinnuetziger Verein,
taeglich aktualisiert, keine Authentifizierung noetig):

    https://entscheidsuche.pansoft.de:9200/entscheidsuche.v2-*/_search

Gefiltert wird auf die Hierarchie-Stufe "BE_VG_001" = Verwaltungsgericht des
Kantons Bern. Das Dokument-Feld attachment.content enthaelt den Volltext bereits,
title/abstract die Metadaten, attachment.content_url die stabile PDF-Fundstelle.

Vorteil: immer tagesaktuell, keine 221-MB-Kopie, kein Refresh-Cron, nutzt die
ausgereifte Suche der Fachinfrastruktur. Nachteil: zur Laufzeit von der
entscheidsuche-Verfuegbarkeit abhaengig.

WICHTIG: Rechtsinformation, keine Rechtsberatung. Anonymisierte
Publikationsfassung; verbindlich ist das Original des Gerichts.
"""

from __future__ import annotations

import re
import ssl
from pathlib import Path

import certifi
import httpx

ES_URL = "https://entscheidsuche.pansoft.de:9200/entscheidsuche.v2-*/_search"
HIERARCHY = "BE_VG_001"  # Verwaltungsgericht des Kantons Bern
TIMEOUT = 20.0

# Der ES-Server liefert seine Intermediate-CA nicht mit (Sectigo). Wir ergaenzen
# sie aus der mitgelieferten PEM, damit die Zertifikatskette sauber verifiziert
# (kein verify=False — bei einem Rechtsinfo-Dienst waere das das falsche Signal).
_CA_FILE = Path(__file__).with_name("entscheidsuche_ca.pem")
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
if _CA_FILE.exists():
    _SSL_CTX.load_verify_locations(cafile=str(_CA_FILE))


def _es(body: dict) -> dict:
    """Elasticsearch-Query absetzen und JSON zurueckgeben."""
    with httpx.Client(verify=_SSL_CTX, timeout=TIMEOUT) as client:
        resp = client.post(ES_URL, json=body)
        resp.raise_for_status()
        return resp.json()


def _gnr_and_date(doc_id: str) -> tuple[str, str]:
    """'BE_VG_001_100-2022-280_2023-09-31' -> ('100.2022.280', '2023-09-31')."""
    m = re.match(r"BE_VG_001_(.+)_(\d{4}-\d{2}-\d{2})$", doc_id)
    if not m:
        return doc_id, ""
    return m.group(1).replace("-", "."), m.group(2)


def _betreff(src: dict) -> str:
    """Kurzbeschrieb: abstract bevorzugt, sonst Anfang des Volltexts."""
    abstract = (src.get("abstract") or {}).get("de") or ""
    if abstract.strip():
        return re.sub(r"\s+", " ", abstract).strip()[:400]
    content = (src.get("attachment") or {}).get("content") or ""
    m = re.search(r"\bbetreffend\b\s*(.{5,300}?)(?:\(|\n|Urteil des|Sachverhalt)",
                  content[:3000], flags=re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def search(keyword: str, limit: int = 15) -> list[dict]:
    """Volltextsuche in den VGer-Bern-Urteilen (live, relevanzsortiert).

    Gibt pro Treffer Geschaeftsnummer, Datum, Betreff, ein Highlight-Snippet und
    die Fundstelle (PDF-URL auf entscheidsuche.ch) zurueck.
    """
    kw = keyword.strip()
    if not kw:
        return []
    limit = max(1, min(int(limit), 50))
    body = {
        "size": limit,
        "_source": ["id", "date", "title.de", "abstract.de", "attachment.content_url"],
        "query": {
            "bool": {
                "must": [{"match": {"attachment.content": kw}}],
                "filter": [{"term": {"hierarchy": HIERARCHY}}],
            }
        },
        "highlight": {
            "fields": {"attachment.content": {"fragment_size": 180, "number_of_fragments": 1}},
            "pre_tags": [">>"], "post_tags": ["<<"],
        },
    }
    data = _es(body)
    out = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        gnr, datum = _gnr_and_date(src.get("id", hit.get("_id", "")))
        snippet = ""
        hl = hit.get("highlight", {}).get("attachment.content")
        if hl:
            snippet = re.sub(r"\s+", " ", hl[0]).strip()
        out.append({
            "gnr": gnr,
            "datum": datum or src.get("date", ""),
            "betreff": _betreff(src),
            "snippet": snippet,
            "quelle_url": (src.get("attachment") or {}).get("content_url", ""),
        })
    return out


def info() -> dict:
    """Datenstand: Anzahl VGer-Urteile und neuester Entscheid (live)."""
    total = _es({"size": 0, "track_total_hits": True,
                 "query": {"term": {"hierarchy": HIERARCHY}}})["hits"]["total"]["value"]
    newest = _es({"size": 1, "_source": ["date"], "sort": [{"date": "desc"}],
                  "query": {"term": {"hierarchy": HIERARCHY}}})["hits"]["hits"]
    return {"anzahl": total, "neuester": newest[0]["_source"]["date"] if newest else "?"}


def get_by_nummer(geschaeftsnummer: str) -> dict | None:
    """Urteil per Geschaeftsnummer live abrufen (z. B. '100.2022.280').

    Akzeptiert '100.2022.280', '100 2022 280' oder '100-2022-280'. Bei mehreren
    Fassungen wird die neueste (nach Datum) genommen.
    """
    gnr = geschaeftsnummer.strip().replace(" ", ".").replace("-", ".").rstrip("Uu")
    if not re.fullmatch(r"[\d.]+", gnr):
        raise ValueError(f"Ungueltige Geschaeftsnummer: {geschaeftsnummer!r}")
    id_fragment = "BE_VG_001_" + gnr.replace(".", "-")
    body = {
        "size": 1,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"hierarchy": HIERARCHY}},
                    {"prefix": {"id": id_fragment}},
                ]
            }
        },
        "sort": [{"date": "desc"}],
    }
    hits = _es(body).get("hits", {}).get("hits", [])
    if not hits:
        return None
    src = hits[0].get("_source", {})
    gnr_norm, datum = _gnr_and_date(src.get("id", hits[0].get("_id", "")))
    att = src.get("attachment") or {}
    return {
        "gnr": gnr_norm,
        "datum": datum or src.get("date", ""),
        "titel": (src.get("title") or {}).get("de", ""),
        "betreff": _betreff(src),
        "text": att.get("content") or "",
        "quelle_url": att.get("content_url", ""),
    }
