# Reverse-Engineering-Werkzeuge

Mit diesen Skripten wurde das Protokoll aus dem macOS-Framework des Herstellers
(`ScanApi.framework`) und aus USB-Mitschnitten rekonstruiert (siehe
`docs/PROTOCOL.md`). Für den Betrieb des Scanners werden sie **nicht** gebraucht.

Das Hersteller-Binary und die Mitschnitte sind aus rechtlichen Gründen nicht
enthalten. Wer die Analyse nachvollziehen will, besorgt sich die Plustek-Software
selbst und gibt den Pfad an:

    export EPHOTO_SCANAPI=/pfad/zu/ScanApi.framework/Versions/A/ScanApi
    pip install lief capstone
    python3 tools/re/disas.py <symbol> -d

| Skript | Zweck |
|---|---|
| `disas.py` | Funktion aus dem Framework disassemblieren |
| `xref.py` | Aufrufstellen eines Symbols finden |
| `callflow.py` | Aufrufgraph einer Funktion |
| `extract_bits.py`, `extract_fields.py` | Register-Bitlandkarte und Mehrbyte-Felder extrahieren (→ `docs/bitmap.txt`, `docs/fields.txt`) |
| `extract_sequence.py` | Scansequenz aus einem USB-Mitschnitt gewinnen |
