/* SANE-Backend fuer den Plustek ePhoto Z300 (USB 07b3:143d, Genesys CGL126).
 *
 * Der Hersteller liefert keinen Linux-Treiber.  Das Protokoll wurde zum Zweck
 * der Interoperabilitaet rekonstruiert (Art. 6 der EU-Softwarerichtlinie
 * 2009/24/EG).  Herstellersoftware wird weder benoetigt noch mitgeliefert:
 * Motorrampe und Gammakennlinie werden berechnet, die Kalibrierdaten liest der
 * Treiber aus dem SPI-Flash des Geraets.
 *
 * Aufbau:
 *   - Registerzugriff und Bulk-Transport ueber libusb-1.0
 *   - Ablauf als Tabelle in ephoto_sequence.h
 *   - der komplette Scan laeuft in sane_start; sane_read gibt den Puffer aus
 *
 * Bekannte Grenze: 300 dpi Farbe, A4.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

#include <libusb-1.0/libusb.h>

#define BACKEND_NAME ephoto
#include <sane/sane.h>
#include <sane/saneopts.h>

#include "ephoto_sequence.h"
#include "enhance.h"

#define VID 0x07b3
#define PID 0x143d

#define REQ_OUT 0x40
#define REQ_IN  0xc0
#define BREQ    0x04
#define V_WREG  0x83
#define V_BUF   0x82
#define EP_IN   0x81
#define EP_OUT  0x02

/* Geometrie und Aufbereitungsparameter kommen aus ep_modes[] (siehe Header) */
static const ep_mode *mode = &ep_modes[0];

/* Abstand Papiersensor -> Scanzeile.  INI: SheetfedLoadPaperStep = 930 Schritte
 * bei MOTORDPI = 300; Zeilen entsprechen 1:1 der Aufloesung. */
#define SENSOR_TO_SCANLINE_300 930

#define REG_STATUS 0x101
#define REG_MOTOR  0x02
#define DOCSENSOR  0x08
#define MOTORENB   0x01

static libusb_context *ctx;
static libusb_device_handle *dev;
static SANE_Byte *image;            /* fertiges RGB-Bild */
static int img_lines, img_read;
static SANE_Bool scanning;
static SANE_Bool enhance = SANE_TRUE;   /* Tonwertkurve + Nachschaerfen */


static const SANE_Device dev_desc = {
  "ephoto:z300", "Plustek", "ePhoto Z300", "sheetfed scanner"
};
static const SANE_Device *dev_list[] = { &dev_desc, NULL };

/* ---- USB-Grundlagen ---------------------------------------------------- */

static int ctrl_out(unsigned v, unsigned idx, const unsigned char *d, int n)
{
  return libusb_control_transfer(dev, REQ_OUT, BREQ, v, idx,
                                 (unsigned char *)d, n, 1000);
}

static int read_reg(int reg)
{
  unsigned char b[2];
  int r = libusb_control_transfer(dev, REQ_IN, BREQ, 0x8e | (reg & 0x100),
                                  ((reg & 0xff) << 8) | 0x22, b, 2, 1000);
  return r == 2 ? b[0] : -1;
}

static int bufset(unsigned addr, unsigned len, unsigned idx)
{
  unsigned char p[8];
  p[0] = addr; p[1] = addr >> 8; p[2] = addr >> 16; p[3] = addr >> 24;
  p[4] = len;  p[5] = len >> 8;  p[6] = len >> 16;  p[7] = len >> 24;
  return ctrl_out(V_BUF, idx, p, 8) == 8 ? 0 : -1;
}

/* ---- Tabellen ---------------------------------------------------------- */

/* Lineare Gammakennlinie; ab Index 128 zaehlt der Hersteller eins hoeher. */
static void gamma_table(unsigned char *out)
{
  for (int i = 0; i < 256; i++) {
    unsigned v = ((unsigned)i << 8) | 0xff;
    if (i >= 128) v++;
    if (v > 0xffff) v = 0xffff;
    out[2 * i] = v & 0xff;
    out[2 * i + 1] = v >> 8;
  }
}

/* Motorrampe:  T[i] = C * i^-0.4, gedeckelt beim Plateau.
 * Die Parameter stehen in der Geraetekonfiguration als CURVE = 20,3000,250:
 * Exponent = 100/250, Plateau = 3000/2.  Erster Eintrag ist der Anlaufwert. */
static void slope_table(unsigned char *out, int dpi)
{
  /* 600 dpi faehrt das Speed2-Profil: ein Anlaufwert, danach konstante
   * Periode ohne Rampe.  Aus dem Vendor-Lauf uebernommen. */
  if (dpi == 600) {
    for (int i = 0; i < 256; i++) {
      unsigned v = i == 0 ? 38461u : 11000u;
      out[2 * i] = v & 0xff;
      out[2 * i + 1] = v >> 8;
    }
    return;
  }
  const double c = 5104.0, e = 0.4;
  const unsigned plateau = 1500, start = 25641;
  for (int i = 0; i < 256; i++) {
    unsigned v = i == 0 ? start : (unsigned)(c * pow((double)i, -e) + 0.5);
    if (v < plateau) v = plateau;
    if (v > 0xffff) v = 0xffff;
    out[2 * i] = v & 0xff;
    out[2 * i + 1] = v >> 8;
  }
}

/* Kalibrierdaten aus dem SPI-Flash: Bank 2 (300 dpi) ab 0x20000, Bank 3
 * (600 dpi) ab 0x30000, jeweils 32 B Kopf ueberspringen.  Im Flash liegen je
 * Pixel 12 Byte lueckenlos; das Shading-SRAM erwartet Bloecke zu 42 Pixeln
 * (504 Byte) plus 8 Byte Fueller.  Lueckenlos geladen rotiert die
 * Kanalzuordnung alle 42 Pixel -> senkrechte Farbbaender. */
#define SHADING_BLOCK_DATA (42 * 12)
#define SHADING_BLOCK 512

static int flash_read(unsigned char *out, int len, int dpi);

static int shading_from_flash(unsigned char *out, int len, int dpi, int pixels)
{
  int data_len = pixels * 12;
  unsigned char *flat = malloc(data_len);
  if (!flat) return -1;
  int r = flash_read(flat, data_len, dpi);
  if (r == 0) {
    memset(out, 0, len);
    for (int src = 0, dst = 0; src < data_len && dst < len;
         src += SHADING_BLOCK_DATA, dst += SHADING_BLOCK) {
      int n = data_len - src < SHADING_BLOCK_DATA ? data_len - src : SHADING_BLOCK_DATA;
      if (n > len - dst) n = len - dst;
      memcpy(out + dst, flat + src, n);
    }
  }
  free(flat);
  return r;
}

static int flash_read(unsigned char *out, int len, int dpi)
{
  unsigned char one = 1, zero4[4] = { 0, 0, 0, 0 };
  if (ctrl_out(0x8b, 0x07, &one, 1) != 1) return -1;      /* SPI freischalten */
  if (ctrl_out(0x8b, 0x09, zero4, 4) != 4) return -1;

  unsigned addr = (dpi == 600 ? 0x30000u : 0x20000u) + 0x20;
  unsigned char a[4] = { addr, addr >> 8, addr >> 16, addr >> 24 };
  if (ctrl_out(0x8b, 0x0b, a, 4) != 4) return -1;         /* Leseadresse */

  int got = 0;
  while (got < len) {
    int want = len - got > 0x40 ? 0x40 : len - got;
    int r = libusb_control_transfer(dev, REQ_IN, BREQ, 0x8a, 0x0f,
                                    out + got, want, 1000);
    if (r <= 0) return -1;
    got += r;
  }
  return 0;
}

/* Nach dem Scan den Auswurf abwarten und den Motor sicher abschalten.
 *
 * Der Vendor-Ablauf bestromt am Ende den Motor, um das Blatt auszuwerfen
 * (EjectPaper = 6500 Schritte).  Bei 600 dpi laeuft der Motor mit 11000 statt
 * 1500 us je Schritt — derselbe Auswurf dauert damit rund 70 statt 10 Sekunden.
 * Deshalb nach einer Wartezeit die Endstufe abschalten; das Blatt hat den
 * Scanbereich zu diesem Zeitpunkt ohnehin verlassen.
 *
 * Gemessen: im Python-Pfad steht der Motor nach rund drei Sekunden von selbst
 * (Gesamtlauf 21 s gegenüber 30 s mit 12-Sekunden-Sperre).  Sechs Sekunden
 * lassen dem Auswurf Luft, ohne unnötig zu warten.
 */
static void settle(double max_seconds)
{
  const int step_ms = 50;
  int waited = 0;
  while (waited < (int)(max_seconds * 1000)) {
    int st = read_reg(REG_STATUS);
    if (st < 0 || !(st & MOTORENB)) return;
    struct timespec ts = { 0, step_ms * 1000000L };
    nanosleep(&ts, NULL);
    waited += step_ms;
  }
  unsigned char off[2] = { REG_MOTOR, 0x00 };
  ctrl_out(V_WREG, 0, off, 2);
  unsigned char idle[2] = { REG_MOTOR, 0x78 };   /* Ruhezustand wie im Init */
  ctrl_out(V_WREG, 0, idle, 2);
}

/* ---- Scan -------------------------------------------------------------- */

static int run_sequence(unsigned char **raw, size_t *raw_len)
{
  unsigned char *tables[DL_COUNT];
  size_t tsize[DL_COUNT];
  for (int i = 0; i < DL_COUNT; i++) { tables[i] = NULL; tsize[i] = 0; }

  tsize[DL_SHADING] = mode->shading_len;
  for (int i = DL_SLOPE0; i <= DL_GAMMA_B; i++) tsize[i] = 512;
  for (int i = 0; i < DL_COUNT; i++) {
    tables[i] = malloc(tsize[i]);
    if (!tables[i]) goto fail;
  }
  slope_table(tables[DL_SLOPE0], mode->dpi);
  memcpy(tables[DL_SLOPE1], tables[DL_SLOPE0], 512);
  memcpy(tables[DL_SLOPE2], tables[DL_SLOPE0], 512);
  gamma_table(tables[DL_GAMMA_R]);
  memcpy(tables[DL_GAMMA_G], tables[DL_GAMMA_R], 512);
  memcpy(tables[DL_GAMMA_B], tables[DL_GAMMA_R], 512);
  if (shading_from_flash(tables[DL_SHADING], mode->shading_len, mode->dpi,
                         mode->pixels) < 0)
    goto fail;

  /* Die mitgeschnittene Sequenz beschreibt nur die Scan-Slots 0-2.  Die
   * Schnellfahr-Slots 3 und 4 nutzt der Auswurf am Ende; stehen dort
   * uninitialisierte SRAM-Werte, faehrt der Motor mit zufaelligen
   * Schrittintervallen und mahlt hoerbar.  Der Vendor schreibt sie beim
   * Oeffnen des Geraets, also ausserhalb des mitgeschnittenen Fensters. */
  {
    unsigned char fast[512];
    for (int i = 0; i < 256; i++) {
      unsigned v = i == 0 ? 38461u : 11000u;   /* Vorschubtabelle */
      fast[2 * i] = v & 0xff;
      fast[2 * i + 1] = v >> 8;
    }
    const unsigned slots[2] = { 0x1000C000u, 0x10010000u };
    for (int k = 0; k < 2; k++) {
      int n = 0;
      if (bufset(slots[k], sizeof(fast), 1) == 0)
        libusb_bulk_transfer(dev, EP_OUT, fast, (int)sizeof(fast), &n, 2000);
    }
  }

  size_t total = 0;
  for (int i = 0; i < mode->seq_len; i++)
    if (mode->seq[i].op == OP_R) total += mode->seq[i].len;

  unsigned char *buf = malloc(total);
  if (!buf) goto fail;
  size_t have = 0;

  int last_read = 0;
  for (int i = 0; i < mode->seq_len; i++)
    if (mode->seq[i].op == OP_R) last_read = i;

  /* Sensorgesteuertes Ende: sobald das Blatt den Papiersensor verlassen hat
   * und die Strecke bis zur Scanzeile nachgefuehrt ist, wird abgebrochen —
   * statt stur die volle A4-Laenge der Sequenz durchzufahren.  Eine kleine
   * Vorlage braucht so nur einen Bruchteil der Zeit, und der lange Auswurf
   * entfaellt.  Der Ausstieg erfolgt zwischen zwei Bloecken; jeder Block hat
   * sein eigenes BUFSET, es bleibt also keine Transaktion offen. */
  const int trail_lines = SENSOR_TO_SCANLINE_300 * mode->dpi / 300;
  int paper_gone_at = -1, stop_now = 0;

  int failed = 0;
  for (int i = 0; i <= last_read && !failed; i++) {
    const ep_step *s = &mode->seq[i];
    if (s->op == OP_R && stop_now) continue;
    switch (s->op) {
    case OP_W:
      if (ctrl_out(V_WREG, 0, ep_payload + s->off, s->len) != s->len) failed = 1;
      break;
    case OP_C:
      libusb_control_transfer(dev, REQ_OUT, s->a == 0x8c ? 0x0c : BREQ,
                              s->a, s->b, (unsigned char *)ep_payload + s->off,
                              s->len, 1000);
      break;
    case OP_D: {
      int n = 0;
      if (bufset(s->b, tsize[s->a], 1) < 0) { failed = 1; break; }
      if (libusb_bulk_transfer(dev, EP_OUT, tables[s->a],
                               (int)tsize[s->a], &n, 2000) < 0) failed = 1;
      break;
    }
    case OP_R: {
      /* auf Paketgroesse aufrunden, sonst liefert libusb einen Overflow */
      int room = (s->len + 511) / 512 * 512;
      unsigned char *tmp = malloc(room);
      int n = 0;
      if (!tmp) { failed = 1; break; }
      if (bufset(s->b, s->len, s->a) < 0) { free(tmp); failed = 1; break; }
      if (libusb_bulk_transfer(dev, EP_IN, tmp, room, &n, 5000) < 0) {
        free(tmp); failed = 1; break;
      }
      if (n > s->len) n = s->len;
      if (have + n > total) n = (int)(total - have);
      memcpy(buf + have, tmp, n);
      have += n;
      free(tmp);
      {
        int lines = (int)(have / mode->stride);
        if (paper_gone_at < 0) {
          int st = read_reg(REG_STATUS);
          if (st >= 0 && (st & DOCSENSOR)) paper_gone_at = lines;
        } else if (lines - paper_gone_at >= trail_lines) {
          stop_now = 1;
        }
      }
      break;
    }
    }
  }

  /* Abschluss immer fahren: eine offene Lesetransaktion legt das Geraet lahm.
   *
   * Ausnahme: Der Vendor bestromt am Ende den Motor (Register 0x02 = 0x50), um
   * das Blatt auszuwerfen.  Ist der Papiersensor bereits frei, ist das
   * ueberfluessig — die Bewegung laeuft dann gegen nichts und klingt
   * entsprechend.  Bei 600 dpi dauert sie ausserdem siebenmal so lang wie bei
   * 300 (11000 statt 1500 us je Schritt).  Deshalb in diesem Fall den Motor
   * ausgeschaltet lassen (0x40 statt 0x50). */
  int paper_left = 0;
  {
    int st = read_reg(REG_STATUS);
    paper_left = (st >= 0) && !(st & DOCSENSOR);
  }
  for (int i = last_read + 1; i < mode->seq_len; i++) {
    const ep_step *s = &mode->seq[i];
    if (s->op == OP_W) {
      if (!paper_left && s->len == 2 && ep_payload[s->off] == REG_MOTOR
          && (ep_payload[s->off + 1] & 0x10)) {
        unsigned char noeject[2] = { REG_MOTOR,
                                     (unsigned char)(ep_payload[s->off + 1] & ~0x10) };
        ctrl_out(V_WREG, 0, noeject, 2);
        continue;
      }
      ctrl_out(V_WREG, 0, ep_payload + s->off, s->len);
    }
    else if (s->op == OP_C)
      libusb_control_transfer(dev, REQ_OUT, s->a == 0x8c ? 0x0c : BREQ,
                              s->a, s->b, (unsigned char *)ep_payload + s->off,
                              s->len, 1000);
  }

  settle(6.0);

  for (int i = 0; i < DL_COUNT; i++) free(tables[i]);
  if (failed && have == 0) { free(buf); return -1; }
  *raw = buf;
  *raw_len = have;
  return 0;

fail:
  for (int i = 0; i < DL_COUNT; i++) free(tables[i]);
  return -1;
}

/* Segmentluecken fuellen, Farbzeilen ausrichten, spiegeln, Rand abschneiden. */
static SANE_Byte *postprocess(unsigned char *raw, size_t len, int *out_lines)
{
  const int W = mode->pixels, S = mode->stride;
  int lines = (int)(len / S);
  if (lines < 16) return NULL;

  /* Segmentgrenzen: Gruen und Blau aus den Nachbarn interpolieren.
   * Nur bei 300 dpi noetig — 600 dpi liefert lueckenlos (gap_period 0). */
  if (mode->gap_period > 0) {
    for (int y = 0; y < lines; y++) {
      unsigned char *r = raw + (size_t)y * S;
      for (int x = mode->gap_first; x < W; x += mode->gap_period) {
        int lo = x > 0 ? x - 1 : x + 1, hi = x + 1 < W ? x + 1 : x - 1;
        for (int c = 1; c < 3; c++)
          r[x * 3 + c] = (unsigned char)((r[lo * 3 + c] + r[hi * 3 + c]) / 2);
      }
    }
  }

  /* Ungleichmaessige Zeilenlage (Periode 4, Motorschritt) ausgleichen.  Auf den
   * Rohzeilen, also vor dem Farbzeilenversatz: der Fehler entsteht je
   * Belichtung und betrifft alle drei Kanaele einer Rohzeile gleich.  Greift
   * nur, wenn das Muster tatsaechlich vorhanden ist (siehe enhance.h). */
  en_dejitter(raw, lines, W, NULL, NULL, NULL);

  /* Zeilenhelligkeit fuer den Beschnitt */
  double *rowmean = calloc(lines, sizeof(double));
  if (!rowmean) return NULL;
  double maxrow = 0;
  for (int y = 0; y < lines; y++) {
    const unsigned char *r = raw + (size_t)y * S;
    long s = 0;
    for (int i = 0; i < S; i += 7) s += r[i];
    rowmean[y] = (double)s / (S / 7);
    if (rowmean[y] > maxrow) maxrow = rowmean[y];
  }
  /* Schwelle bei halber Papierhelligkeit: der Kantenuebergang dauert nur
   * etwa acht Zeilen, ein fester Zusatzbeschnitt wuerde Bildinhalt kosten */
  double thr = maxrow * 0.5;
  int y0 = 0, y1 = lines - 1;
  while (y0 < lines && rowmean[y0] <= thr) y0++;
  while (y1 > y0 && rowmean[y1] <= thr) y1--;
  free(rowmean);

  y0 += mode->ch_off; y1 -= mode->ch_off;
  if (y1 <= y0) return NULL;
  int n = y1 - y0 + 1;

  SANE_Byte *out = malloc((size_t)n * S);
  if (!out) return NULL;
  for (int y = 0; y < n; y++) {
    unsigned char *o = out + (size_t)y * S;
    const unsigned char *gr = raw + (size_t)(y0 + y) * S;
    const unsigned char *rr = raw + (size_t)(y0 + y + mode->ch_off) * S;
    const unsigned char *br = raw + (size_t)(y0 + y - mode->ch_off) * S;
    for (int x = 0; x < W; x++) {
      int s = (W - 1 - x) * 3;      /* HorizontalMirror = 1 */
      o[x * 3]     = rr[s];
      o[x * 3 + 1] = gr[s + 1];
      o[x * 3 + 2] = br[s + 2];
    }
  }
  if (enhance)
    en_tone_sharpen(out, n, W, 1.0, 0.3);
  *out_lines = n;
  return out;
}

/* ---- SANE-Schnittstelle ------------------------------------------------ */

enum { OPT_NUM = 0, OPT_MODE, OPT_RES, OPT_ENHANCE, OPT_COUNT };
static SANE_Option_Descriptor opts[OPT_COUNT];
static SANE_String_Const mode_list[] = { SANE_VALUE_SCAN_MODE_COLOR, NULL };
static const SANE_Int res_list[] = { 2, 300, 600 };

static void init_options(void)
{
  opts[OPT_NUM].name = SANE_NAME_NUM_OPTIONS;
  opts[OPT_NUM].title = SANE_TITLE_NUM_OPTIONS;
  opts[OPT_NUM].desc = SANE_DESC_NUM_OPTIONS;
  opts[OPT_NUM].type = SANE_TYPE_INT;
  opts[OPT_NUM].size = sizeof(SANE_Word);
  opts[OPT_NUM].cap = SANE_CAP_SOFT_DETECT;

  opts[OPT_MODE].name = SANE_NAME_SCAN_MODE;
  opts[OPT_MODE].title = SANE_TITLE_SCAN_MODE;
  opts[OPT_MODE].desc = SANE_DESC_SCAN_MODE;
  opts[OPT_MODE].type = SANE_TYPE_STRING;
  opts[OPT_MODE].size = 32;
  opts[OPT_MODE].cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
  opts[OPT_MODE].constraint_type = SANE_CONSTRAINT_STRING_LIST;
  opts[OPT_MODE].constraint.string_list = mode_list;

  opts[OPT_RES].name = SANE_NAME_SCAN_RESOLUTION;
  opts[OPT_RES].title = SANE_TITLE_SCAN_RESOLUTION;
  opts[OPT_RES].desc = SANE_DESC_SCAN_RESOLUTION;
  opts[OPT_RES].type = SANE_TYPE_INT;
  opts[OPT_RES].unit = SANE_UNIT_DPI;
  opts[OPT_RES].size = sizeof(SANE_Word);
  opts[OPT_RES].cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
  opts[OPT_RES].constraint_type = SANE_CONSTRAINT_WORD_LIST;
  opts[OPT_RES].constraint.word_list = res_list;

  /* desc muss gesetzt sein, sonst stuerzt scanimage --help ab */
  opts[OPT_ENHANCE].name = "enhance";
  opts[OPT_ENHANCE].title = "Bildaufbereitung";
  opts[OPT_ENHANCE].desc = "Tonwertkurve und leichtes Nachschaerfen anwenden. "
                           "Aus: lineare Sensordaten, z. B. fuer eigene Nachbearbeitung.";
  opts[OPT_ENHANCE].type = SANE_TYPE_BOOL;
  opts[OPT_ENHANCE].size = sizeof(SANE_Word);
  opts[OPT_ENHANCE].cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
}

SANE_Status sane_init(SANE_Int *version, SANE_Auth_Callback cb)
{
  (void)cb;
  if (version) *version = SANE_VERSION_CODE(1, 0, 0);
  if (libusb_init(&ctx) < 0) return SANE_STATUS_IO_ERROR;
  init_options();
  return SANE_STATUS_GOOD;
}

void sane_exit(void)
{
  if (dev) { libusb_close(dev); dev = NULL; }
  if (ctx) { libusb_exit(ctx); ctx = NULL; }
  free(image); image = NULL;
}

SANE_Status sane_get_devices(const SANE_Device ***list, SANE_Bool local)
{
  (void)local;
  libusb_device_handle *h = libusb_open_device_with_vid_pid(ctx, VID, PID);
  if (!h) { static const SANE_Device *empty[] = { NULL }; *list = empty;
            return SANE_STATUS_GOOD; }
  libusb_close(h);
  *list = dev_list;
  return SANE_STATUS_GOOD;
}

SANE_Status sane_open(SANE_String_Const name, SANE_Handle *h)
{
  (void)name;
  dev = libusb_open_device_with_vid_pid(ctx, VID, PID);
  if (!dev) return SANE_STATUS_INVAL;
  libusb_set_auto_detach_kernel_driver(dev, 1);
  libusb_claim_interface(dev, 0);
  *h = (SANE_Handle)&dev_desc;
  return SANE_STATUS_GOOD;
}

void sane_close(SANE_Handle h)
{
  (void)h;
  if (dev) { libusb_release_interface(dev, 0); libusb_close(dev); dev = NULL; }
  free(image); image = NULL;
}

const SANE_Option_Descriptor *sane_get_option_descriptor(SANE_Handle h, SANE_Int n)
{
  (void)h;
  return (n >= 0 && n < OPT_COUNT) ? &opts[n] : NULL;
}

SANE_Status sane_control_option(SANE_Handle h, SANE_Int n, SANE_Action a,
                                void *v, SANE_Int *info)
{
  (void)h;
  if (n < 0 || n >= OPT_COUNT) return SANE_STATUS_INVAL;
  if (a == SANE_ACTION_GET_VALUE) {
    switch (n) {
    case OPT_NUM: *(SANE_Word *)v = OPT_COUNT; break;
    case OPT_MODE: strcpy((char *)v, SANE_VALUE_SCAN_MODE_COLOR); break;
    case OPT_RES: *(SANE_Word *)v = mode->dpi; break;
    case OPT_ENHANCE: *(SANE_Word *)v = enhance; break;
    }
    return SANE_STATUS_GOOD;
  }
  if (a == SANE_ACTION_SET_VALUE) {
    if (n == OPT_RES) {
      SANE_Word want = *(SANE_Word *)v;
      for (int i = 0; i < EP_MODE_COUNT; i++)
        if (ep_modes[i].dpi == want) { mode = &ep_modes[i]; break; }
      if (info) *info |= SANE_INFO_RELOAD_PARAMS;
    }
    else if (n == OPT_ENHANCE)
      enhance = *(SANE_Word *)v ? SANE_TRUE : SANE_FALSE;
    return SANE_STATUS_GOOD;
  }
  return SANE_STATUS_UNSUPPORTED;
}

SANE_Status sane_get_parameters(SANE_Handle h, SANE_Parameters *p)
{
  (void)h;
  p->format = SANE_FRAME_RGB;
  p->last_frame = SANE_TRUE;
  p->bytes_per_line = mode->stride;
  p->pixels_per_line = mode->pixels;
  p->lines = img_lines > 0 ? img_lines : (mode->dpi == 600 ? 7020 : 3513);
  p->depth = 8;
  return SANE_STATUS_GOOD;
}

SANE_Status sane_start(SANE_Handle h)
{
  (void)h;
  if (!dev) return SANE_STATUS_IO_ERROR;

  int st = read_reg(REG_STATUS);
  if (st < 0) return SANE_STATUS_IO_ERROR;
  if (st & DOCSENSOR) return SANE_STATUS_NO_DOCS;

  unsigned char *raw = NULL;
  size_t raw_len = 0;
  if (run_sequence(&raw, &raw_len) < 0) return SANE_STATUS_IO_ERROR;

  free(image);
  image = postprocess(raw, raw_len, &img_lines);
  free(raw);
  if (!image) return SANE_STATUS_IO_ERROR;

  img_read = 0;
  scanning = SANE_TRUE;
  return SANE_STATUS_GOOD;
}

SANE_Status sane_read(SANE_Handle h, SANE_Byte *buf, SANE_Int maxlen, SANE_Int *len)
{
  (void)h;
  if (!scanning || !image) return SANE_STATUS_EOF;
  int total = img_lines * mode->stride;
  if (img_read >= total) { scanning = SANE_FALSE; *len = 0; return SANE_STATUS_EOF; }
  int n = total - img_read;
  if (n > maxlen) n = maxlen;
  memcpy(buf, image + img_read, n);
  img_read += n;
  *len = n;
  return SANE_STATUS_GOOD;
}

void sane_cancel(SANE_Handle h) { (void)h; scanning = SANE_FALSE; }
SANE_Status sane_set_io_mode(SANE_Handle h, SANE_Bool m)
{ (void)h; return m ? SANE_STATUS_UNSUPPORTED : SANE_STATUS_GOOD; }
SANE_Status sane_get_select_fd(SANE_Handle h, SANE_Int *fd)
{ (void)h; (void)fd; return SANE_STATUS_UNSUPPORTED; }

/* ---- Namensraum -------------------------------------------------------- *
 * Das dll-Backend von SANE laedt Backends ueber praefixierte Symbole
 * (sane_ephoto_init statt sane_init).  Ohne sie faellt es auf die
 * unpraefixierten zurueck — die kollidieren dann mit dem zuerst geladenen
 * Backend, und das Geraet taucht nicht auf.  Deshalb beide Namen anbieten.
 */
#define ALIAS(ret, name, args) \
  ret sane_ephoto_##name args __attribute__((alias("sane_" #name)))

ALIAS(SANE_Status, init, (SANE_Int *, SANE_Auth_Callback));
ALIAS(void, exit, (void));
ALIAS(SANE_Status, get_devices, (const SANE_Device ***, SANE_Bool));
ALIAS(SANE_Status, open, (SANE_String_Const, SANE_Handle *));
ALIAS(void, close, (SANE_Handle));
ALIAS(const SANE_Option_Descriptor *, get_option_descriptor,
      (SANE_Handle, SANE_Int));
ALIAS(SANE_Status, control_option,
      (SANE_Handle, SANE_Int, SANE_Action, void *, SANE_Int *));
ALIAS(SANE_Status, get_parameters, (SANE_Handle, SANE_Parameters *));
ALIAS(SANE_Status, start, (SANE_Handle));
ALIAS(SANE_Status, read, (SANE_Handle, SANE_Byte *, SANE_Int, SANE_Int *));
ALIAS(void, cancel, (SANE_Handle));
ALIAS(SANE_Status, set_io_mode, (SANE_Handle, SANE_Bool));
ALIAS(SANE_Status, get_select_fd, (SANE_Handle, SANE_Int *));
