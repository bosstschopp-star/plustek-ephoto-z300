CFLAGS = -O2 -fPIC -Wall -Wextra $(shell pkg-config --cflags libusb-1.0)
LDLIBS = $(shell pkg-config --libs libusb-1.0) -lm

libsane-ephoto.so.1: ephoto.c ephoto_sequence.h enhance.h
	$(CC) $(CFLAGS) -shared -o $@ ephoto.c $(LDLIBS)

install: libsane-ephoto.so.1
	install -m644 $< $(DESTDIR)/usr/lib/x86_64-linux-gnu/sane/
	ln -sf libsane-ephoto.so.1 $(DESTDIR)/usr/lib/x86_64-linux-gnu/sane/libsane-ephoto.so
	grep -qx ephoto /etc/sane.d/dll.conf || echo ephoto >> /etc/sane.d/dll.conf

enhance_test: enhance_test.c enhance.h
	$(CC) -O2 -Wall -Wextra -o $@ enhance_test.c -lm

clean:
	rm -f libsane-ephoto.so.1 enhance_test
