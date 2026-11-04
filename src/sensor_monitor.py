#!/usr/bin/env python3
"""
Sensoren beobachten, während ein Blatt eingelegt wird.

Rein lesend — es wird kein einziges Register beschrieben, der Antrieb bleibt
unberührt.

Der Papiersensor ist aus CAsic::DocSensor bekannt; für PID 0x143d/0x1443 gilt
dort ausdrücklich:

    ReadReg(0x101, &v);  Dokument vorhanden  <=>  (v & 0x08) == 0

Zusätzlich wird der komplette Registerbereich 0x100-0x10F überwacht sowie der
Interrupt-Endpoint 0x83, über den das Gerät laut Konfiguration
(ButtonEventFromInterrupt=1, PollingButtonEvent=0) seine Ereignisse meldet.
"""
import pathlib
import argparse
import sys
import time

import usb.core

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from ephoto import EPhoto  # noqa: E402

WATCH = list(range(0x100, 0x110))
DOC_REG, DOC_BIT = 0x101, 0x08
EP_INTERRUPT = 0x83


def snapshot(dev):
    out = {}
    for r in WATCH:
        try:
            out[r] = dev.read_reg(r, timeout=500)
        except Exception:  # noqa: BLE001
            out[r] = None
    return out


def fmt(state):
    return " ".join("--" if state[r] is None else f"{state[r]:02x}" for r in WATCH)


def doc_present(state):
    v = state.get(DOC_REG)
    return None if v is None else not (v & DOC_BIT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--interval", type=float, default=0.05)
    args = ap.parse_args()

    dev = EPhoto()

    print("Register 0x100-0x10F werden beobachtet, Papiersensor = 0x101 Bit 0x08 "
          "(invertiert).")
    print("Nur Lesezugriffe — nichts wird geschrieben.\n")

    base = snapshot(dev)
    print("      " + " ".join(f"{r & 0xFF:02x}" for r in WATCH))
    print(f"Start {fmt(base)}   Blatt: "
          f"{'JA' if doc_present(base) else 'nein'}\n")
    print(f">>> Bitte jetzt ein Blatt einlegen. Beobachte {args.seconds:g} s ...\n")

    prev = base
    t_end = time.time() + args.seconds
    events = 0
    while time.time() < t_end:
        # Interrupt-Endpoint nicht-blockierend abfragen
        try:
            data = dev.dev.read(EP_INTERRUPT, 1, timeout=20)
            if len(data):
                print(f"[{time.strftime('%H:%M:%S')}] INTERRUPT: {bytes(data).hex()}")
                events += 1
        except usb.core.USBError:
            pass

        cur = snapshot(dev)
        if cur != prev:
            changed = [r for r in WATCH if cur[r] != prev[r]]
            print(f"[{time.strftime('%H:%M:%S')}] {fmt(cur)}")
            for r in changed:
                a = "--" if prev[r] is None else f"{prev[r]:#04x}"
                bch = "--" if cur[r] is None else f"{cur[r]:#04x}"
                extra = ""
                if r == DOC_REG:
                    extra = ("   -> BLATT ERKANNT" if doc_present(cur)
                             else "   -> Blatt weg")
                print(f"      Register {r:#05x}: {a} -> {bch}{extra}")
            prev = cur
            events += 1
        time.sleep(args.interval)

    print(f"\nFertig. {events} Ereignisse. Endzustand:")
    print(f"      {fmt(prev)}   Blatt: {'JA' if doc_present(prev) else 'nein'}")


if __name__ == "__main__":
    main()
