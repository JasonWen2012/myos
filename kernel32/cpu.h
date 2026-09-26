// myos 32-bit kernel: the parts of CPU control that only assembly can express.

#pragma once

#include "types.h"

extern "C" {
// kernel32/boot.asm: stop the CPU for good (interrupts masked first).
void hlt_forever();
// kernel32/boot.asm: end a headless run through QEMU's isa-debug-exit device,
// with the process status (value << 1) | 1.  On real hardware the port is simply
// unused, so the write is harmless there.
void debug_exit(myos::uint32 value);
}
