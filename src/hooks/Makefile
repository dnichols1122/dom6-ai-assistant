CC      = gcc
CFLAGS  = -shared -fPIC -O2 -Wall -Wextra
LDFLAGS = -ldl -lpthread
TARGET  = trn_hook.so

$(TARGET): trn_hook.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)

.PHONY: clean
