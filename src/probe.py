#!/usr/bin/env python3
"""
Plustek ePhoto Z300 (VID 07b3 / PID 143d, "M21") — read-only register probe.

Protocol recovered from the macOS ScanApi.framework (CIOUSB::ReadReg /
CIOUSB::WriteReg).  This script ONLY performs control reads; it never writes a
register and never touches the motor or lamp.

  read reg:  bmRequestType 0xC0, bRequest 0xA0,
             wValue  = 0x8E | (reg & 0x100)
             wIndex  = ((reg & 0xFF) << 8) | 0x22
             wLength = 2
"""
import sys
import usb.core
import usb.util

VID, PID = 0x07B3, 0x143D

REQ_VENDOR_IN = 0xC0
BREQUEST = 0xA0
VALUE_READ_REGISTER = 0x8E
INDEX_READ_TAG = 0x22


def open_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("Scanner 07b3:143d nicht gefunden.")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    return dev


def read_reg(dev, reg, timeout=2000):
    """Return the 2 bytes the device replies with for register `reg`."""
    wValue = VALUE_READ_REGISTER | (reg & 0x100)
    wIndex = ((reg & 0xFF) << 8) | INDEX_READ_TAG
    return bytes(dev.ctrl_transfer(REQ_VENDOR_IN, BREQUEST, wValue, wIndex, 2, timeout))


def main():
    dev = open_device()
    try:
        print(f"# {dev.manufacturer} / {dev.product} / serial={dev.serial_number}")
    except (ValueError, usb.core.USBError):
        print("# (string descriptors unreadable — missing permissions?)")
    print(f"# bcdDevice={dev.bcdDevice:#06x}\n")

    lo = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x00
    hi = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0xFF

    print("      " + " ".join(f"{c:02x}" for c in range(16)))
    for base in range(lo, hi + 1, 16):
        cells = []
        for reg in range(base, min(base + 16, hi + 1)):
            try:
                cells.append(read_reg(dev, reg)[0:1].hex())
            except usb.core.USBError as e:
                cells.append("--")
        print(f"{base:04x}: " + " ".join(cells))


if __name__ == "__main__":
    main()
