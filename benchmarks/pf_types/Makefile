CFLAGS := -O2 -Wall -Wextra

all: minor_page_fault_types

minor_page_fault_types: minor_page_fault_types.c
	$(CC) $(CFLAGS) -o $@ $^

clean:
	rm -f minor_page_fault_types

.PHONY: all clean
