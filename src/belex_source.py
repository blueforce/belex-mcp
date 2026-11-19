"""
belex_source.py — Datenschicht fuer BELEX-MCP.

Implementiert ist WEG 1 (LexFind / HuggingFace-Datensatz):
    Datensatz "rcds/swiss_legislation" (Quelle LexFind.ch, Lizenz CC-BY-SA-4.0),
    Datei train.jsonl.xz (~190 MB), gefiltert auf Kanton BE / Sprache Deutsch,
    ingestet in lokale SQLite mit FTS5. Danach laeuft der Server komplett offline.

Datenlage im Dump (verifiziert am 2026-07-10):
    - 35'698 Zeilen gesamt, davon 1'344 Kanton BE (672 de + 672 fr), alle aktiv.
    - sr_number ist fuer BE die BSG-Nummer (z. B. '721.0' = Baugesetz).
    - version_active_since = Stand der Fassung (je Erlass ca. 2020-2023,
      der Dump ist ein Snapshot, NICHT tagesaktuell).
    - html_content ist DEFEKT (identischer Fremdinhalt fuer alle Zeilen) ->
      der Volltext kommt aus pdf_content (bereits extrahierter Klartext).
    - html_url zeigt auf die amtliche Quelle: belex.sites.be.ch/data/<BSG>/de

Zusaetzlich implementiert ist WEG 2 (BELEX-API live) fuer TAGESAKTUELLE Texte:
    Die LexWork-App laedt ihre Inhalte ueber eine undokumentierte, aber stabile
    JSON-API des Kantons:
      - Enumeration: /api/<lang>/texts_of_law/lightweight_index  (alle Erlasse)
      - Volltext:    /api/<lang>/texts_of_law/<BSG>  -> Feld xhtml_tol (HTML)
    build_index_live() ingestiert von dort in dieselben Tabellen. Aufruf:
      python src/belex_source.py --live
    Vorteil: tagesaktuell (721.0 z. B. Stand 01.04.2023 statt 01.01.2023 im Dump),
    kantonseigene Quelle, ~704 aktive Erlasse. Nachteil: undokumentiert (die
    Endpunkte koennen sich aendern) und ~704 HTTP-Requests pro Refresh.

WEG 3 (per-Erlass PDF parsen) bleibt ungenutzt; WEG 2 deckt denselben Zweck
strukturierter ab (HTML statt PDF).

WICHTIG: Rechtsinformation, keine Rechtsberatung. Verbindlich ist allein die
amtliche Quelle auf belex.sites.be.ch.
"""

from __future__ import annotations

import html as html_lib
import json
import lzma
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

# Lokale SQLite als Cache/Index. Wird von build_index()/build_index_live() befuellt.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "belex.sqlite"
DUMP_PATH = DATA_DIR / "train.jsonl.xz"

BELEX_BASE = "https://www.belex.sites.be.ch"
BELEX_API = f"{BELEX_BASE}/api"  # WEG 2: undokumentierte, aber stabile JSON-API

DATASET = "rcds/swiss_legislation"
DUMP_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main/train.jsonl.xz"
DATENQUELLE = f"HuggingFace {DATASET} (LexFind.ch-Dump, CC-BY-SA-4.0)"
DATENQUELLE_LIVE = "BELEX-API (belex.sites.be.ch, Kanton Bern) — tagesaktuell"

KANTON = "be"
SPRACHE = "de"  # Bern ist zweisprachig; fr liegt bereit (Upgrade-Pfad)

# Schema fuer beide Ingest-Wege identisch -> server.py bleibt unveraendert.
_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS erlass(
        bsg TEXT PRIMARY KEY, title TEXT, text TEXT,
        as_of TEXT, source_url TEXT
    );
    -- Externer Content: FTS haelt nur den Index, die Daten bleiben in
    -- 'erlass' (spart ~50% Dateigroesse gegenueber einer Kopie).
    CREATE VIRTUAL TABLE IF NOT EXISTS erlass_fts USING fts5(
        bsg, title, text,
        content='erlass',
        tokenize="unicode61 remove_diacritics 2"
    );
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


# --------------------------------------------------------------------------- #
# Oeffentliche API, die server.py nutzt
# --------------------------------------------------------------------------- #
def search(keyword: str, limit: int = 15) -> list[dict]:
    """Volltextsuche ueber die lokal indexierten Berner Erlasse (WEG 1).

    Zweistufig: zuerst Treffer im Titel (praezise, z. B. 'Baugesetz' -> 721.0),
    danach mit Volltext-Treffern auffuellen. Innerhalb der Stufen bm25-gerankt.
    """
    if not DB_PATH.exists():
        return [{"bsg": "?", "title": _hint("search")}]
    query = _fts_query(keyword)
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    con = sqlite3.connect(DB_PATH)
    try:
        hits: list[dict] = []
        seen: set[str] = set()
        for match in (f"{{title}} : ({query})", query):
            rows = con.execute(
                "SELECT f.bsg, f.title, e.as_of FROM erlass_fts f "
                "JOIN erlass e ON e.rowid = f.rowid "
                "WHERE erlass_fts MATCH ? "
                "ORDER BY bm25(erlass_fts, 2.0, 10.0, 1.0) LIMIT ?",
                (match, limit),
            ).fetchall()
            for bsg, title, as_of in rows:
                if bsg not in seen and len(hits) < limit:
                    seen.add(bsg)
                    hits.append({"bsg": bsg, "title": title, "as_of": as_of})
        return hits
    finally:
        con.close()


def get_by_bsg(bsg_number: str) -> dict | None:
    """Erlass per BSG-Nummer aus dem lokalen Index holen (WEG 1)."""
    nummer = _assert_bsg(bsg_number)
    if not DB_PATH.exists():
        return {"bsg": nummer, "title": _hint("get_by_bsg"), "text": ""}
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT bsg, title, text, as_of, source_url FROM erlass WHERE bsg = ?",
            (nummer,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return dict(zip(("bsg", "title", "text", "as_of", "source_url"), row))


def get_article(bsg_number: str, article: str) -> str | None:
    """Einzelnen Artikel aus dem Volltext schneiden (einfache Heuristik)."""
    doc = get_by_bsg(bsg_number)
    if not doc or not doc.get("text"):
        return None
    art = re.escape(article.strip())
    m = re.search(
        rf"(Art\.?\s*{art}\b.*?)(?=\n?\s*Art\.?\s*\d+[a-z]?\b)",
        doc["text"], flags=re.S,
    )
    return re.sub(r"[ \t]+", " ", m.group(1)).strip()[:4000] if m else None


def get_meta() -> dict:
    """Infos zum Index: Datenquelle, Indexdatum, Anzahl Erlasse."""
    if not DB_PATH.exists():
        return {"hinweis": _hint("get_meta")}
    con = sqlite3.connect(DB_PATH)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        meta["anzahl_erlasse"] = con.execute("SELECT COUNT(*) FROM erlass").fetchone()[0]
        return meta
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Index-Aufbau (WEG 1)
# --------------------------------------------------------------------------- #
def build_index(force: bool = False) -> int:
    """Bern-Erlasse aus dem LexFind-Dump laden und in SQLite+FTS5 schreiben.

    Laedt train.jsonl.xz einmalig nach data/ (falls nicht vorhanden), streamt
    die Zeilen (lzma, kein Vollentpacken), filtert auf canton=be/language=de
    und befuellt die Tabellen 'erlass' und 'erlass_fts'. Gibt die Anzahl
    ingestierter Erlasse zurueck. force=True baut den Index neu auf.
    """
    if not DUMP_PATH.exists():
        _download_dump()
    if force and DB_PATH.exists():
        DB_PATH.unlink()

    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(_SCHEMA_SQL)
        if not force and con.execute("SELECT COUNT(*) FROM erlass").fetchone()[0]:
            print("Index existiert bereits — Neuaufbau mit --force.", file=sys.stderr)
            return con.execute("SELECT COUNT(*) FROM erlass").fetchone()[0]

        n = 0
        for row in _iter_dump():
            if (
                row.get("canton") != KANTON
                or row.get("language") != SPRACHE
                or not row.get("is_active")
            ):
                continue
            bsg = (row.get("sr_number") or "").strip()
            if not bsg:
                continue
            title = (row.get("title") or "").strip()
            abbr = (row.get("abbreviation") or "").strip()
            if abbr:
                title = f"{title} ({abbr})"
            # html_content ist im Dump defekt -> Volltext aus pdf_content.
            text = _clean_text(row.get("pdf_content") or "")
            con.execute(
                "INSERT OR REPLACE INTO erlass (bsg, title, text, as_of, source_url) "
                "VALUES (?,?,?,?,?)",
                (
                    bsg,
                    title,
                    text,
                    row.get("version_active_since") or "",
                    row.get("html_url") or f"{BELEX_BASE}/data/{bsg}/{SPRACHE}",
                ),
            )
            n += 1

        con.execute("INSERT INTO erlass_fts(erlass_fts) VALUES('rebuild')")
        con.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
            [
                ("datenquelle", DATENQUELLE),
                ("indexiert_am", date.today().isoformat()),
                ("kanton", KANTON),
                ("sprache", SPRACHE),
            ],
        )
        con.commit()
        return con.execute("SELECT COUNT(*) FROM erlass").fetchone()[0]
    finally:
        con.close()


def _download_dump() -> None:
    """LexFind-Dump (~190 MB) einmalig nach data/ laden."""
    import httpx  # nur beim Index-Aufbau noetig, nicht im Serverbetrieb

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DUMP_PATH.with_suffix(".xz.part")
    print(f"Lade {DUMP_URL} ...", file=sys.stderr)
    with httpx.stream("GET", DUMP_URL, follow_redirects=True, timeout=None) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tmp.rename(DUMP_PATH)


def _iter_dump():
    """Zeilen des Dumps streamen (lzma-dekomprimiert, JSON pro Zeile)."""
    with lzma.open(DUMP_PATH, "rt", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


# --------------------------------------------------------------------------- #
# Index-Aufbau (WEG 2) — tagesaktuell aus der BELEX-API
# --------------------------------------------------------------------------- #
def build_index_live(lang: str = SPRACHE, pause: float = 0.1) -> int:
    """Erlasse direkt aus der BELEX-API laden und in SQLite+FTS5 schreiben.

    Enumeriert ueber .../texts_of_law/lightweight_index alle aktiven Erlasse und
    holt pro Erlass .../texts_of_law/<BSG> (Feld xhtml_tol = HTML-Volltext,
    publication_enactment = Stand). Baut den Index frisch neu auf (tagesaktuell).
    pause: Hoeflichkeitspause zwischen den Requests (Sekunden).

    Gibt die Anzahl ingestierter Erlasse zurueck.
    """
    import httpx

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=BELEX_API, timeout=60.0, follow_redirects=True) as client:
        index = client.get(f"/{lang}/texts_of_law/lightweight_index").raise_for_status().json()
        # index = { <kategorie-id>: [ {systematic_number, title, abrogated, ...}, ... ] }
        erlasse = [
            e for lst in index.values() for e in lst if not e.get("abrogated")
        ]
        # Deterministische Reihenfolge (nach BSG-Nummer) fuer nachvollziehbare Laeufe.
        erlasse.sort(key=lambda e: _bsg_sort_key(e.get("systematic_number", "")))
        total = len(erlasse)
        print(f"BELEX-API: {total} aktive Erlasse zu ingesten ({lang}) ...", file=sys.stderr)

        if DB_PATH.exists():
            DB_PATH.unlink()  # Live-Refresh baut immer frisch auf.
        con = sqlite3.connect(DB_PATH)
        try:
            con.executescript(_SCHEMA_SQL)
            n = 0
            for i, meta in enumerate(erlasse, 1):
                bsg = (meta.get("systematic_number") or "").strip()
                if not bsg:
                    continue
                try:
                    doc = client.get(f"/{lang}/texts_of_law/{bsg}").raise_for_status().json()
                except httpx.HTTPError as exc:  # einzelnen Erlass ueberspringen, Lauf fortsetzen
                    print(f"  ! {bsg}: {exc!r} — uebersprungen", file=sys.stderr)
                    continue
                tol = doc.get("text_of_law", {})
                version = tol.get("selected_version") or tol.get("current_version") or {}
                title = (tol.get("title") or meta.get("title") or "").strip()
                abbr = (tol.get("abbreviation") or "").strip()
                if abbr:
                    title = f"{title} ({abbr})"
                text = _strip_html(version.get("xhtml_tol") or "")
                con.execute(
                    "INSERT OR REPLACE INTO erlass (bsg, title, text, as_of, source_url) "
                    "VALUES (?,?,?,?,?)",
                    (
                        bsg,
                        title,
                        text,
                        tol.get("publication_enactment") or "",
                        f"{BELEX_BASE}/app/{lang}/texts_of_law/{bsg}",
                    ),
                )
                n += 1
                if i % 50 == 0 or i == total:
                    print(f"  {i}/{total} ({bsg}) ...", file=sys.stderr)
                    con.commit()
                if pause:
                    time.sleep(pause)

            con.execute("INSERT INTO erlass_fts(erlass_fts) VALUES('rebuild')")
            con.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                [
                    ("datenquelle", DATENQUELLE_LIVE),
                    ("indexiert_am", date.today().isoformat()),
                    ("kanton", KANTON),
                    ("sprache", lang),
                ],
            )
            con.commit()
            return con.execute("SELECT COUNT(*) FROM erlass").fetchone()[0]
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# Hilfen
# --------------------------------------------------------------------------- #
# Gueltige BSG-Nummern sind numerisch-hierarchisch und koennen einen Anhang
# nach Bindestrich tragen (Anhaenge und Teilerlasse), z. B. '721.0', '105.43-1',
# '105.235.1-12'. Bewusst ASCII-Ziffern: \d wuerde auch fremde Ziffernzeichen
# zulassen, die in keiner BSG-Nummer vorkommen.
_BSG_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:-[0-9]+)?")

BSG_BEISPIELE = "721.0, 105.43-1, 105.235.1-12"
BSG_FORMAT_HINWEIS = (
    "Eine BSG-Nummer besteht aus Ziffern mit Punkten und optional einem Anhang "
    f"nach Bindestrich, zum Beispiel {BSG_BEISPIELE}."
)


def normalize_bsg(bsg: str) -> str:
    """Eingabe behutsam glaetten, bevor geprueft wird.

    Faengt ab, was Clients typischerweise mitschicken: Leerraum, ein
    vorangestelltes 'BSG' und typografische Striche statt des Bindestrichs.
    """
    s = (bsg or "").strip()
    s = re.sub(r"^BSG[\s.:]*", "", s, flags=re.I).strip()
    for strich in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        s = s.replace(strich, "-")
    return s


def _assert_bsg(bsg: str) -> str:
    """Normalisierte BSG-Nummer zurueckgeben oder mit Hinweis scheitern."""
    s = normalize_bsg(bsg)
    if not _BSG_RE.fullmatch(s):
        raise ValueError(f"Ungueltige BSG-Nummer: {bsg!r}. {BSG_FORMAT_HINWEIS}")
    return s


def aehnliche_bsg(bsg: str, limit: int = 5) -> list[str]:
    """Nummern vorschlagen, die auf der gesuchten aufbauen (Anhaenge, Unterziffern)."""
    s = normalize_bsg(bsg)
    # Nur gepruefte Nummern ins LIKE-Muster: so koennen weder '%' noch '_'
    # aus der Eingabe als Platzhalter wirken.
    if not _BSG_RE.fullmatch(s) or not DB_PATH.exists():
        return []
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT bsg FROM erlass WHERE bsg LIKE ? OR bsg LIKE ? ORDER BY bsg LIMIT ?",
            (f"{s}.%", f"{s}-%", limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]


def _fts_query(keyword: str) -> str:
    """Nutzereingabe in eine sichere FTS5-Query uebersetzen (Tokens als Phrasen)."""
    tokens = re.findall(r"\w+", keyword, flags=re.UNICODE)
    return " ".join(f'"{t}"' for t in tokens)


def _clean_text(raw: str) -> str:
    """Leichte Normalisierung des extrahierten PDF-Texts."""
    raw = raw.replace("\xa0", " ")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _strip_html(raw: str) -> str:
    """HTML (xhtml_tol der BELEX-API) in lesbaren Klartext wandeln."""
    raw = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", raw, flags=re.I | re.S)
    raw = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>|<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    # Zeilenweise trimmen, Leerzeilen kollabieren (xhtml_tol ist sehr luftig).
    lines = [ln.strip() for ln in raw.replace("\xa0", " ").split("\n")]
    out, blank = [], False
    for ln in lines:
        ln = re.sub(r"[ \t]+", " ", ln)
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _bsg_sort_key(bsg: str):
    """BSG-Nummer numerisch-hierarchisch sortieren ('152.05' vor '721.0').

    Jedes Segment als (0, zahl) oder (1, text), damit gemischte Segmente
    (z. B. '152.5a') typkonsistent vergleichbar bleiben.
    """
    return [(0, int(p)) if p.isdigit() else (1, p) for p in bsg.split(".")]


def _hint(fn: str) -> str:
    return (f"[belex.sqlite fehlt — zuerst build_index() ausfuehren "
            f"(python src/belex_source.py); {fn} liefert dann echte Daten]")


if __name__ == "__main__":
    # --live  -> tagesaktuell aus der BELEX-API (WEG 2)
    # sonst   -> Offline-Dump (WEG 1); --force baut neu auf
    if "--live" in sys.argv:
        count = build_index_live()
        quelle = "BELEX-API (live)"
    else:
        count = build_index(force="--force" in sys.argv)
        quelle = "LexFind-Dump"
    meta = get_meta()
    print(f"Index gebaut ({quelle}): {count} Berner Erlasse (BSG, deutsch) -> {DB_PATH}")
    print(f"Datenquelle: {meta.get('datenquelle')} · indexiert am {meta.get('indexiert_am')}")
    print("Smoke-Test 'Baugesetz':")
    for h in search("Baugesetz", limit=3):
        print(f"  BSG {h['bsg']} — {h['title']} · Stand {h['as_of']}")
