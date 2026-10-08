# Masta Deals Bot

Sucht Amazon.de-Angebote über die Creators API und schickt sie per Telegram.
Läuft automatisch über GitHub Actions (siehe `.github/workflows/bot.yml`).

- Einstellungen (Suchbegriffe, Rabatt, Menge, Uhrzeiten) stehen oben in `bot.py`.
- `gepostet.json` ist der Merkzettel gegen Doppelposts, `bot.log` das Protokoll. Beide pflegt der Bot selbst.
- Zugangsdaten liegen NICHT in den Dateien, sondern unter Settings -> Secrets and variables -> Actions.
- Nie eine `.env`-Datei hochladen.
