// myos 32-bit kernel: the global descriptor table.

#pragma once

#include "types.h"

namespace myos {

// The kernel's own flat selectors.  They match the ones the loader installs
// (boot/boot.inc), which is what lets the kernel take ownership of the table
// without disturbing anything already running.
constexpr uint16 GDT_SELECTOR_CODE = 0x08;
constexpr uint16 GDT_SELECTOR_DATA = 0x10;

struct GdtEntry {
    uint16 limit_low;
    uint16 base_low;
    uint8 base_middle;
    uint8 access;
    uint8 granularity;
    uint8 base_high;
};
static_assert(sizeof(GdtEntry) == 8, "a descriptor is eight bytes");

// The value `lgdt` reads: a 16-bit limit and a 32-bit linear base.
struct GdtPointer {
    uint16 limit;
    uint32 base;
} __attribute__((packed));
static_assert(sizeof(GdtPointer) == 6, "lgdt reads six bytes");

void gdt_init();
// Sets one descriptor.  `granularity` carries the flags nibble in its high half
// (0xC0 for 4 KiB pages and 32-bit operands).
void gdt_set_entry(uint32 index, uint32 base, uint32 limit, uint8 access,
                   uint8 granularity);
// What the CPU has loaded, read back with sgdt.  The self-test compares it with
// the table above, which is the difference between "lgdt was called" and "lgdt
// worked".
void gdt_read_loaded(GdtPointer* out);
// The table the kernel built: its linear address and its size in bytes.
uint32 gdt_address();
uint32 gdt_size();

}  // namespace myos
