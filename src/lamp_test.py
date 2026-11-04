#!/usr/bin/env python3
"""
Erster Schreibzugriff: Lampe ein- und wieder ausschalten.

Bewusst minimal gehalten — es wird ausschliesslich Register 0x03 beschrieben.
Der Init-Block aus ScanApi_07b3_143d.ini wird NICHT angewendet, denn er setzt
0x02 = 0x78, was MTRPWR und AGOHOME enthält und damit den Antrieb anlaufen
lassen könnte.

Ablauf:
  1. Ausgangszustand lesen und anzeigen
  2. abbrechen, falls der Motor bereits bestromt ist
  3. LAMPPWR setzen, zurücklesen, kurz warten
  4. Ausgangswert wiederherstellen — auch bei Fehler oder Strg-C
"""
import pathlib
import argparse
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from ephoto import EPhoto, DeviceError, REG_LAMP, REG_MOTOR, LAMPPWR, MTRPWR  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="Leuchtdauer (Vorgabe: 5)")
    args = ap.parse_args()

    dev = EPhoto()
    print("Ausgangszustand:")
    print(dev.describe())

    motor = dev.read_reg(REG_MOTOR)
    if motor & MTRPWR:
        sys.exit("\nABBRUCH: Motor ist bereits bestromt (reg 0x02 & 0x10). "
                 "Bitte Gerät aus- und wieder einstecken.")

    original = dev.read_reg(REG_LAMP)
    target = original | LAMPPWR
    print(f"\nSchreibe reg 0x03: {original:#04x} -> {target:#04x}  (LAMPPWR setzen)")

    try:
        dev.write_reg(REG_LAMP, target)
        back = dev.read_reg(REG_LAMP)
        print(f"zurückgelesen:     {back:#04x}   LAMPPWR={'gesetzt' if back & LAMPPWR else 'NICHT gesetzt'}")

        if not back & LAMPPWR:
            print("\nDas Bit hält nicht. Wahrscheinlich fehlt Taktversorgung oder\n"
                  "CIS-Stromversorgung — dann ist mehr Initialisierung nötig.")
        else:
            print(f"\n>>> Leuchtet die Beleuchtung? Warte {args.seconds:g} s ...")
            time.sleep(args.seconds)

        # Motorregister zur Kontrolle erneut prüfen
        m2 = dev.read_reg(REG_MOTOR)
        if m2 & MTRPWR:
            print(f"WARNUNG: reg 0x02 = {m2:#04x}, MTRPWR ist gesetzt!")

    except (DeviceError, Exception) as e:  # noqa: BLE001
        print(f"\nFehler: {type(e).__name__}: {e}")
        raise
    finally:
        try:
            dev.write_reg(REG_LAMP, original)
            print(f"\nwiederhergestellt: reg 0x03 = {dev.read_reg(REG_LAMP):#04x}")
        except Exception as e:  # noqa: BLE001
            print(f"\nWiederherstellung fehlgeschlagen: {e}")

    print("\nEndzustand:")
    print(dev.describe())


if __name__ == "__main__":
    main()
