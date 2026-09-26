// myos 32-bit kernel: what the firmware said about memory.

#pragma once

#include "types.h"

namespace myos {

// The loader collected the E820 map in real mode and left it in the boot info
// block, because by the time the kernel runs there is no BIOS to ask.
uint32 mem_entry_count();
// Total conventional-plus-extended memory the map marks as usable, in KiB.
uint32 mem_usable_kib();
// Prints the map the way `mem` wants it: the totals first, then the entries.
void mem_report();

}  // namespace myos
