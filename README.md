# belex — Berner Recht & Gerichtspraxis als MCP-Server

Ein [MCP](https://modelcontextprotocol.io)-Server, der KI-Assistenten (Claude & Co.)
**zitierfähigen** Zugriff auf zwei Bestände des Kantons Bern gibt:

- **Gesetze** — die Bernische Systematische Gesetzessammlung (**BSG**)
- **Gerichtspraxis** — die Urteile des **Verwaltungsgerichts** des Kantons Bern

Anders als das Bundesrecht liegen beide nicht als offene Schnittstelle vor. belex
führt sie zusammen und liefert jede Antwort mit Fundstelle (BSG-Nummer + Stand bzw.
Geschäftsnummer + Datum) aus den amtlichen Quellen.

> ⚠️ **Rechtsinformation, keine Rechtsberatung.** Die Ausgaben können unvollständig
> oder nicht mehr aktuell sein und ersetzen keine anwaltliche Prüfung. Verbindlich
> ist allein die amtliche Fassung ([belex.sites.be.ch](https://belex.sites.be.ch)
> bzw. das Original des Gerichts). Im Zweifel immer juristisch prüfen lassen.

## Tools

| Tool | Bestand | Zweck |
|------|---------|-------|
| `belex_search` | Gesetze | Erlasse per Stichwort finden |
| `belex_get_by_bsg` | Gesetze | Erlass per BSG-Nummer (z. B. `721.0`) mit Volltext |
| `belex_get_article` | Gesetze | Einzelnen Artikel herausschneiden |
| `belex_index_info` | Gesetze | Datenstand (Anzahl, Quelle, Indexdatum) |
| `vg_search` | Urteile | VGer-Urteile per Stichwort im Volltext |
| `vg_get_by_nummer` | Urteile | Urteil per Geschäftsnummer (z. B. `100.2022.280`) |
| `vg_index_info` | Urteile | Datenstand (Anzahl, neuester Entscheid) |

## Nutzen (Remote, ohne Installation)

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "belex": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://belex.blueforce.ch/mcp"]
    }
  }
}
```

Danach das KI-Tool neu starten. Landing-Page & Demo: <https://belex.blueforce.ch>

## Datenquellen & Architektur

- **Gesetze:** lokaler SQLite-Index (FTS5), tagesaktuell gebaut aus der
  undokumentierten, aber stabilen JSON-API der BELEX-App (`belex.sites.be.ch/api`).
- **Gerichtspraxis:** live abgefragt aus der offenen Elasticsearch von
  [entscheidsuche.ch](https://entscheidsuche.ch) (gefiltert auf das Verwaltungsgericht
  Bern), kein eigener Index nötig.

```
src/belex_source.py   Gesetze: Download + Index (SQLite/FTS5)
src/vg_live.py        Gerichtspraxis: Live-Abfrage entscheidsuche-ES
src/server.py         FastMCP-Server mit allen 7 Tools (stdio + HTTP)
src/http_app.py       schlanker WSGI-Adapter: GET / = Landing-Page, POST /mcp = MCP
passenger_wsgi.py     Einstieg für gehosteten Betrieb (LiteSpeed/Passenger)
static/               Landing-Page + Assets
```

## Lokal betreiben (stdio, Claude Desktop)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python src/belex_source.py --live    # Gesetzes-Index bauen (~3 Min)
python src/server.py                 # stdio-Server
```

Refresh der Gesetze (die BSG ändert am 1. des Monats): denselben `--live`-Befehl
erneut ausführen (z. B. als Monats-Cron).

## Lizenz

Code: [MIT](LICENSE). Gesetze und Gerichtsentscheide sind amtliche Werke
(Art. 5 URG) und nicht urheberrechtlich geschützt. belex ist **kein amtliches
Angebot** des Kantons Bern.
