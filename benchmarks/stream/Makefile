CC ?= clang
CFLAGS ?= -O2 -g -Wall

stream_bench: stream_bench.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f stream_bench

.PHONY: clean
