# Energiemonitor: Balena-Container

**Status:** produktiv im Langzeittest (Version 2.7.4)

Container für die Edgebox des [KWF Energiemonitors](https://github.com/Verein-Kleinwohnformen/energiemonitor-docs): Node-RED für die Messapplikation, ein lokaler MQTT-Broker und ein BLE-Scanner für Sensirion-Gadgets. Gebaut für die Energiestudie mit der HSLU und für den [Verein Kleinwohnformen](https://kleinwohnformen.ch). Der Energiemonitor ist kein Produkt, sondern eine offene Plattform zum Nachbauen.

**Du willst einen Monitor bauen?** Starte mit der [Bauanleitung](https://github.com/Verein-Kleinwohnformen/energiemonitor-docs/blob/main/docs/bauanleitung.md). Dieses Repo enthält nur den Container und die Konfigurationsvorlagen.

## Services

Definiert in [`docker-compose.yml`](docker-compose.yml):

| Service | Aufgabe |
|---|---|
| `node-red` | Messapplikation, Host-Netzwerk, Port 80, Zugriff auf GPIO, I2C und serielle Schnittstelle. Basis `debian:bookworm`, Node.js 20 (22 wurde wegen der BLE-Bibliotheken zurückgenommen) |
| `mqtt` | Mosquitto 2.0, **ohne Anmeldung**, deshalb nur auf `127.0.0.1:1883` gebunden |
| `ble-scanner` | Python-Scanner für Sensirion Smart Gadgets (SHT3x, SHT4x, MyCO2, AQI), liest BLE-Advertisements passiv und veröffentlicht über MQTT. Beendet sich sauber, wenn keine Gadgets konfiguriert sind |

Unterstützte Hardware: Seeed Edgebox RPI-200 (Raspberry Pi CM4). Ein Raspberry Pi 4 funktioniert für Selbstbauer ebenfalls (`raspberrypi4-64`).

## Konfiguration

Alles läuft über Balena-Variablen. Die vollständige Liste mit Bedeutung steht in der [Variablen-Doku](https://github.com/Verein-Kleinwohnformen/energiemonitor-docs/blob/main/docs/variablen.md).

**Pflicht pro Gerät, mit eigenem Wert:**

- `USERNAME` und `PASSWORD` für den Node-RED-Login. Ohne `PASSWORD` startet Node-RED nicht.
- `ENCRIPTION_KEY` verschlüsselt die in Node-RED gespeicherten Zugangsdaten. Der Name enthält den Tippfehler, weil Node-RED ihn so liest.

Das Gerät ist über die Balena Public Device URL erreichbar. Setze die Werte vor dem ersten Start, verwende starke, zufällige Werte und gib die URL nicht weiter. Mehr dazu in der [Sicherheits-Doku](https://github.com/Verein-Kleinwohnformen/energiemonitor-docs/blob/main/docs/sicherheit.md). Wenn du `ENCRIPTION_KEY` nachträglich änderst, gehen die gespeicherten Zugangsdaten verloren, es sei denn, du [migrierst sie](https://github.com/Verein-Kleinwohnformen/energiemonitor-docs/blob/main/docs/migration-verschluesselungsschluessel.md).

## Netzwerk

Der Hotspot `energiemonitor` und die optionale LTE-Verbindung werden über Dateien in `/mnt/boot/system-connections` des Host-OS konfiguriert. Vorlagen: [`system_connections/`](system_connections/). In `balena-hotspot` ersetzt du `<DEIN-HOTSPOT-PASSWORT>` durch ein eigenes Passwort (mindestens 12 Zeichen, pro Gerät). Schritte in der Bauanleitung, Kapitel 8.

## Deployment

1. Repo klonen.
2. Mit der [Balena CLI](https://docs.balena.io/reference/balena-cli/) anmelden (`balena login`).
3. `balena push <deine-flotte>`. Das Gerät lädt das Image selbst.

Zum lokalen Bauen: `docker-compose up --build` (die Variablen `USERNAME`, `PASSWORD` und `ENCRIPTION_KEY` müssen in der Umgebung gesetzt sein).

## Herkunft

Fork des archivierten [balena-node-red](https://github.com/balena-labs-projects/balena-node-red) (Apache-2.0, © 2016 balena Ltd.). Änderungen: eigener Node-RED-Container mit angepasster `settings.js`, Debian Bookworm, BLE-Scanner und MQTT-Broker, Ausrichtung auf die Edgebox RPI-200. Siehe [NOTICE](NOTICE).

## Weitere Repos

[Einstieg und Doku](https://github.com/Verein-Kleinwohnformen/energiemonitor-docs) · [Connector](https://github.com/Verein-Kleinwohnformen/kwf-energiemonitor-db) · [API](https://github.com/Verein-Kleinwohnformen/energiemonitor-api) · [App](https://github.com/Verein-Kleinwohnformen/energiemonitor-app)

## Lizenz

Apache License 2.0, siehe [LICENCE](LICENCE) und [NOTICE](NOTICE). Fragen: [energiemonitor@kleinwohnformen.ch](mailto:energiemonitor@kleinwohnformen.ch)
