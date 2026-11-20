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
> ist allein die amtliche Fassung ([www.belex.sites.be.ch](https://www.belex.sites.be.ch)
> bzw. das Original des Gerichts). Im Zweifel immer juristisch prüfen lassen.

## Tools

| Tool | Bestand | Zweck |
|------|---------|-------|
| `belex_search` | Gesetze | Erlasse per Stichwort finden (`keyword`, gleichwertig `query`) |
| `belex_get_by_bsg` | Gesetze | Erlass per BSG-Nummer (z. B. `721.0` oder `731.2-1`) mit Volltext |
| `belex_get_article` | Gesetze | Einzelnen Artikel herausschneiden |
| `belex_index_info` | Gesetze | Datenstand (Anzahl, Quelle, Indexdatum) |
| `vg_search` | Urteile | VGer-Urteile per Stichwort im Volltext (`keyword`, gleichwertig `query`) |
| `vg_get_by_nummer` | Urteile | Urteil per Geschäftsnummer (z. B. `100.2022.280`) |
| `vg_index_info` | Urteile | Datenstand (Anzahl, neuester Entscheid) |

### BSG-Nummern

Ziffern mit Punkten und optional einem Anhang nach Bindestrich:
`Ziffern(.Ziffern)*(-Ziffern)?`, immer dreistellig beginnend. Von den 712
Erlassen im Index tragen 96 einen solchen Anhang, etwa `731.2-1` (IVöB) oder
`105.235.1-12`. Andere Zeichen als `0-9`, `.` und `-` kommen nicht vor.
Ein vorangestelltes `BSG` und typografische Striche werden vor der Prüfung
geglättet.

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
static/               Landing-Page, Assets, robots.txt
```

## Grenzen am öffentlichen Endpunkt

Der Endpunkt ist offen, also bremsen ein Minutenfenster und zwei
Tageskontingente. Gezählt wird nach `REMOTE_ADDR`, nicht nach
`X-Forwarded-For`: den Header kann jeder setzen. Die Tageskontingente hängen
bei IPv6 am **/64**, nicht an der Einzeladresse, weil ein IPv6-Client darin
beliebig wechseln kann und der gemessene Crawler genau das tat.

| Fenster | Grenze | Gilt für |
|---------|--------|----------|
| Minutenfenster | 30 Anfragen pro 60 Sekunden | jede Anfrage, auch GET |
| Tageskontingent «teuer» | 25 Aufrufe pro Tag | `vg_search`, `vg_get_by_nummer`, `vg_index_info` |
| Tageskontingent «billig» | 300 Aufrufe pro Tag | die vier `belex_*`-Tools |

Die Trennung folgt den Kosten: die `vg_*`-Tools fragen live die Elasticsearch von
entscheidsuche.ch ab, also fremde Infrastruktur eines gemeinnützigen Vereins
(`vg_index_info` kostet dort zwei Abfragen pro Aufruf). Die `belex_*`-Tools lesen
nur die lokale SQLite.

Dimensioniert ist das an gemessener Nutzung (Zugriffslog 31.10. bis 20.11.2026),
nicht an einem Schätzwert: eine echte Client-Adresse kam auf höchstens 4
Werkzeugaufrufe pro Tag (p95 3, Median 1, über 442 Adresstage), der auffälligste
der im November aufgetauchten Crawler auf 32 aus einem einzigen /64. Ein
naheliegendes Tageslimit von 200 hätte ihn nicht erwischt.

`initialize`, `tools/list`, `ping` und Notifications zählen nicht mit, damit das
Protokoll-Handshake echter Clients nie am Kontingent scheitert. Ein erschöpftes
Kontingent antwortet mit **HTTP 200** und einem `tools/call`-Ergebnis mit
`isError` plus Klartextbegründung, nicht mit 429: ein unerwarteter Statuscode hat
im August 2026 bei `mcp-remote` einen Reconnect-Sturm ausgelöst.

Die erste Abweisung pro Schlüssel, Tag und Gruppe schreibt eine Zeile ins
`stderr.log`. So bleibt nachprüfbar, ob je echte Nutzung getroffen wurde.

Die Zähler liegen in `data/rate.sqlite` (Tabellen `hits` für das Minutenfenster,
`quota` für die Tage), weil LiteSpeed mehrere `lswsgi`-Prozesse fährt und ein
Zähler im Prozessspeicher darum nicht greift. Beide Tabellen räumen alte Zeilen
selbst weg.

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
