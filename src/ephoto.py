#!/usr/bin/env python3
"""
Plustek ePhoto Z300 (07b3:143d) — low-level device access.

Protocol recovered from the vendor's macOS ScanApi.framework; see
docs/PROTOCOL.md for the derivation.

  read reg:   bmRequestType 0xC0, bRequest 0xA0
              wValue  = 0x8E | (reg & 0x100)
              wIndex  = ((reg & 0xFF) << 8) | 0x22
              wLength = 2   -> (value, 0x55)

  write reg:  bmRequestType 0x40, bRequest 0xA0
              wValue  = 0x83 | (reg & 0x100)
              wIndex  = 0
              data    = (reg & 0xFF, value)
"""
import usb.core

VID, PID = 0x07B3, 0x143D

REQ_IN, REQ_OUT = 0xC0, 0x40
BREQUEST = 0xA0
VALUE_READ_REGISTER = 0x8E
VALUE_SET_REGISTER = 0x83
INDEX_READ_TAG = 0x22
REPLY_SENTINEL = 0x55

# --- named register bits, extracted from the vendor driver -----------------
# (identical across CGL125/126/128/846, so they hold regardless of which
#  chip variant this unit actually carries)
REG_MOTOR = 0x02
MTRREV = 0x04      # direction
FASTFED = 0x08     # fast feed
MTRPWR = 0x10      # motor powered
AGOHOME = 0x20     # automatic home seek
ACDCDIS = 0x40

REG_LAMP = 0x03
LAMPTIM = 0x0F     # lamp timer
LAMPPWR = 0x10     # lamp on/off
AVEENB = 0x40
LAMPDOG = 0x80     # lamp watchdog

# Register, die tatsächlich etwas auslösen können — im Unterschied zu den
# vielen reinen Parameterregistern (Schrittzahlen, Zeilenzähler, Timing), deren
# Beschreiben für sich genommen nichts bewegt.
#
#   0x0F  MOVE — der eigentliche Auslöser.  CAsic::_MOVE schreibt diesen
#         Register als ganzen Bytewert; in AdfLoadPaper ist der Wert 1.  Erst
#         dieser Schreibzugriff setzt die zuvor konfigurierte Bewegung in Gang.
#         Der Init-Block lässt 0x0e-0x10 bezeichnenderweise aus.
#   0x02  MTRPWR / AGOHOME / FASTFED / MTRREV / NOTHOME — Antriebszustand
#   0x01  im genesys-Backend das SCAN-Startbit; im Vendor-Binary gibt es für
#         dieses Register keine benannten Bit-Accessoren, seine Bedeutung ist
#         hier also unbelegt. Bis das geklärt ist: gesperrt.
MOTION_REGISTERS = {0x01, 0x02, 0x0F}

# Kommandoregister — kein Zustand, sondern Wirkung.  Der Init-Block der
# Vendor-Konfiguration lässt 0x0e-0x10 bezeichnenderweise aus: er schreibt 0x0d
# und setzt bei 0x11 fort.
#
#   0x0E  Software-Reset.  Empirisch bestätigt: ein Schreibzugriff mit 0x00
#         setzt den Chip zurück (0x03 fällt auf den Einschaltwert 0x8f).
#   0x0F  MOVE — Bewegungsauslöser (siehe MOTION_REGISTERS)
#   0x10  im selben Block ausgelassen, Bedeutung ungeklärt
COMMAND_REGISTERS = {0x0E, 0x0F, 0x10}


class DeviceError(RuntimeError):
    pass


class EPhoto:
    def __init__(self, allow_motion=False):
        self.allow_motion = allow_motion
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise DeviceError("Scanner 07b3:143d nicht gefunden")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass

    # -- register access ---------------------------------------------------
    def read_reg(self, reg, timeout=2000):
        wValue = VALUE_READ_REGISTER | (reg & 0x100)
        wIndex = ((reg & 0xFF) << 8) | INDEX_READ_TAG
        r = bytes(self.dev.ctrl_transfer(REQ_IN, BREQUEST, wValue, wIndex, 2, timeout))
        if len(r) != 2 or r[1] != REPLY_SENTINEL:
            raise DeviceError(f"unerwartete Antwort für Register {reg:#05x}: {r.hex()}")
        return r[0]

    def write_reg(self, reg, value, timeout=2000):
        if (reg & 0xFF) in MOTION_REGISTERS and not self.allow_motion:
            raise DeviceError(
                f"Register {reg:#04x} steuert die Mechanik — Schreibzugriff "
                f"gesperrt (EPhoto(allow_motion=True) zum Freigeben)")
        return self._write_raw(reg, value, timeout)

    def _write_raw(self, reg, value, timeout=2000):
        wValue = VALUE_SET_REGISTER | (reg & 0x100)
        data = bytes((reg & 0xFF, value & 0xFF))
        n = self.dev.ctrl_transfer(REQ_OUT, BREQUEST, wValue, 0, data, timeout)
        if n != len(data):
            raise DeviceError(f"Kurzschreibzugriff auf {reg:#05x}: {n}/{len(data)}")
        return n

    def update_bits(self, reg, mask, value):
        """Read-modify-write `mask` within `reg`.  Returns (before, after)."""
        before = self.read_reg(reg)
        new = (before & ~mask) | (value & mask)
        self.write_reg(reg, new)
        return before, self.read_reg(reg)

    # -- convenience -------------------------------------------------------
    def motor_is_powered(self):
        return bool(self.read_reg(REG_MOTOR) & MTRPWR)

    def lamp_is_on(self):
        return bool(self.read_reg(REG_LAMP) & LAMPPWR)

    def describe(self):
        m = self.read_reg(REG_MOTOR)
        l = self.read_reg(REG_LAMP)
        return (f"Motor  reg 0x02 = {m:#04x}  "
                f"[MTRPWR={bool(m & MTRPWR):d} AGOHOME={bool(m & AGOHOME):d} "
                f"FASTFED={bool(m & FASTFED):d} MTRREV={bool(m & MTRREV):d}]\n"
                f"Lampe  reg 0x03 = {l:#04x}  "
                f"[LAMPPWR={bool(l & LAMPPWR):d} LAMPDOG={bool(l & LAMPDOG):d} "
                f"LAMPTIM={l & LAMPTIM:#03x} AVEENB={bool(l & AVEENB):d}]")
