// myos 32-bit kernel: the global descriptor table.

#pragma once

#include "types.h"

namespace myos {

// The kernel's own flat selectors.  The first two match the ones the loader installs
// (boot/boot.inc), which is what lets the kernel take ownership of the table without
// disturbing anything already running.
constexpr uint16 GDT_SELECTOR_CODE = 0x08;              // index 1, ring 0
constexpr uint16 GDT_SELECTOR_DATA = 0x10;              // index 2, ring 0
// The user's descriptors differ from the kernel's only in their privilege level:
// same base, same 4 GiB limit.  Ring 3 code loads them with a requestor privilege
// level of 3, which is why the selector actually used is these values plus
// GDT_USER_RPL -- getting that wrong is a general protection fault on the first
// reload of a segment register in user mode.
constexpr uint16 GDT_SELECTOR_USER_CODE = 0x18;         // index 3, DPL 3
constexpr uint16 GDT_SELECTOR_USER_DATA = 0x20;         // index 4, DPL 3
constexpr uint16 GDT_SELECTOR_TSS = 0x28;               // index 5
constexpr uint16 GDT_USER_RPL = 3;
constexpr uint32 GDT_ENTRY_COUNT = 6;

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

void gdt_init(uint32 tss_address, uint32 tss_size);
// Sets one descriptor.  `granularity` carries the flags nibble in its high half
// (0xC0 for 4 KiB pages and 32-bit operands).
void gdt_set_entry(uint32 index, uint32 base, uint32 limit, uint8 access,
                   uint8 granularity);
// The access byte of a descriptor, read back by the self-test to prove the ring-3
// entries really are ring 3 instead of trusting the code that wrote them.
uint8 gdt_entry_access(uint32 index);
// What the CPU has loaded, read back with sgdt.  The self-test compares it with
// the table above, which is the difference between "lgdt was called" and "lgdt
// worked".
void gdt_read_loaded(GdtPointer* out);
// The table the kernel built: its linear address and its size in bytes.
uint32 gdt_address();
uint32 gdt_size();

}  // namespace myos
