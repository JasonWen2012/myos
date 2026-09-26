// myos 32-bit kernel: the text console, on screen and over the serial port.

#pragma once

#include "types.h"

namespace myos {

// VGA text mode attributes: (background << 4) | foreground.
enum Color : uint8 {
    COLOR_BLACK = 0,
    COLOR_BLUE = 1,
    COLOR_GREEN = 2,
    COLOR_CYAN = 3,
    COLOR_RED = 4,
    COLOR_MAGENTA = 5,
    COLOR_BROWN = 6,
    COLOR_LIGHT_GREY = 7,
    COLOR_DARK_GREY = 8,
    COLOR_LIGHT_BLUE = 9,
    COLOR_LIGHT_GREEN = 10,
    COLOR_LIGHT_CYAN = 11,
    COLOR_LIGHT_RED = 12,
    COLOR_LIGHT_MAGENTA = 13,
    COLOR_YELLOW = 14,
    COLOR_WHITE = 15,
};

constexpr uint8 attr(uint8 foreground, uint8 background) {
    return static_cast<uint8>((background << 4) | (foreground & 0x0F));
}

// The serial port is initialised first and is not optional: it is the only channel
// a headless run can read, which makes it the kernel's testable output.  It is also
// an input: COM1 is the second keyboard, and what a scripted test types.
void serial_init();
void serial_putc(char c);
bool serial_rx_ready();
uint8 serial_read_byte();
// Turns on the UART's "received data available" interrupt.  Called once the IDT
// exists, since until then the interrupt would have nowhere to go.
void serial_enable_rx_interrupt();
// The IRQ4 handler: reads the byte (which is what clears the interrupt) and hands
// it to the shared input queue.
void serial_handle_rx();
// How many bytes IRQ4 has delivered.  Reported by `keylog`, and the only way to
// tell "the host sent nothing" from "the driver lost it".
uint32 serial_bytes_received();

// The console writes every character to the screen *and* to the serial port, so a
// transcript of a run is the same text the user sees.
void console_init();
void console_clear();
void console_putc(char c);
void console_write(const char* text, size_t count);
void console_puts(const char* text);
void console_set_color(uint8 attribute);
uint8 console_color();
void console_put_dec(uint32 value);
void console_put_hex8(uint8 value);
void console_put_hex16(uint16 value);
void console_put_hex32(uint32 value);

// kprintf understands %s %c %d %u %x %p %% and nothing else -- enough for the
// kernel's own messages, and no floating point to drag in.
void kprintf(const char* format, ...);

}  // namespace myos
