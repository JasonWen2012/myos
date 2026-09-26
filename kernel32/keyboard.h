// myos 32-bit kernel: input, from the keyboard and from the serial port.

#pragma once

#include "types.h"

namespace myos {

// Both the PS/2 keyboard (through IRQ1) and COM1 (through IRQ4) push characters
// into one queue.  That is not a shortcut: a kernel with a serial console has two
// keyboards, and the shell should not care which one a line was typed on.  It is
// also what makes the shell testable -- a test types over the serial port, and a
// person types on the keyboard, with identical handling.

void keyboard_init();
constexpr uint32 KEY_QUEUE_SIZE = 64;
bool key_available();
// Blocks until a key arrives.  With interrupts enabled this is a `hlt`, so the CPU
// is idle rather than spinning.
char key_get();
// Called by the IRQ1 handler with the byte read from port 0x60.
void keyboard_handle_scancode(uint8 scancode);
// Called by the IRQ4 handler when COM1 has a byte.
void serial_handle_rx();
// The decoder, exercised with synthetic scancodes by the self-test.
char keyboard_translate(uint8 scancode);
// Drops one character into the queue (used by the serial path and by tests).
void key_push(char c);
// Number of scancodes the keyboard has delivered, for `keylog`.
uint32 keyboard_scancode_count();

}  // namespace myos
