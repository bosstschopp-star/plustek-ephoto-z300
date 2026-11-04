/* Testprogramm fuer enhance.h ohne Scanner: liest ein binaeres PPM (P6),
 * wendet Zeilenentzerrung, Tonwertkurve und Nachschaerfen an, schreibt PPM.
 *
 *   make enhance_test && ./enhance_test ein.ppm aus.ppm
 */
#include <stdio.h>

#include "enhance.h"

int main(int argc, char **argv)
{
  if (argc != 3) { fprintf(stderr, "Aufruf: %s ein.ppm aus.ppm\n", argv[0]); return 2; }
  FILE *f = fopen(argv[1], "rb");
  int w, h, maxv;
  if (!f || fscanf(f, "P6 %d %d %d", &w, &h, &maxv) != 3 || maxv != 255) {
    fprintf(stderr, "kein 8-Bit-PPM: %s\n", argv[1]); return 1;
  }
  fgetc(f);
  size_t n = (size_t)w * h * 3;
  unsigned char *img = malloc(n);
  if (!img || fread(img, 1, n, f) != n) { fprintf(stderr, "PPM unvollstaendig\n"); return 1; }
  fclose(f);

  double offs[EN_PERIOD], before, after;
  int done = en_dejitter(img, h, w, offs, &before, &after);
  printf("Zeilenlagen %.2f %.2f %.2f %.2f px; Phasenschwankung %.4f -> %.4f; %s\n",
         offs[0], offs[1], offs[2], offs[3], before, after,
         done ? "entzerrt" : "unveraendert");
  en_tone_sharpen(img, h, w, 1.0, 0.3);

  f = fopen(argv[2], "wb");
  fprintf(f, "P6\n%d %d\n255\n", w, h);
  fwrite(img, 1, n, f);
  fclose(f);
  return 0;
}
