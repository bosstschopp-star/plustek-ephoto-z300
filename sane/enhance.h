/* Bildaufbereitung fuer den ePhoto Z300 — abgeleitet aus dem Vergleich eines
 * Linux- mit einem Windows-Scan desselben Fotos (600 dpi):
 *
 *   1. Zeilenlage entzerren.  Die Rohzeilen liegen mit Periode 4 ungleichmaessig
 *      (geschaetzt 0 / 2,02 / 2,40 / 2,49 px statt 0 / 1 / 2 / 3), sichtbar als
 *      Treppen an schraegen Kanten.  Die Lagen werden je Scan aus dem Bild
 *      selbst geschaetzt und die Zeilen gleichmaessig neu abgetastet.  Wirkt nur,
 *      wenn das Muster tatsaechlich vorhanden ist.
 *   2. Tonwertkurve.  Der Sensor liefert linear und damit viel zu dunkel
 *      (Median-Luminanz 78 statt 156).  Kurve am Windows-Scan gemessen.
 *   3. Leichtes Nachschaerfen (Unscharfmaske sigma 1,0, Staerke 0,3).
 *
 * Alle Funktionen arbeiten auf RGB-Zeilen zu je 3 Byte pro Pixel.
 */
#ifndef EPHOTO_ENHANCE_H
#define EPHOTO_ENHANCE_H

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define EN_PERIOD 4

/* Tonwertkurve, Stuetzstellen bei 0, 16, ..., 240, 255 */
static const double en_curve_y[17] = {
  39.9, 64.2, 101.0, 124.9, 143.7, 158.4, 171.4, 182.6, 193.1,
  202.7, 211.5, 219.4, 226.7, 233.4, 239.2, 244.9, 249.2
};

static void en_tone_lut(double *lut)
{
  for (int i = 0; i < 256; i++) {
    int k = i / 16;                            /* 0..15; das letzte Stueck reicht bis 255 */
    double x0 = k * 16, x1 = k < 15 ? x0 + 16 : 255;
    lut[i] = en_curve_y[k] + (i - x0) / (x1 - x0) * (en_curve_y[k + 1] - en_curve_y[k]);
  }
}

/* ---- 1. Zeilenlage ------------------------------------------------------ */

/* Kubische Gewichte wie OpenCV INTER_CUBIC (a = -0,75) */
static void en_cubic(double t, double w[4])
{
  const double a = -0.75;
  double x;
  x = 1 + t; w[0] = ((a * x - 5 * a) * x + 8 * a) * x - 4 * a;
  x = t;     w[1] = ((a + 2) * x - (a + 3)) * x * x + 1;
  x = 1 - t; w[2] = ((a + 2) * x - (a + 3)) * x * x + 1;
  w[3] = 1 - w[0] - w[1] - w[2];
}

static int en_reflect(int i, int n)            /* BORDER_REFLECT */
{
  while (i < 0 || i >= n) i = i < 0 ? -i - 1 : 2 * n - i - 1;
  return i;
}

/* Fuer jede Zielzeile y die (gebrochene) Quellzeile: Zeile r liegt bei
 * r + offs[r % 4]; gesucht ist die Umkehrung dieser monotonen Abbildung. */
static void en_source_rows(int lines, const double *offs, double *src)
{
#define EN_POS(r) ((r) + offs[(r) % EN_PERIOD])
  int r = 0;
  for (int y = 0; y < lines; y++) {
    while (r + 1 < lines && EN_POS(r + 1) <= y) r++;
    double p0 = EN_POS(r);
    if (y <= p0) src[y] = r;                          /* vor der ersten Zeile */
    else if (r + 1 >= lines) src[y] = lines - 1;      /* hinter der letzten */
    else {
      double p1 = EN_POS(r + 1);
      src[y] = r + (p1 > p0 ? (y - p0) / (p1 - p0) : 0);
    }
  }
#undef EN_POS
}

/* Spalten einer float-Matrix (rows x cols) vertikal neu abtasten */
static void en_resample_f(const float *in, float *out, int rows, int cols,
                          const double *src)
{
  for (int y = 0; y < rows; y++) {
    int i = (int)floor(src[y]);
    double w[4];
    en_cubic(src[y] - i, w);
    const float *r0 = in + (size_t)en_reflect(i - 1, rows) * cols;
    const float *r1 = in + (size_t)en_reflect(i, rows) * cols;
    const float *r2 = in + (size_t)en_reflect(i + 1, rows) * cols;
    const float *r3 = in + (size_t)en_reflect(i + 2, rows) * cols;
    float *o = out + (size_t)y * cols;
    for (int x = 0; x < cols; x++)
      o[x] = (float)(w[0] * r0[x] + w[1] * r1[x] + w[2] * r2[x] + w[3] * r3[x]);
  }
}

/* Wie stark die Zeilendifferenzen noch mit der Phase schwanken (0 = gar nicht) */
static double en_periodicity(const float *s, float *tmp, double *src,
                             int rows, int cols, const double *offs)
{
  en_source_rows(rows, offs, src);
  en_resample_f(s, tmp, rows, cols, src);
  double ph[EN_PERIOD] = { 0 };
  int cnt[EN_PERIOD] = { 0 };
  for (int y = 0; y + 1 < rows; y++) {
    const float *a = tmp + (size_t)y * cols, *b = a + cols;
    double d = 0;
    for (int x = 0; x < cols; x++) d += fabs((double)b[x] - a[x]);
    ph[y % EN_PERIOD] += d / cols;
    cnt[y % EN_PERIOD]++;
  }
  double mean = 0, var = 0;
  for (int p = 0; p < EN_PERIOD; p++) { ph[p] /= cnt[p] ? cnt[p] : 1; mean += ph[p]; }
  mean /= EN_PERIOD;
  for (int p = 0; p < EN_PERIOD; p++) var += (ph[p] - mean) * (ph[p] - mean);
  return mean > 0 ? sqrt(var / EN_PERIOD) / mean : 0;
}

/* Zeilenlagen schaetzen und, falls das Muster vorhanden ist, die Zeilen
 * gleichmaessig neu abtasten.  Liefert 1, wenn entzerrt wurde. */
static int en_dejitter(unsigned char *buf, int lines, int width, double *offs_out,
                       double *before_out, double *after_out)
{
  double offs[EN_PERIOD] = { 0 };
  int stride = width * 3;
  if (lines < 64 || width < 64) return 0;

  /* Stichprobe: tonwertkorrigierte Luminanz, Raender weg, jede 8. Spalte.
   * Der Ausschnitt beginnt auf einem Vielfachen von 4, damit die Phasen zu
   * den Zeilennummern des ganzen Bildes passen. */
  double lut[256];
  en_tone_lut(lut);
  int y0 = lines / 10 / EN_PERIOD * EN_PERIOD, y1 = lines * 9 / 10;
  int x0 = width / 8, x1 = width * 7 / 8, step = 8;
  int rows = y1 - y0, cols = (x1 - x0 + step - 1) / step;
  float *s = malloc(sizeof(float) * rows * cols);
  float *tmp = malloc(sizeof(float) * rows * cols);
  double *src = malloc(sizeof(double) * (rows > lines ? rows : lines));
  if (!s || !tmp || !src) { free(s); free(tmp); free(src); return 0; }
  for (int y = 0; y < rows; y++) {
    const unsigned char *r = buf + (size_t)(y0 + y) * stride;
    for (int c = 0; c < cols; c++) {
      const unsigned char *px = r + (size_t)(x0 + c * step) * 3;
      s[(size_t)y * cols + c] = (float)(0.299 * lut[px[0]] + 0.587 * lut[px[1]]
                                        + 0.114 * lut[px[2]]);
    }
  }

  double base = en_periodicity(s, tmp, src, rows, cols, offs);
  double before = base;
  static const double steps[3] = { 0.2, 0.05, 0.01 };
  for (int k = 0; k < 3; k++) {
    int better = 1;
    while (better) {
      better = 0;
      for (int p = 1; p < EN_PERIOD; p++) {      /* Phase 0 ist der Bezug */
        for (int sgn = -1; sgn <= 1; sgn += 2) {
          double t[EN_PERIOD];
          memcpy(t, offs, sizeof t);
          t[p] += sgn * steps[k];
          int ok = 1;                            /* Zeilen duerfen sich nicht ueberholen */
          for (int q = 0; q < EN_PERIOD; q++) {
            double a = q + t[q], b = q + 1 + t[(q + 1) % EN_PERIOD];
            if (b < a) ok = 0;
          }
          if (!ok) continue;
          double v = en_periodicity(s, tmp, src, rows, cols, t);
          if (v < base - 1e-6) { memcpy(offs, t, sizeof t); base = v; better = 1; }
        }
      }
    }
  }
  free(s); free(tmp);

  if (before_out) *before_out = before;
  if (after_out) *after_out = base;
  if (offs_out) memcpy(offs_out, offs, sizeof offs);

  /* Nur eingreifen, wenn das Muster deutlich da ist und sich klar bessert.
   * Gemessen: Windows-Scan 0,006, Linux roh 0,43, Linux entzerrt 0,016. */
  if (before < 0.05 || base > before * 0.5) { free(src); return 0; }

  unsigned char *out = malloc((size_t)lines * stride);
  if (!out) { free(src); return 0; }
  en_source_rows(lines, offs, src);
  for (int y = 0; y < lines; y++) {
    int i = (int)floor(src[y]);
    double w[4];
    en_cubic(src[y] - i, w);
    const unsigned char *r[4];
    for (int k = 0; k < 4; k++)
      r[k] = buf + (size_t)en_reflect(i - 1 + k, lines) * stride;
    unsigned char *o = out + (size_t)y * stride;
    for (int x = 0; x < stride; x++) {
      double v = w[0] * r[0][x] + w[1] * r[1][x] + w[2] * r[2][x] + w[3] * r[3][x];
      o[x] = v < 0 ? 0 : v > 255 ? 255 : (unsigned char)(v + 0.5);
    }
  }
  memcpy(buf, out, (size_t)lines * stride);
  free(out); free(src);
  return 1;
}

/* ---- 2./3. Tonwerte und Schaerfe ----------------------------------------- */

static int en_reflect101(int i, int n)
{
  if (n == 1) return 0;
  while (i < 0 || i >= n) i = i < 0 ? -i : 2 * n - i - 2;
  return i;
}

/* Tonwertkurve anwenden und mit einer Unscharfmaske nachschaerfen.
 * Arbeitet zeilenweise mit einem Ringpuffer von 9 Zeilen; der zusaetzliche
 * Speicher ist damit unabhaengig von der Bildhoehe. */
static int en_tone_sharpen(unsigned char *img, int lines, int width,
                           double sigma, double amount)
{
  enum { R = 4, N = 2 * R + 1 };
  int stride = width * 3;
  double lut[256], k[N], ks = 0;
  en_tone_lut(lut);
  for (int i = 0; i < N; i++) { k[i] = exp(-0.5 * (i - R) * (i - R) / (sigma * sigma)); ks += k[i]; }
  for (int i = 0; i < N; i++) k[i] /= ks;

  float *val = malloc(sizeof(float) * N * stride);     /* tonwertkorrigierte Zeilen */
  float *hb = malloc(sizeof(float) * N * stride);      /* davon horizontal geglaettet */
  if (!val || !hb) { free(val); free(hb); return -1; }

  int loaded = -1;
  for (int y = 0; y < lines; y++) {
    int need = y + R < lines ? y + R : lines - 1;
    while (loaded < need) {
      loaded++;
      const unsigned char *in = img + (size_t)loaded * stride;
      float *v = val + (size_t)(loaded % N) * stride;
      float *h = hb + (size_t)(loaded % N) * stride;
      for (int x = 0; x < stride; x++) v[x] = (float)lut[in[x]];
      for (int x = 0; x < width; x++)
        for (int c = 0; c < 3; c++) {
          double acc = 0;
          for (int j = 0; j < N; j++)
            acc += k[j] * v[en_reflect101(x + j - R, width) * 3 + c];
          h[x * 3 + c] = (float)acc;
        }
    }
    const float *v = val + (size_t)(y % N) * stride;
    unsigned char *o = img + (size_t)y * stride;
    for (int x = 0; x < stride; x++) {
      double b = 0;
      for (int j = 0; j < N; j++)
        b += k[j] * hb[(size_t)(en_reflect101(y + j - R, lines) % N) * stride + x];
      double out = v[x] + amount * (v[x] - b);
      o[x] = out < 0 ? 0 : out > 255 ? 255 : (unsigned char)(out + 0.5);
    }
  }
  free(val); free(hb);
  return 0;
}

#endif
