// myos 32-bit kernel: the global descriptor table.
//
// The loader already installed a flat GDT so that it could enter protected mode,
// and this one is deliberately identical in effect.  Loading our own matters for
// two reasons: the loader's table lives in low memory that nothing else owns, and
// a kernel that cannot describe its own memory has no business growing.

#include "gdt.h"

#include "console.h"
#include "types.h"

extern "C" {
// Defined in boot.asm: loads the table and reloads every segment register,
// including CS (which needs a far jump, not a move).
void gdt_load(const myos::GdtPointer* pointer, myos::uint16 code, myos::uint16 data);
void read_gdtr(myos::GdtPointer* out);
}

namespace myos {
namespace {

constexpr uint32 GDT_ENTRIES = 3;          // null, code, data
GdtEntry gdt[GDT_ENTRIES];
GdtPointer gdt_pointer;

}  // namespace

void gdt_set_entry(uint32 index, uint32 base, uint32 limit, uint8 access,
                   uint8 granularity) {
    GdtEntry& entry = gdt[index];
    entry.base_low = static_cast<uint16>(base & 0xFFFF);
    entry.base_middle = static_cast<uint8>((base >> 16) & 0xFF);
    entry.base_high = static_cast<uint8>((base >> 24) & 0xFF);
    entry.limit_low = static_cast<uint16>(limit & 0xFFFF);
    entry.granularity = static_cast<uint8>(((limit >> 16) & 0x0F) | (granularity & 0xF0));
    entry.access = access;
}

void gdt_init() {
    // 0x00: the null descriptor the CPU requires as entry zero.
    gdt_set_entry(0, 0, 0, 0, 0);
    // 0x08: 32-bit code, base 0, limit 4 GiB, ring 0.
    gdt_set_entry(1, 0, 0x000FFFFF, 0x9A, 0xC0);
    // 0x10: 32-bit data, base 0, limit 4 GiB, ring 0.
    gdt_set_entry(2, 0, 0x000FFFFF, 0x92, 0xC0);

    gdt_pointer.limit = static_cast<uint16>(sizeof(gdt) - 1);
    gdt_pointer.base = reinterpret_cast<uint32>(&gdt[0]);
    gdt_load(&gdt_pointer, GDT_SELECTOR_CODE, GDT_SELECTOR_DATA);
}

void gdt_read_loaded(GdtPointer* out) {
    read_gdtr(out);
}

uint32 gdt_address() {
    return gdt_pointer.base;
}

uint32 gdt_size() {
    return sizeof(gdt);
}

}  // namespace myos
