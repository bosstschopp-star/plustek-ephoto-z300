# Plustek ePhoto Z300 — USB-Protokoll (rekonstruiert)

Gerät: `07b3:143d`, iProduct `Photo Scanner(M21)`, `bcdDevice 7.02`

Quelle: `ScanApi.framework` aus dem offiziellen macOS-Paket
(`ePhoto Z300 V2.5.9.0.pkg`, Framework-Build 30.04.2024). Das Framework ist ein
reines Userspace-Dylib mit **unstripped C++-Symbolen** (13.905 Symbole,
2.606 eindeutig demangled) und aktivem Debug-Logging. Es existiert kein
Kernel-Treiber (kein kext) — der gesamte Gerätezugriff läuft über IOKit-
Control- und Bulk-Transfers und ist damit 1:1 auf libusb portierbar.

## 1. USB-Topologie

| Endpoint | Typ | Richtung | Zweck |
|---|---|---|---|
| `0x02` | Bulk, 512 B | OUT | Kommando-/Datenstrom zum Gerät |
| `0x81` | Bulk, 512 B | IN | Bilddaten / Antworten |
| `0x83` | Interrupt, 1 B | IN | Button-/Statusevents |

Interface 0, Class/SubClass/Protocol jeweils `0xFF` (vendor specific).
`Self Powered`, `MaxPower 10 mA`.

## 2. Control-Transfer-Basisschicht

Rekonstruiert aus `CIOUSB::ControlWrite(unsigned short, unsigned int, unsigned char*, unsigned int)`
(`0xf07e0`) und `CIOUSB::ControlRead(...)` (`0xf0270`). Beide bauen eine
`IOUSBDevRequest`-Struktur auf dem Stack:

```
ControlWrite:  bmRequestType = 0x40   ControlRead:  bmRequestType = 0xC0
               bRequest      = 0xA0                 bRequest      = 0xA0
               wValue        = arg1                 wValue        = arg1
               wIndex        = arg4                 wIndex        = arg4
               wLength       = arg2                 wLength       = arg2
               pData         = arg3                 pData         = arg3
```

Erfolgskriterium im Schreibpfad: Rückgabewert 0 **und** `wLenDone == wLength`.

## 3. Registerzugriff

Der Chip hat einen **9-Bit-Registerraum** (`0x000`–`0x1FF`). Bit 8 der Adresse
wird nicht im Datenpaket, sondern im `wValue` transportiert.

### Am Gerät verifiziert (Schreibpfad)

`src/lamp_test.py` setzt `LAMPPWR` (Register `0x03`, Bit `0x10`):
`0x8f` → `0x9f`, korrekt zurückgelesen, Ausgangswert wiederhergestellt.
**Die Beleuchtung ging tatsächlich an** — der Schreibpfad ist damit nicht nur
protokollarisch, sondern physikalisch bestätigt. Das Motorregister `0x02` blieb
durchgehend `0x00`.

Bemerkenswert: Es war *keine* vorherige Initialisierung nötig. Takt- und
CIS-Versorgung sind nach dem Einschalten offenbar bereits aktiv.

### Register schreiben — `CIOUSB::WriteReg(unsigned short reg, unsigned char val, int)` (`0xf2970`)

```
buffer[0] = reg & 0xFF
buffer[1] = val
wValue    = 0x83 | (reg & 0x100)
wIndex    = 0            (Alternativpfad im Binary: 0x8b als zweiter wValue)
wLength   = 2
-> ControlWrite(0x40, 0xA0, wValue, wIndex, buffer, 2)
```

Nach dem Transfer spiegelt der Treiber den Wert in ein Schatten-Array
(`this->[0xe0][reg * 2]`, 16 Bit pro Register).

### Register lesen — `CIOUSB::ReadReg(unsigned short reg, unsigned char* out, int)` (`0xf2350`)

```
wValue    = 0x8E | (reg & 0x100)
wIndex    = ((reg & 0xFF) << 8) | 0x22
wLength   = 2
-> ControlRead(0xC0, 0xA0, wValue, wIndex, buffer, 2)
```

Im Binary liegt jeweils ein zweites wValue/wIndex-Paar (`0x8A`, `(reg<<8)|0x0A`)
als Alternativpfad vor; der Index wird über eine Variable gewählt, die in beiden
Funktionen zunächst auf 0 gesetzt wird.

### Am Gerät verifiziert

Der Lesepfad ist gegen die echte Hardware bestätigt (`src/dump_regs.py`):
**512/512 Register gelesen, 0 Fehler, 0 instabile Werte.**

Die Antwort ist immer 2 Byte: `Byte[0]` = Registerwert, `Byte[1]` = konstant
`0x55`. Das Füllbyte eignet sich als Plausibilitätsprüfung für eine gültige
Antwort.

Zustand nach Power-On (uninitialisiert):

```
       00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f
 0000: 95 00 00 8f 00 40 00 00 00 00 80 21 00 00 00 00
 0010: 00 00 00 00 30 00 33 04 10 00 00 00 00 00 80 00
 0020: 14 00 04 00 00 08 00 00 00 00 00 00 00 00 00 00
 0030: 00 fe f1 1b 00 00 00 74 00 00 00 00 00 00 37 00
 0070: 06 08 08 0a 00 00 00 00 00 00 00 00 00 00 2a 30
 0090: 00 00 00 00 00 00 00 00 00 1f 00 3f 3f 3f 00 00
 00a0: 01 b0 00 00 00 00 00 00 00 00 00 00 00 00 00 00
 00b0: 00 00 00 00 00 00 00 00 00 00 00 00 ff 1f 00 00
 0100: f0 48 00 00 00 00 00 00 00 00 00 00 00 00 04 00
```

Der erweiterte Bereich `0x100`–`0x1FF` ist vorhanden, aber dünn belegt
(`0x100=0xF0`, `0x101=0x48`, `0x10E=0x04`); der Rest liest 0, solange das AFE
nicht mit Strom versorgt ist. Register `0x31` variiert zwischen Läufen
(`0xFC` → `0xFE`), innerhalb eines Laufs ist es stabil.

## 3a. Register-Bit-Landkarte

`CAsic` exportiert die einzelnen Registerbits als benannte Accessoren, jeweils
in zwei Überladungen:

```
CAsic::_AGOHOME(bool, int)        CAsic::_AGOHOME(int, bool, int)
CAsic::_BITSET(bool, int)         CAsic::_ACDCDIS(bool, int)
CAsic::_BUFEMPTY(bool, int)       CAsic::_BWHI(bool, int)  ...
```

Diese Namen sind identisch mit der Registerbit-Nomenklatur des SANE-Backends
`genesys` (`REG_0x02_AGOHOME`, `REG_0x04_BITSET`, `REG_0x02_ACDCDIS`, …) — ein
weiterer starker Beleg für die Genesys-Logic-Herkunft.

Jede Chipklasse (`CGL125`, `CGL126`, `CGL128`, `CGL846`) überschreibt diese
Accessoren mit ihrer chipspezifischen Register-/Bitposition (je 271 Methoden).
Daraus lässt sich die **vollständige Register-Bit-Landkarte pro Chip
automatisiert per Disassembly extrahieren** — das ist der nächste große
Arbeitsschritt.

## 3b. Objektstruktur / Chipauswahl

`CScanApi::CScanApi()` (`0x1bbd0`) bettet sämtliche Handler als Member an festen
Offsets ein — es gibt keinen Switch, die Auswahl erfolgt später per Zeiger:

| Member | Offset |
|---|---|
| `CAsic` (Basis) | `+0x00aa8` |
| `CIOUSB` | `+0x08c38` |
| `CGL846` | `+0x08c20` |
| `CGL125` | `+0x18ea0` |
| `CGL126` | `+0x18eb8` |
| `CGL128` | `+0x18ed0` |
| `CAfe` | `+0x18ee8` |
| `CWM8199` | `+0x18f08` |
| `CAD9826` | `+0x18f20` |
| `CHT82V36` | `+0x18f38` |
| `CES8140` | `+0x18f50` |

**Offen:** Welche der vier Chipklassen für PID `143D` aktiv wird. Die plists
enthalten keinen entsprechenden Schlüssel (`HT8236 = 1` wählt lediglich das AFE),
und die PID `0x143D` kommt im Codesegment nicht als Vergleichskonstante vor. Die
Auswahl ist damit entweder fest verdrahtet oder wird zur Laufzeit aus einem
Register abgeleitet.

Praktikabler Weg: die Bit-Landkarten aller vier Klassen extrahieren und
empirisch gegen die Hardware sowie gegen den Init-Block der plist prüfen — nur
eine davon ist konsistent.

## 3d. Extrahierte Register-Bit-Landkarte

`tools/extract_bits.py` extrahiert die Bitdefinitionen automatisch aus den
Chipklassen. Jeder Accessor hat eine feste Codeform: Read-Modify-Write auf ein
`uint16`-Schattenabbild (Index = Registernummer, Offset = `reg * 2`), gefolgt von
`CIOUSB::WriteRegs(reg, &shadow[reg], 1, …)` — die Registernummer steht als
Immediate in `esi` direkt vor dem Aufruf.

Ergebnis: **126 Bitdefinitionen für CGL846, 129 für CGL126, 110 für CGL128,
109 für CGL125** (`docs/bitmap.txt`), davon 49 chipübergreifend identisch.

Gegenprobe gegen SANE `genesys` — exakte Übereinstimmung:

| extrahiert | SANE `genesys` |
|---|---|
| `AGOHOME 0x02/0x20` | `REG_0x02_AGOHOME = 0x20` |
| `FASTFED 0x02/0x08` | `REG_0x02_FASTFED = 0x08` |
| `ACDCDIS 0x02/0x40` | `REG_0x02_ACDCDIS = 0x40` |
| `BITSET 0x04/0x40` | `REG_0x04_BITSET = 0x40` |
| `GMMENB 0x05/0x08` | `REG_0x05_GMMENB = 0x08` |
| `CLKSET 0x0b/0xe0` | `REG_0x0B_CLKSET = 0xe0` |
| `ENBDRAM 0x0b/0x08` | `REG_0x0B_ENBDRAM = 0x08` |

### Lampe und Motor

Beide Gruppen sind über **alle vier Chipklassen identisch** — für ihre Ansteuerung
muss die Chipfrage nicht entschieden werden.

| Bit | Register | Maske | Bedeutung |
|---|---|---|---|
| `LAMPPWR` | `0x03` | `0x10` | Lampe ein/aus |
| `LAMPDOG` | `0x03` | `0x80` | Lampen-Watchdog |
| `LAMPTIM` | `0x03` | `0x0f` | Lampen-Timer |
| `MTRPWR` | `0x02` | `0x10` | Motor bestromt |
| `MTRREV` | `0x02` | `0x04` | Laufrichtung |
| `FASTFED` | `0x02` | `0x08` | Schnellvorschub |
| `AGOHOME` | `0x02` | `0x20` | automatische Heimfahrt |

Der Init-Block setzt `02,78` und `03,20`: Motor stromlos, Lampe aus — konsistent
mit einem sicheren Ausgangszustand.

Chipabhängig sind dagegen die PWM- und Schrittregister:

| Bit | CGL125 | CGL126 | CGL128 | CGL846 |
|---|---|---|---|---|
| `MTRPWM` | `0x7d/0xc0` | `0x9b/0xc0` | `0x9b/0xc0` | `0x67/0xc0` |
| `FASTPWM` | `0x7d/0xc0` | `0x9c/0xc0` | `0x9c/0xc0` | `0x68/0xc0` |
| `STEPSEL` | `0xa0/0x07` | `0xa0/0x07` | `0xa0/0x07` | `0x60/0xe0` |
| `LINESEL` | `0x1d/0xe0` | `0x1d/0xe0` | `0x1d/0xe0` | `0x1e/0x0f` |

## 3e. Chipidentifikation

**GL846 ist ausgeschlossen.** Sein GPIO-Block liegt auf `0x6c/0x6d/0xa6/0xa8`,
bei CGL125/126/128 dagegen auf `0x31/0x32/0x33`. Die Konfiguration des Z300
belegt eindeutig Letzteres:

- Init-Block schreibt `31,00 32,02 33,0c` und fasst `0x6c`/`0x6d` nie an
- `LedReadyID = "10,1"` → GPIO10 = `0x32/0x02`; der Init setzt genau `32,02`
- `StandbyReg = "0x33,0x08"` → GPIO20 = `0x33/0x08`
- Speed-Profile schalten `31.80` (GPIO8) und `33.08` (GPIO20)

**Innerhalb von CGL125/126/128 spricht das meiste für CGL126:**

- `EPROMSEL` (`0x0a/0x08`) existiert unter den dreien nur bei CGL126 — und der
  Init setzt `0a,48`, also genau dieses Bit. Das Gerät besitzt einen SPI-Flash
  (`FlashRAM=1`, `FlashSize=64,8`, `SaveShadingToFlash=1`).
- `AUTOADF` (`0x9f/0x08`) fehlt bei CGL128, ist bei CGL125/126 vorhanden.

Das ist kein Beweis, aber übereinstimmende Indizien aus drei unabhängigen
Richtungen. Zweitkandidat ist CGL125.

## 4a. SPI-Flash und Kalibrierdaten

Chip: GigaDevice GD25-Serie, JEDEC-ID `c8 40 13` — 512 KiB SPI-NOR.

`CIOUSB::SFlash_Read` und `SFlash_RDID` verzweigen beide auf
`this->[0x12] != 0x602`. Das Feld wird über `CIOUSB::Bcd(unsigned short)` aus dem
USB-Descriptor gesetzt; dieses Gerät meldet `bcdDevice 0x0702` und nimmt daher
den einfachen Zweig ohne den memory-mapped SPI-Controller bei `0x03000000`.

### Zugriffssequenz

`wValue 0x8B` ist ein Control-OUT-Kommando, dessen Unteroperation über `wIndex`
gewählt wird; `wValue 0x8A` liest.

```
SPI_init:   ControlWrite(0x8B, wIndex=0x07, [0x01],        len=1)   # freischalten
            ControlWrite(0x8B, wIndex=0x09, uint32_le(0),  len=4)   # konfigurieren
JEDEC-ID:   ControlRead (0x8A, wIndex=0x10,               len=3)
Lesen:      ControlWrite(0x8B, wIndex=0x0B, uint32_le(a),  len=4)   # Adresse
            ControlRead (0x8A, wIndex=0x0F,               len<=0x40) # in 64-B-Blöcken
```

**`SPI_init` ist zwingend.** Ohne die Freischaltung antwortet der Flash nicht,
und die Firmware stallt den Control-Transfer nicht sauber, sondern hängt — das
Gerät verschwindet dann vom USB-Bus und muss stromlos gemacht werden. Deshalb
arbeitet `src/flash.py` mit kurzen Timeouts (700 ms) und prüft nach jedem neuen
Kommandotyp per Registerlesen, ob das Gerät noch antwortet.

### Bankaufteilung

`FlashSize = "64,8"` — 64 KiB je Bank, 8 Bänke = 512 KiB. Bank = Adresse /
`0x10000`. Ein Abtasten des gesamten Chips bestätigt die Konfigurationsangaben
`flash_addr_sheetfed_300 = "2,0x00000,0x08000"` und
`flash_addr_sheetfed_600 = "3,0x00000,0x10000"` exakt:

| Bereich | Größe | Bank | Inhalt |
|---|---|---|---|
| `0x020000` | 32 KiB | 2 | Shading 300 dpi |
| `0x030000` | 64 KiB | 3 | Shading 600 dpi |

Alles übrige liest `0xFF` (gelöscht).

### Format der Shading-Daten

32-Byte-Kopf, danach je Pixel und Kanal ein Paar aus zwei 16-Bit-Werten
(Dunkelwert, Verstärkung), **kanalverschachtelt** — Eintrag *i* gehört zu
Kanal *i mod 3*.

```
Offset 0x00  uint16 kopf[8]     kopf[6] = Auflösung in dpi
Offset 0x10  uint32 = 1
Offset 0x14  uint32 gesamt      = Pixel * 3
Offset 0x18  uint32 pixel
Offset 0x20  { uint16 dunkel; uint16 gain; } eintrag[gesamt]
```

Beide Bänke gehen exakt auf:

| | 300 dpi | 600 dpi |
|---|---|---|
| dpi / Pixel / Einträge | 300 / 2550 / 7650 | 600 / 5100 / 15300 |
| Nutzdaten | 32 + 7650·4 = 30632 | 32 + 15300·4 = 61232 |

Dunkelwerte liegen bei 0–200, Verstärkungen bei `0x4000`–`0x9c00`. Kanal 0 hat
durchgehend Dunkelwert 0.

Ausgelesene Daten dieses Geräts: `captures/shading_300dpi.bin`,
`captures/shading_600dpi.bin`.

## 4b. Papiersensor

`CAsic::DocSensor` (`0x5bdf0`) verzweigt nach Geräterevision und PID. Für
`0x143d` und `0x1443` — die beiden Modelle sind im Code fest zusammen verdrahtet —
lautet der Zweig ab `0x5be88`:

```c
ReadReg(0x101, &v);
return ((v >> 3) & 1) ? 0 : 1;      // Bit 0x08 frei  =>  Dokument liegt an
```

**Am Gerät bestätigt** (`src/sensor_monitor.py`, rein lesend):

| | ohne Blatt | mit Blatt |
|---|---|---|
| Register `0x101` | `0x48` | `0x40` — Bit `0x08` fällt ab |
| Register `0x100` | `0xf0` | `0x30` — Bits `0x80` und `0x40` fallen ab |

Register `0x100` liefert also zwei weitere Sensorbits, vermutlich die
IR-Lichtschranke an zwei Positionen (`EnableLightIRToggleScan=1`).

### Der Einzug kommt nicht vom Gerät

Während der Messung blieb der Interrupt-Endpoint `0x83` stumm, und das
eingelegte Blatt wurde **nicht** eingezogen. Beides zusammen heisst:

- Die Papiererkennung wird **gepollt**, nicht über Interrupt gemeldet.
  `ButtonEventFromInterrupt=1` bezieht sich auf den Knopf, nicht auf das Papier.
- Der automatische Einzug ist **treibergesteuert**: die Software pollt
  `DocSensor`, erkennt das Blatt und setzt daraufhin den Motor in Gang
  (`CAsic::AdfLoadPaper`, `SheetfedLoadPaperStep = 930`).

Für den Treiber ergibt sich damit die Ablaufkette
*Sensor pollen → Motor starten → scannen*. Ohne Motoransteuerung gibt es keinen
Einzug und folglich keinen Scan.

## 4c. Bewegungssteuerung

### Der Auslöser

`CAsic::_MOVE` delegiert an `CGL126::MOVE(int wert, bool schreiben, int)`:

```c
shadow[0x1e] = wert;                       // 0x1e / 2 = Register 0x0F
if (schreiben) WriteRegs(0x0f, &shadow[0x1e], 1);
```

**Register `0x0F` ist der Bewegungsauslöser**, geschrieben als ganzer Bytewert
(nicht bitweise). In `AdfLoadPaper` lautet der Aufruf `_MOVE(1, true, 0)` — der
Auslöser ist also `WriteReg(0x0F, 0x01)`.

Bezeichnend: Der Init-Block überspringt `0x0e`–`0x10`; er schreibt `0x0d` und
setzt dann bei `0x11` fort. Kommandoregister werden nicht initialisiert.

### Laufanzeige und Abbruch

`CAsic::WaitMotorStop(timeout)` pollt `_MOTORENB`, das seinerseits liest:

```c
CGL126::MOTORENB(bool refresh, int) {
    if (refresh) ReadRegs(0x101, &shadow[0x202], 1);
    return shadow[0x101] & 1;
}
```

**`MOTORENB` = Register `0x101`, Bit `0x01`.** Register `0x101` ist damit das
zentrale Statusregister:

| Bit | Bedeutung |
|---|---|
| `0x01` | Motor läuft (`MOTORENB`) |
| `0x08` | **kein** Dokument (invertiert, `DocSensor`) |
| `0x40` | in allen Messungen gesetzt |

Bestätigt durch die Sensormessung: dort las `0x101` die Werte `0x48` und `0x40`,
Bit `0x01` in beiden Fällen frei — der Motor stand.

### Ablauf des Papiereinzugs

`CAsic::AdfLoadPaper(bool)` (`0x5e7e0`), aus der Aufrufkette rekonstruiert:

1. **Zustand sichern** — Getter-Überladungen `_X(bool,int)` von `STEPTIM`,
   `ACDCDIS`, `FEEDL`, `LPERIOD`, `TGTIME`, `TBTIME`, `EXPR`, `EXPG`, `EXPB`
   sowie `GetMotorStep()`
2. **Schrittzahl berechnen** — aus `UnitDpi × (AdfStepSel+1)`, überschrieben von
   `AdfCaps` bzw. `AdfScanFastMove`; `Unit2Step()`, `SetMotorStep()`
3. **Bewegung konfigurieren** — Setter-Überladungen `_X(int,bool,int)`:
   `STEPTIM`, `ACDCDIS`, `NOTHOME`, `AGOHOME`, `MTRPWR`, `FASTFED`, `MTRREV`,
   `FMOVNO`, `FEEDL`
4. **Übertragen und starten** — `CIOUSB::WriteAllReg()`,
   `CAsic::WriteFastTable()` (Beschleunigungstabelle), dann `_MOVE(1, true, 0)`
5. **Warten** — Schleife über `AdfSensor()` mit 10 s Timeout (`0x2710`),
   danach `WaitMotorStop()`
6. **Zurücksetzen** — alle in Schritt 1 gesicherten Werte in umgekehrter
   Reihenfolge, zuletzt `SetMotorStep()`

Der Notaus ist damit klar: `MTRPWR` (Register `0x02`, Bit `0x10`) löschen.

## 4d. Mehrbyte-Registerfelder

Felder über mehrere Register folgen einem anderen Codemuster als die Einzelbits
und werden von `tools/extract_fields.py` erfasst:

```c
CRegister::SetRegs(reg, wert, count, ...);
CIOUSB::WriteRegs(reg, &shadow[reg*2], count, ...);
```

29 Felder für CGL125/126/128, 18 für CGL846 (`docs/fields.txt`). Auszug:

| Feld | CGL125/126/128 | CGL846 | Bedeutung |
|---|---|---|---|
| `LINCNT` | `0x25`–`0x27` | `0x25`–`0x27` | Zeilenzahl |
| `MAXWD` | `0x28`–`0x2a` | `0x35`–`0x37` | |
| `DPISET` | `0x2c`–`0x2d` | `0x2c`–`0x2d` | Auflösung |
| `FEEDL` | `0x3d`–`0x3f` | `0x3d`–`0x3f` | Vorschublänge |
| `PREFED` / `PSTFED` | `0x40`–`0x44` | `0x8f`–`0x93` | Vor-/Nachlauf |
| `SCANLEN` | `0x45`–`0x47` | `0x95`–`0x97` | |
| `LPERIOD` | `0x7d`–`0x7f` | `0x38`–`0x39` | Zeilenperiode |
| `DUMMY` | `0x80`–`0x81` | – | |
| `STRPIXEL` / `ENDPIXEL` | `0x82`–`0x87` | `0x30`–`0x33` | Scanfenster |
| `EXPR` / `EXPG` / `EXPB` | `0x8a`–`0x92` | `0x10`–`0x15` | LED-Belichtung je Kanal |
| `STEPNO` / `FWDSTEP` / `BWDSTEP` | `0xa4`–`0xa9` | – | Schrittzahlen |
| `FASTNO` / `FSHDEC` / `FMOVNO` | `0xaa`–`0xaf` | – | Schnellfahrt |
| `VALIDWORD` | `0x102`–`0x105` | `0x42`–`0x45` | Status |
| `SCANCNT` | `0x10b`–`0x10d` | `0x4b`–`0x4d` | Status |

Auch hier decken sich die Namen mit SANE `genesys` (`REG_LINCNT` bei `0x25`,
`REG_DPISET` bei `0x2c`). Die massiven Abweichungen bei CGL846 sind ein weiteres
Indiz gegen diese Chipvariante (siehe 3e).

## 4e. Motortabelle

`CAsic::WriteFastTable` → `GetFastTable` → `GetMotorTable` (Berechnung) →
`WriteMotorTable` (Übertragung).

**Berechnung** (`GetMotorTable`, `0x564c0`): `CRegistry::GetCurve` liest die drei
Konfigurationswerte (`Motor\CURVE = 20,3000,250`, `FastCurve = 80,2000,220`),
`GetSlope` und `GammaCurve` erzeugen daraus die Rampe, `ConvertPPS` rechnet
Geschwindigkeiten in Timerwerte um. Beteiligt sind ausserdem `_FSTPSEL`,
`_STEPSEL`, `GetVref`, `MaxStep` und `_STEPTIM`.

**Übertragung** (`WriteMotorTable`, `0x53480`): `CAsic::_MotorAddr(int)` liefert
die Zieladresse, dann schreiben `CIOUSB::WriteData` und
`CIOUSB::WriteSramData` die Tabelle in das SRAM des Chips — **über den
Bulk-Endpoint**, nicht per Control-Transfer.

### Konsequenz für die Reihenfolge

Der Bulk-Datenpfad ist damit die gemeinsame Voraussetzung für **beides**: die
Motortabelle *und* den Bilddatenstrom. Ohne ihn gibt es keine Bewegung mit
Beschleunigungsrampe und kein Bild. Er ist der nächste Arbeitsblock.

**Es gibt keine Abkürzung.** `CAsic::HomeCarriage` (`0x56150`) delegiert an
`CAsic::MoveCarriage` (`0x53960`), und diese ruft vor `_MOVE` ebenfalls
`WriteAllReg()` und `WriteFastTable()` auf. Jede Bewegung — auch die einfache
Heimfahrt — setzt die geladene Motortabelle voraus. Ein Motortest ohne den
Bulk-Datenpfad ist damit nicht möglich.

`MoveCarriage` folgt im Übrigen demselben sechsphasigen Muster wie
`AdfLoadPaper`: Zustand sichern, Schritte berechnen, Bewegungsbits setzen
(`ACDCDIS`, `NOTHOME`, `AGOHOME`, `MTRPWR`, `FASTFED`, `MTRREV`, `LPERIOD`,
`FMOVNO`, `FEEDL`), übertragen, auslösen, zurücksetzen.

## 4f. Bulk-Datenpfad

Ein Bulk-Transfer wird durch Control-Transfers vorbereitet und dann über die
Bulk-Endpoints abgewickelt. Drei Vorbereitungskommandos:

### `SetRegAddr(uint8 port)` — `0x4b660`

```
ControlWrite(wValue=0x83, wIndex=0, data=[port], len=1)
```

Derselbe `wValue 0x83` wie beim Registerschreiben, aber mit **Länge 1 statt 2**:
statt Register und Wert zu schreiben, wird nur der Adresszeiger gesetzt. So
wählt ein Bulk-Transfer sein Ziel.

### `SetupBulkLength(uint32 addr, uint32 len, uint8 flag)` — `0xf4070`

```
buffer[0..3] = addr        (uint32, little endian)
buffer[4..7] = len         (uint32)
ControlWrite(wValue=0x82, wIndex=flag, data=buffer, len=8)
```

Ist `CRegistry::BULK1024` gesetzt, wird die Länge vorher auf ein Vielfaches von
1024 abgerundet (`len & 0xFFFFFC00`), der Rest gesondert behandelt und
`FFCFG_chk_DMA_Stop()` aufgerufen.

### `SetBulkMode(uint8 a, uint8 b, uint32 c)` — `0xf3d60`

```
ControlWrite(wValue=0x82, wIndex=0, data=<8 Byte Modus>, len=8)
```

Der Modusbyte-Puffer hängt von `bcdDevice`, `IsHispeed()` und
`CRegistry::EppMode` ab; im EPP-Zweig wird zusätzlich
`WriteUsbReg(0x0f, wert)` gesetzt.

### Der eigentliche Transfer

`CIOUSB::BulkWrite` (`0xf1310`) und `BulkRead` (`0xf0fd0`) richten die
Datenmenge auf die Paketgröße aus — `0x200` (512 Byte) bei High-Speed, sonst
`0x40` — und schreiben bzw. lesen dann auf Endpoint `0x02` respektive `0x81`.

Abgeschlossen wird mit `EndWriteTransaction()` beziehungsweise
`EndReadTransaction(int*)`; letzteres prüft per Control-Read, ob der
Transaktionszähler auf null steht (`m_TransactionBytes != 0` ist ein Fehlerfall).

### Einordnung

`wValue 0x82` ist in SANE `genesys` als **`VALUE_BUFFER`** definiert, und
`gl843_bulk_write_data` dort arbeitet nach demselben Muster: 8-Byte-Kopf per
Control-Transfer, danach der Nutzdatenblock über den Bulk-Endpoint. Die
Übereinstimmung mit der Genesys-Logic-Familie setzt sich also bis in den
Datenpfad fort.

### Lesepfad für diese Geräterevision

`CIOUSB::ReadData` und `StartReadTransaction` verzweigen auf `bcdDevice >= 0x600`.
Dieses Gerät (`0x702`) nimmt jeweils den kurzen Zweig — **`SetRegAddr` und
`SetBulkMode` entfallen vollständig**:

```c
ReadTransaction(buf, &len, addr):
    while (rest > 0):
        StartReadTransaction(rest, addr)   // -> SetupBulkLength(addr, rest, 0)
        ReadRawData(ptr, &rest)            // -> BulkRead auf EP 0x81
        EndReadTransaction(0)
```

Konkret also:

```
ControlWrite(wValue=0x82, wIndex=0, u32(addr) + u32(len), len=8)
BulkRead(EP 0x81, len)                       # auf Vielfaches von 512 aufgefüllt
ControlWrite(wValue=0x8D, wIndex=0, [0x00], len=1)
```

`EndReadTransaction` pollt anschliessend in bis zu 12 Durchläufen per
Control-Read, ob der Transaktionszähler auf null steht.

### Am Gerät verifiziert

`src/bulk.py` liest ab Adresse 0 — der Transfer läuft durch, das Gerät bleibt
in jedem Schritt ansprechbar. Die gelesenen Daten sind **das Registerfile
selbst** und damit ein Selbsttest der Rahmung:

```
blob[i] == reg[(i//2)*2 + addr]        512/512 Byte exakt
```

`addr` wählt das Startregister; geliefert werden `addr`, `addr+2`, `addr+4`, …,
jedes über zwei Bytes dupliziert. Geprüft für `addr` = 0, 1, 2, 4 — jeweils
64/64 Byte exakt. Bei `addr=1` beginnt die Ausgabe korrekt mit Register `0x01`,
gefolgt von `0x03` und `0x05`.

Der Bulk-Datenpfad ist damit vollständig bestätigt: Control-Setup, Endpoint-
Transfer und Abschluss funktionieren wie rekonstruiert.

### Schreibpfad für diese Geräterevision

`CIOUSB::WriteSramData` verzweigt ebenfalls auf `bcdDevice >= 0x600` und ist
dort genauso schlank wie der Lesepfad — die Variante mit `SetRegAddr(0x28)`,
`SetBulkMode`, `WriteReg(0x5b, …)` und `EndWriteTransaction` gilt nur für
`0x300`–`0x5ff`:

```
ControlWrite(wValue=0x82, wIndex=1, u32(addr) + u32(len), len=8)
BulkWrite(EP 0x02, daten)
```

`wIndex` unterscheidet die Richtung: **0 = lesen, 1 = schreiben.**

**Am Gerät verifiziert** (`src/bulk.py`): 512 Byte Testmuster nach
`0x10010000` geschrieben, 512/512 Byte exakt zurückgelesen, Originalinhalt
anschliessend vollständig wiederhergestellt. Das Gerät blieb in jedem Schritt
ansprechbar.

## 4g. Speicherkarte des CGL126

Aus `CGL126::MotorAddr` / `ImageAddr` / `GammaAddr` / `ShadingAddr`:

| Bereich | Adresse |
|---|---|
| Motortabelle 0–4 | `0x10000000`, `0x10004000`, `0x10008000`, `0x1000C000`, `0x10010000` |
| Shading | `0x10014000` |
| Gamma R / G / B | `0x01000000` / `0x01000200` / `0x01000400` |
| Bildpuffer | `0x840`, `0x1080`, `0x2100`, `0x4200` — je nach `DPIHW` |
| Registerfile | ab `0x0` (siehe Lesepfad) |

Der niedrige Adressbereich ist also der Register-/FIFO-Raum, `0x01000000` die
Gamma-Tabellen, `0x10000000` das grosse SRAM. Ein Schreibzugriff auf niedrige
Adressen träfe Register — beim Erproben entsprechend Vorsicht.

### Aufrufende Schichten

| Funktion | Zweck |
|---|---|
| `WriteSramData(addr, buf, len)` | Motortabelle und Gamma-Tabellen ins SRAM |
| `WriteData` / `WriteRawData` | allgemeiner Schreibpfad |
| `ReadData` / `ReadRawData` | Bilddaten |
| `WriteTransaction` / `ReadTransaction` | Klammer aus Start…Ende |

## 5. Artefakte aus der Windows-Installation

Unter `C:\ProgramData\Plustek\` liegen Aufzeichnungen eines echten Scanvorgangs
vom 25.12.2025 — Bodenprobe statt Rekonstruktion. Kopien in
`vendor/reginfo/` und `vendor/tables/`.

### Registerabzüge

`M11U\RegInfo_MASTER_*.txt` — je 288 Register (`0x001`–`0x120`) an drei Punkten
des Ablaufs:

| Datei | Zeitpunkt |
|---|---|
| `RegInfo_MASTER_Register.txt` | nach der Initialisierung |
| `RegInfo_MASTER_SetScanParam.txt` | nach Setzen der Scanparameter |
| `RegInfo_MASTER_StartScan2_Page0.txt` | beim Scanstart |

**Init → SetScanParam** (26 Register): Takt `0x0b` `0x6c`→`0x4c`, Belichtungs-
und Timingregister `0x52`–`0x57`, `0x5a`–`0x5b`, `LPERIOD` (`0x7d`–`0x7f`) auf
`00 0b b8` = 3000 — identisch mit `"7d"=hex:00,0b,b8` aus dem **Speed4**-Profil
der INI. Der protokollierte Scan lief also mit 300 dpi.

**SetScanParam → StartScan2** (50 Register) — der eigentliche Start:

| Register | Wert | Bedeutung |
|---|---|---|
| `0x01` | `0x22` → `0x23` | Bit `0x01` gesetzt — das SCAN-Startbit |
| `0x02` | `0x78` → `0x50` | `MTRPWR`+`ACDCDIS`; `AGOHOME`/`FASTFED` gelöscht |
| `0x03` | `0x20` → `0x10` | `LAMPPWR` gesetzt |
| `LINCNT` | `0x002a3d` | 10813 Zeilen |
| `DPISET` | `0x04b0` | 1200 |
| `STRPIXEL` / `ENDPIXEL` | 125 / 2675 | 2550 Pixel |
| `STEPNO` / `FASTNO` / `FMOVNO` | `0x17` / `0x17` / `0xff` | |

Damit ist auch Register `0x01` geklärt, für das es im Binary keine benannten
Bit-Accessoren gibt: **Bit `0x01` ist das Scan-Startbit**, analog zu
`REG_0x01_SCAN` im SANE-`genesys`-Backend.

Der Block `0x0e1`–`0x0f8` enthält eine arithmetische Folge mit Schrittweite
`0x0A9E` (2718) in Paaren (n, n+1) — vermutlich Segmentpositionen des CIS.

### Motortabellen

`Debug_Logs\ScanAPI_DEBUGLOG\apiRead.log` (1,6 MB) protokolliert die berechneten
Tabellen **elementweise** (`CAsic::WriteScanTable m_ScanTable[i] = v`). 960
Einträge, daraus 80 Tabellen à 12 Werte, davon vier verschiedene:

| Tabelle | Werte | Häufigkeit |
|---|---|---|
| Scan | `25641, 5104, 3866, 3287, 2929, 2679, 2489, 2340, 2219, 2117, 2030, 1953` | 64× |
| Vorschub | `38461, 11000 ×11` | 14× |
| | `25641, 9000 ×11` | 1× |
| | `38461, 27500 ×11` | 1× |

Die Werte sind **Mikrosekunden pro Schritt** — `1e6/25641 = 39,0` und
`1e6/1953 = 512,0` gehen glatt auf. Das ist die Ausgabe von
`CAsic::ConvertPPS`. Die Scantabelle beschleunigt von 39 auf 512 Schritte/s.

Gespeichert als `vendor/tables/scan_tables.json`.

**Damit entfällt die Rekonstruktion der Kurvenmathematik** in `GetMotorTable`
für den ersten Motorversuch — die fertigen Tabellen liegen vor.

### Scanparameter

Aus denselben Protokoll (`CScan--StartScan1()`):

| dummy | startpixel | endpixel | dpiset | Pixel | Zweck |
|---|---|---|---|---|---|
| 35 | 125 | 2675 | 1200 | 2550 | 300 dpi |
| 35 | 215 | 5315 | 1200 | 5100 | 600 dpi |
| 64 | 560 | 10488 | 300 | 9928 | Kalibrierdurchlauf |

`lperiod`: 3000, 5500, 12000.

**Kreuzvalidierung:** Die Pixelzahlen 2550 und 5100 stimmen exakt mit den
Kopfdaten der aus dem SPI-Flash gelesenen Shading-Tabellen überein (Abschnitt
4a) — zwei völlig unabhängige Quellen, dieselben Werte.

## 6. Bewegung — am Gerät verifiziert

`src/motor_test.py` löst eine echte, hörbare Motorbewegung aus. Der Weg dorthin
ging über zwei falsche Annahmen, die beide durch Messung widerlegt wurden.

### Was die Bewegung tatsächlich steuert

| Element | Wert |
|---|---|
| Timerwerte je Schritt | Motorslots `0x1000C000` und `0x10010000` |
| Schrittzahl | **`FASTNO`** (`0xaa`–`0xab`) |
| Antrieb bestromt | `MTRPWR` (`0x02` Bit `0x10`), zusammen mit `ACDCDIS` → `0x02 = 0x50` |
| Auslöser | `WriteReg(0x0F, 0x01)` |
| Laufanzeige | `0x101` Bit `0x01` (`MOTORENB`) |
| Nach dem Lauf | `0x101` Bit `0x20` gesetzt |

### Die beiden Irrwege

**`FEEDL` begrenzt diese Bewegung nicht.** Aus dem Vendorlauf übernommen war
`FEEDL = 1`; eine Erhöhung auf 400 änderte die Laufzeit nicht (60 → 80 ms).

**Die Tabellenlänge begrenzt sie auch nicht.** `WriteMotorTable` schreibt zwar
`steps * 2` Byte, aber eine auf 600 Einträge verlängerte Tabelle lief genauso
kurz.

Der Fehler lag woanders: `MoveCarriage` ruft vor `_MOVE` **`WriteFastTable()`**
auf, nicht `WriteScanTable()`. Die beiden schreiben verschiedene Slots:

```
WriteScanTable → WriteMotorTable(index 1,2,3) → _MotorAddr(0,1,2) = 0x10000000 / 4000 / 8000
WriteFastTable → WriteMotorTable(index 4,5)   → _MotorAddr(3,4)   = 0x1000C000 / 10000
```

Ich hatte in die Scan-Slots geschrieben; in den Schnellfahr-Slots stand noch
das Zufallsmuster vom Einschalten. Nach der Korrektur stieg die Laufzeit auf
0,25 s — und diese Zahl verriet den letzten Parameter:

```
0,25 s / 11000 µs je Schritt = 23 Schritte  =  FASTNO
```

Gegenprobe: `FASTNO = 200` ergab 1,81 s Laufzeit und eine deutlich hörbare
Bewegung des Papiereinzugs.

### Ablauf

```
1. Tabelle (uint16 je Schritt) in die Slots 0x1000C000 und 0x10010000,
   auf 1024 Byte aufgerundet
2. Bewegungsfelder setzen, insbesondere FASTNO = gewünschte Schrittzahl
3. reg 0x02 = MTRPWR | ACDCDIS
4. reg 0x0F = 1
5. 0x101 Bit 0x01 pollen bis 0, mit Timeout
6. reg 0x02 = 0, reg 0x0F = 0
```

Die verwendete Tabelle `[38461, 11000 ×n]` stammt aus dem Vendor-Debuglog
(Abschnitt 5) — 11000 µs je Schritt entsprechen rund 91 Schritten pro Sekunde.

## 7. Scan — funktionsfähig

Der erste vollständige Scan unter Linux gelang durch **Wiedergabe der
mitgeschnittenen Vendor-Sequenz** (`src/replay_scan.py`). Grundlage sind zwei
USBPcap-Aufzeichnungen echter Windows-Scans, aus denen
`tools/extract_sequence.py` eine abspielbare Befehlsfolge erzeugt:
249 Schritte, davon 107 Registerschreibvorgänge, 12 Kommandos, 7 Downloads und
123 Bildblöcke.

### Was der Mitschnitt korrigiert hat

| Punkt | vorher (aus dem Mac-Binary) | tatsächlich |
|---|---|---|
| `bRequest` | `0xA0` | `0x04`, für Status `0x0c` — `0xA0` funktioniert aber ebenfalls |
| Registerschreiben | einzeln | Blöcke bis 32 Paare je Transfer |
| Bildabruf | ein BUFSET über die Gesamtlänge | **je Block ein neuer BUFSET**, erster mit `wIndex 8`, dann `wIndex 0` |
| Slope-Tabelle | 12 Werte, Endwert 1953 | **256 Werte, Plateau 1500 ab Index 22** |

Der letzte Punkt erklärt den zuvor viel zu langsamen Motor. Mit der echten
Tabelle stimmt die Scangeschwindigkeit exakt mit dem Windows-Treiber überein
(vom Nutzer bestätigt).

### Ablauf

```
Init + Speed-Profil (Registerblöcke)
Download Shading   31080 B -> 0x10014000
Download Slope   3x  512 B -> 0x10000000 / 0x10004000 / 0x10008000
Download Gamma   3x  512 B -> 0x01000000 / 0x01000200 / 0x01000400
reg 0x01 = 0x23      (SCAN-Bit)
reg 0x0f = 0x01      (Auslöser)
123x:  BUFSET(0x10000000, 260100, wIndex 8 bzw. 0)  +  Bulk-IN 260100 B
reg 0x02 = 0x40, LINCNT = 0, cmd 0x8d, reg 0x01 = 0x22, reg 0x02 = 0x50
```

Ergebnis: 31.908.150 Byte = **4171 Zeilen à 7650 Byte** (2550 Pixel, RGB8),
gelesen in 3,8 s.

**Wichtig:** Der Lesepuffer muss auf ein Vielfaches von 512 aufgerundet werden.
Eine Anforderung von exakt `0xFFC0` (wie im Vendorcode) führt an libusb zu
`ERRNO 75 Overflow`, weil das Gerät ein volles Paket mehr sendet.

## 8. Bildaufbereitung

Zwei Artefakte im Rohstrom, beide am eigenen Scan vermessen
(`src/postprocess.py`):

### Segmentlücken

Je Zeile stehen **Nullbyte-Paare an den Offsets 88/89, 214/215, 340/341, …** —
Periode 126 Byte = 42 Pixel. Betroffen sind Grün und Blau des Pixels
`p ≡ 29 (mod 42)`; Rot ist intakt. Das sind die Grenzen der Sensorsegmente.

Das Vendor-Referenzbild ist ebenfalls 2550 Pixel breit und zeigt dort keine
Artefakte — der Hersteller entfernt die Spalten also nicht, sondern füllt sie.
`fill_segment_gaps` interpoliert Grün und Blau aus den Nachbarpixeln.

### Kanalversatz

Die drei Farbzeilen des CIS sitzen räumlich versetzt. Durch Rastersuche über ein
Farbsaum-Mass bestimmt: **R −6, B +6 Zeilen** gegenüber Grün. Das senkt den Saum
von 23,7 auf 3,65 — eine Verbesserung um 85 %. Passt zu `LINESPACE = -24` bei
halber Auflösung (300 statt 600 dpi).

### Orientierung

Der Sensor liefert die **Spalten gespiegelt** — genau das besagt
`HorizontalMirror = 1` in der Gerätekonfiguration. Die Zeilenrichtung stimmt
bereits. Am Testblatt bestätigt; das Ergebnis deckt sich mit der Vendor-Ausgabe.

Eine 180-Grad-Drehung wäre falsch: sie beseitigt zwar die Spiegelung, dreht das
Bild aber zusätzlich auf den Kopf.

### Beschnitt

`trim_dark` schneidet bei der halben Papierhelligkeit — der Kantenübergang
dauert nur etwa acht Zeilen. Ein fester Zusatzbeschnitt ist **nicht** nötig:
`DiscardLine = 54` aus der Konfiguration meint zu verwerfende Zeilen beim
Sensorauslesen, nicht einen Beschnitt des fertigen Bildes. So getrimmt ergibt
eine A4-Seite exakt 2550 × 3513 Pixel = 216 × 297 mm.

### Shading aus dem Gerät

Der Vendor lädt einen umgerechneten Kalibrierblock (nur die ersten 354 Byte
stimmen mit dem Flash überein). Die **rohen Flash-Daten funktionieren direkt und
liefern ein saubereres Bild** — die im Vendor-Durchlauf sichtbare defekte
Spalte verschwindet damit. Der Treiber liest sie zur Laufzeit aus Bank 2
(300 dpi) bzw. Bank 3 (600 dpi).

### Ergebnis

`captures/scan_final.png` — 2550 × 3513, sauber, ohne Herstellermaterial
erzeugt.

## 9. 600 dpi

Die Sequenz stammt aus einem **Registerprotokoll** des Vendor-Treibers
(`docs/captures/regwrites_600dpi.txt`, 355 Ereignisse), nicht aus einem
USB-Mitschnitt. `tools/parse_regwrites.py` überführt sie in dieselbe
Schrittform. Zur Kontrolle wurde derselbe Parser auf das 300-dpi-Protokoll
angewandt — er liefert exakt die 4171 Zeilen aus dem pcap.

| | 300 dpi | 600 dpi |
|---|---|---|
| Schritte / Leseblöcke | 249 / 123 | 345 / 245 |
| Blockgröße | 260100 B | 520200 B (je 34 Zeilen) |
| Breite / Stride | 2550 / 7650 | 5100 / 15300 |
| Zeilen roh | 4171 | 8304 |
| Shading-Download | 31080 B (Flash-Bank 2) | 62168 B (Bank 3) |
| Motorrampe | `25641`, Kurve, Plateau 1500 | `38461`, dann konstant 11000 |

Der Ablauf ist identisch: Scanstart `01=23`, Auslöser `0f=01`, Abschluss
`02=40` / `02=50` / `01=22`.

### Am Gerät gemessen, nicht angenommen

**Kanalversatz verdoppelt sich**: R −12, B +12 Zeilen (300 dpi: −6/+6).

**Segmentlücken gibt es bei 600 dpi nicht.** Die Vermutung, die Periode
verdopple sich von 42 auf 84 Pixel, war falsch: gemessen sind es null Lücken,
der Sensor liefert lückenlos. Ein Füllen hätte dort echte Bildpunkte durch
Interpolation ersetzt. `GAPS` enthält deshalb nur einen Eintrag für 2550 Pixel;
`gap_columns` liefert für andere Breiten eine leere Liste.

Ergebnis: 5100 × 7020 Pixel = 216 × 297 mm.

## 10. Gelöst: senkrechte Farbbänder bei Farbvorlagen

> **Lösung (2026-10-21):** Das Shading-SRAM ist blockweise organisiert —
> je **42 Pixel × 12 Byte = 504 Byte Nutzdaten + 8 Byte Füller = 512 Byte**,
> der letzte Block verkürzt (300 dpi: 60 × 512 + 30 × 12 = 31080 Byte,
> 600 dpi: 121 × 512 + 18 × 12 = 62168 Byte). Der Vendor kopiert die
> Kalibrierdaten aus Flash-Bank 2/3 unverändert in dieses Format; in zwei
> Mitschnitten stimmen alle 61 Blöcke Byte für Byte mit dem Flash überein.
> Lückenlos geschrieben verschiebt sich die Kanalzuordnung um 8 Byte je Block
> (= 2 Kanäle), also rotiert sie alle 42 Pixel und ist nach 3 Blöcken wieder
> in Phase — genau die beobachteten Bänder. Die Zuordnung „Bildspalte =
> Eintrag × 2550/2590“ unten ist die lineare Näherung dieses Schemas.
> Umgesetzt in `src/tables.py` und `sane/ephoto.c`. Der Rest dieses
> Abschnitts dokumentiert die Untersuchung vor der Lösung; einige der dort
> genannten Annahmen (Vendor berechnet die Kalibrierung, Dunkelwerte im Flash
> = 0) haben sich als falsch erwiesen.


Auf farbigen Mitteltonvorlagen entstehen senkrechte Bänder mit abwechselnder
Farbgewichtung. Auf weissem Papier und in Textscans unsichtbar, weil dort alles
gesättigt ist — deshalb erst spät bemerkt.

### Gesicherte Messungen

Alle mit einem gleichmässig hellblauen Blatt, auf dem jede Struktur im Bild
zwangsläufig der Fehler ist.

**Streifenbreite: 42 Pixel**, mit Vielfachen (84). Das ist exakt die
Sensorsegmentperiode. Es sind Rechteckstufen mit scharfen Kanten, kein weicher
Verlauf.

**Segmentprofil hat Periode 3.** Die Grün/Blau-Verhältnisse je Segment
wiederholen sich als `0,879 / 0,924 / 0,997`, also über 3 × 42 = 126 Pixel.

**Mit neutraler Kennlinie verschwinden die Bänder vollständig** (Dunkel 0,
Gain `0x4000`) — dafür fällt die Randausleuchtung ab (Rand/Mitte 0,23 statt
0,51).

**Die geladene Kalibrierung ist in sich unperiodisch.** Aus dem SRAM
zurückgelesen: 2590 Einträge, keine Periodenspitze in irgendeinem Kanal. Der
Fehler entsteht also bei der *Anwendung*, nicht in den Daten.

### Die Zuordnung ist gemessen

`tools/probe_shading_map.py` markiert einzelne Kalibriereinträge durch gedämpfte
Verstärkung und liest ab, wo die dunklen Spalten im Bild landen:

| Eintrag | 500 | 1000 | 1500 | 2000 | 2400 |
|---|---|---|---|---|---|
| Bildspalte | 492 | 985 | 1477 | 1969 | 2362 |

Daraus **Bildspalte = Eintrag × 2550/2590**, Abweichung höchstens 0,5 Spalten.
Diese Abbildung ist in `shading_from_flash` umgesetzt. Sie halbiert den
Phasenausreisser (0,117 → 0,062), **beseitigt die Bänder aber nicht** — sie war
also nötig, aber nicht die Ursache.

### Widerlegt

- **Die Kalibrierdaten sind nicht schuld.** Auch der mitgeschnittene
  Vendor-Block — aus einem funktionierenden Scan — erzeugt die Bänder.
- **Blindpixel an den Segmentgrenzen**: 61 Lücken gegenüber 40 nötigen
  Einfügungen, passt nicht.
- **Segmentlücken füllen**: macht keinen Unterschied. Auf farbiger Vorlage gibt
  es die Nullbytes gar nicht — die früher gemessenen stammen von gesättigtem
  Weiss. `fill_segment_gaps` ist überflüssig, aber unschädlich.
- **Globaler Byteversatz** im Datenstrom: senkt die Segmentstreuung nur von
  0,054 auf 0,034, löst es nicht.
- **Der Flash enthält die Zielwerte gar nicht.** Die Verstärkungen des
  Vendor-Downloads (`36666`, `36248`, …) kommen im Flash nicht vor; die Verläufe
  korrelieren nur mit 0,80. Der Vendor *berechnet* die Kalibrierung. Die
  Dunkelwerte sind im Flash durchgehend 0, im Download im Mittel 35 — er misst
  sie offenbar je Lauf neu.

### Wo weitersuchen

Die Rechteckform und die Periode 3 über Segmente deuten auf eine
**segmentweise Kanalgewichtung** im AFE, nicht auf die Shading-Tabelle. Die
Register `0x52`–`0x57` beschreiben die Sensorsegmente (Werte `08,0a,00,02,04,06`)
— dort wäre anzusetzen, ebenso bei `CAsic::AdjustTG` und den AFE-Registern
`0x50`/`0x51`/`0x5d`/`0x5e`.

Am schnellsten klärt es ein **USB-Mitschnitt eines farbigen Fotoscans unter
Windows**: alle bisherigen Mitschnitte zeigen weisse Blätter, auf denen der
Fehler unsichtbar ist. Aufgabenliste im README.

## 3c. Schwestergerät

`ScanInfoVID_07B3_PID_1443.plist` hat eine **strukturell identische**
Konfiguration (identische Schlüsselmenge, nur die Geräte-ID unterscheidet sich).
Ein daraus entstehender Treiber deckt sehr wahrscheinlich beide Geräte ab.

### Einordnung

`0x83` entspricht `VALUE_SET_REGISTER` im SANE-Backend `genesys`. Zusammen mit
den Chip-Klassen `CGL846`, `CGL125`, `CGL126`, `CGL128` im Framework
(Quelldateien `GL846.cpp`, `GL125.cpp`, `GL126.cpp`, `GL128.cpp`) ist damit
belegt, dass es sich um einen **Genesys-Logic-Scannerchip** handelt. Der
erweiterte Registerraum > `0xFF` weicht vom klassischen GL846 ab.

**Noch offen:** welche der vier Chipklassen für PID `143D` instanziiert wird.
Die Zuordnung steht nicht in den Konfigurationsdaten, sondern im Code.

## 4. Weitere Transportprimitive (aus den Symbolen)

- `StartReadTransaction` / `EndReadTransaction`, `StartWriteTransaction` / `EndWriteTransaction` — Transaktionsklammern um Bulk-Transfers
- `APBWriteAddr` / `APBWriteData` / `APBReadData`, `AHBWriteAddr` — direkter Zugriff auf die AMBA-Busse des SoC
- `SFlash_*` (RDID, Read, Write, Erase, ChipErase, WREN/WRDI, ChkWIP) — SPI-Flash, hält u.a. die Shading-Kalibrierung
- `SPI_*`, `WriteAfe` / `ReadAfe` / `WriteAllAfe` — Analog-Front-End
- `Interrupt(unsigned char&)`, `GetPushButtonID`, `ReadButtonID` — Buttons über EP `0x83`
- `WriteSevenSegmentNum(char)` — die 7-Segment-Anzeige am Gerät

## 5. Gerätekonfiguration

`ScanApi.framework/Resources/ScanInfoVID_07B3_PID_143D.plist` enthält die
vollständige gerätespezifische Konfiguration als lesbares XML — u.a.:

- Register-Init-Block (Schlüssel `0`): 116 Adress/Wert-Paare
- `AfeReadReg = 0x0050,0x0107,0x0108`, `AfeWriteReg = 0x0051,0x005d,0x005e`
- `HT8236 = 1` → AFE ist ein HT82V36 (Klasse `CHT82V36`)
- Auflösungen: `PhysicalDpi 600`, `HWDPI 1200`, `PseudoDpi 1200,600`; Scanprofile für 300 und 600 dpi
- `SYSTEMCLOCK 48`, `AHBClock 60`, `MOTORDPI 300`
- Motorkurven (`Curve 20,3000,250`, `FastCurve 80,2000,220`)
- Sheetfed-Geometrie (`AREAPAGE 0,0,10200,14040`), Papiereinzug, Auswurf
- Kalibrierparameter (`PIXELGAIN`, `PIXELDARK`, `LINEGAIN`, Shading-Ablage im Flash)

Die gerätespezifischen Daten sind damit vollständig **Konfiguration, nicht Code** —
sie müssen nicht rekonstruiert werden, sondern können direkt übernommen werden.
