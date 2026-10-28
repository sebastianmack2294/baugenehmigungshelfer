# Baugenehmigungshelfer mit täglicher Gesetze-Prüfung

- docs/index.html: das Tool (mit Button "Jetzt Gesetze prüfen")
- docs/pruefbericht.html: interner Bericht mit den genauen Änderungen (wird vom Skript erzeugt)
- check.py, extract.js: Prüfskript, liest Quellen und Werte direkt aus dem Tool
- .github/workflows/check.yml: startet die Prüfung täglich bei GitHub

Einrichtung: Repository bei GitHub anlegen, alles hochladen, unter Settings > Pages den Ordner "docs" veröffentlichen,
unter Actions den Workflow "Gesetze prüfen" einmal von Hand starten.
