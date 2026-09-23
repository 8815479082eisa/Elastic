# Elastic Observability Demo – shopdemo

Demo-Projekt zur KI-gestützten Incident-Analyse mit Elasticsearch.
Ein Log-Generator erzeugt synthetische Logs im Elastic Common Schema (ECS) für ein
fiktives Shopsystem und kann gezielt Störungen einspielen. Ein MCP-Server stellt
einem LLM (z. B. Claude Desktop) Werkzeuge bereit, mit denen es die Ursache einer
Störung direkt aus den Logs ermitteln kann.

## Komponenten

| Verzeichnis / Datei | Beschreibung |
|---|---|
| `docker-compose.yml` | Lokaler Elastic Stack (Elasticsearch + Kibana, Single Node, Security aktiv) |
| `log_generator/generator.py` | Erzeugt ECS-Logs für die Services `checkout`, `payment`, `auth` und `inventory` |
| `mcp_server/` | MCP-Server mit Werkzeugen zur Log-Analyse |
| `docs/log-schema.md` | Log-Schema, Felder und Beschreibung der Incident-Szenarien |

## Voraussetzungen

- Docker und Docker Compose
- Python 3.10 oder neuer

## Einrichtung

1. Repository klonen und Umgebungsvariablen anlegen:

   ```bash
   git clone https://github.com/8815479082eisa/Elastic.git
   cd Elastic
   cp .env.example .env
   ```

   Danach in `.env` die Werte eintragen, zum Beispiel:

   ```env
   STACK_VERSION=9.5.4
   ELASTIC_PASSWORD=ein-sicheres-passwort
   KIBANA_PASSWORD=ein-anderes-passwort
   ES_PORT=9200
   ES_URL=http://localhost:9200
   ES_USER=elastic
   ```

2. Elastic Stack starten:

   ```bash
   docker compose up -d
   ```

   Elasticsearch ist anschließend unter `http://localhost:9200` erreichbar,
   Kibana unter `http://localhost:5601` (Login mit `elastic` und `ELASTIC_PASSWORD`).

3. Python-Umgebung einrichten:

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux / macOS
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

## Logs erzeugen

Der Generator simuliert Anfragen über alle vier Services. Alle Logs einer Anfrage
teilen sich dieselbe `trace.id`. Die Logs werden rückwirkend für ein Zeitfenster
erzeugt und in den Data Stream `logs-shopdemo-default` geschrieben.

```bash
# Normalbetrieb, Ausgabe auf der Konsole
python log_generator/generator.py --scenario none --output stdout

# Incident in eine Datei schreiben
python log_generator/generator.py --scenario db_timeout --output file --file logs.ndjson

# Incident direkt in Elasticsearch schreiben
python log_generator/generator.py --scenario memory_leak --output es
```

Für `--output es` liest der Generator die Zugangsdaten aus Umgebungsvariablen
(nicht aus `.env`): `ES_PASSWORD` ist Pflicht, `ES_URL` und `ES_USER` sind optional.

```powershell
# PowerShell
$env:ES_PASSWORD = "ein-sicheres-passwort"
```

### Parameter

| Parameter | Standard | Beschreibung |
|---|---|---|
| `--scenario` | `none` | `none`, `db_timeout`, `auth_cert_expired` oder `memory_leak` |
| `--hours` | `2` | Länge des rückwirkend erzeugten Zeitfensters in Stunden |
| `--incident-minutes-ago` | `30` | Beginn des Incidents in Minuten vor jetzt |
| `--rate` | `20` | Anfragen pro Minute |
| `--output` | `stdout` | `stdout`, `file` oder `es` |
| `--file` | `logs.ndjson` | Zieldatei bei `--output file` |
| `--seed` | – | Zufalls-Seed für reproduzierbare Läufe |

### Incident-Szenarien

| Szenario | Ursache | Muster in den Logs |
|---|---|---|
| `db_timeout` | Connection-Pool der Payment-Datenbank erschöpft | Kaskade: `payment` → `checkout` (gemeinsame `trace.id`) |
| `auth_cert_expired` | Abgelaufenes TLS-Zertifikat im Auth-Service | Alle Services fallen gleichzeitig mit 401 aus |
| `memory_leak` | Speicherleck im Inventory-Service | Steigender Speicherverbrauch, OutOfMemory, Neustart |

Kein Feld verrät das eingespielte Szenario. Die Ursache muss, wie im echten
Betrieb, aus den Logdaten selbst ermittelt werden. Details stehen in
[docs/log-schema.md](docs/log-schema.md).

## MCP-Server

Der MCP-Server (`mcp_server/server.py`) liest die Zugangsdaten aus der `.env` im
Projektverzeichnis und bietet drei Werkzeuge an:

| Werkzeug | Zweck |
|---|---|
| `get_error_stats` | Zählt Fehler pro Service, pro Fehlertyp und im Zeitverlauf – Einstiegspunkt der Analyse |
| `find_correlated_errors` | Zeitleiste der Fehler und Fehlerketten über `trace.id`, um Ursache und Folgefehler zu trennen |
| `search_logs` | Sucht einzelne Logzeilen mit Filtern nach Service, Level, Zeitraum und Freitext |

Personenbezogene Felder (`user.email`, `source.ip`) werden nicht an das LLM
zurückgegeben (DSGVO).

Verbindung testen:

```bash
python mcp_server/es_client.py
```

### Einbindung in Claude Desktop

In `claude_desktop_config.json` eintragen (Pfade anpassen):

```json
{
  "mcpServers": {
    "elastic-observability": {
      "command": "D:\\Elastic\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Elastic\\mcp_server\\server.py"]
    }
  }
}
```

Nach einem Neustart von Claude Desktop kann man zum Beispiel fragen:
„Analysiere die Fehler der letzten Stunde und finde die Ursache.“

## Hinweis

Die Konfiguration ist nur für die lokale Entwicklung gedacht: HTTP ohne TLS,
Ports nur an `127.0.0.1` gebunden. Die Datei `.env` enthält Passwörter und wird
nicht ins Repository übernommen.
