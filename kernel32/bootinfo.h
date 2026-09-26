// myos 32-bit kernel: the boot information the loader leaves for us.
//
// The loader writes this block at a fixed low address before it opens the A20 gate
// and leaves real mode, so the kernel reads it at a known address instead of being
// handed a pointer.  boot/boot.inc defines the address and the same layout in
// assembly, and a test compares the two definitions, because a silent disagreement
// here would show up as a plausible-looking memory map of garbage.

#pragma once

#include "types.h"

namespace myos {

// Keep in step with BOOT_INFO_LIN in boot/boot.inc.
constexpr uintptr BOOT_INFO_ADDRESS = 0x00006000;
constexpr uint32 BOOT_INFO_MAGIC = 0x4942594D;   // 'M','Y','B','I' little endian
constexpr uint32 BOOT_INFO_VERSION = 1;
constexpr uint32 MEMORY_MAP_MAX_ENTRIES = 32;

// One raw INT 15h E820 entry.  Kept raw rather than unpacked: the kernel reports
// it, and unpacking happens where the values are used.  The loader normalises
// every entry to this shape -- a firmware that writes the older 20-byte form has
// the slot's tail zeroed -- so the kernel can index the map at a fixed stride.
struct MemoryMapEntry {
    uint64 base;
    uint64 length;
    uint32 type;
    uint32 extended;
};
static_assert(sizeof(MemoryMapEntry) == 24, "E820 entries are 24 bytes");

struct BootInfo {
    char magic[4];              // +0  "MYBI"
    uint8 version;              // +4
    uint8 boot_drive;           // +5
    uint8 vga_mode;             // +6
    uint8 vga_page;             // +7
    uint32 memory_map_count;    // +8
    uint32 memory_map_entry_size;  // +12, as the firmware reported it (informative)
    uint32 reserved[4];         // +16..31
    MemoryMapEntry memory_map[MEMORY_MAP_MAX_ENTRIES];  // +32
};

static_assert(__builtin_offsetof(BootInfo, memory_map_count) == 8,
              "the loader writes the entry count at +8");
static_assert(__builtin_offsetof(BootInfo, memory_map_entry_size) == 12,
              "the loader writes the entry size at +12");
static_assert(__builtin_offsetof(BootInfo, memory_map) == 32,
              "the loader writes the first E820 entry at +32");

// The loaded image, and the header the loader validated at its start.
constexpr uintptr KERNEL_LOAD_ADDRESS = 0x00100000;
// Where the loader staged the image before copying it up.  The kernel can still
// read both copies, which is what makes "did the copy work" a checkable question
// rather than an assumption.  Keep in step with boot/boot.inc.
constexpr uintptr KERNEL32_STAGE_LIN = 0x00010000;
constexpr uint32 IMAGE_MAGIC = 0x534F594D;       // 'M','Y','O','S'
constexpr uint32 IMAGE_HEADER_SIZE = 16;

struct ImageHeader {
    uint32 magic;      // +0
    uint8 arch;        // +4
    uint8 version;     // +5
    uint8 flags;       // +6
    uint8 checksum;    // +7
    uint32 entry;      // +8
    uint32 size;       // +12
};
static_assert(sizeof(ImageHeader) == IMAGE_HEADER_SIZE, "the header is 16 bytes");

// Defined in kernel32/boot.asm and placed at offset 0 of the image by
// cofllink.kernel_layout(), which is where the loader reads it from.  Reading it
// through the symbol rather than through KERNEL_LOAD_ADDRESS keeps the two from
// drifting apart.
extern "C" const ImageHeader image_header;

inline const BootInfo* boot_info() {
    return reinterpret_cast<const BootInfo*>(BOOT_INFO_ADDRESS);
}

}  // namespace myos
