// myos 32-bit kernel: the E820 memory map the loader collected.

#include "mem.h"

#include "bootinfo.h"
#include "console.h"
#include "libc.h"
#include "types.h"

namespace myos {
namespace {

constexpr uint32 E820_TYPE_USABLE = 1;

// The loader caps the map at MEMORY_MAP_MAX_ENTRIES and normalises every entry to
// the kernel's 24-byte shape, so the count is the only thing to clamp here.  The
// entry size it recorded is reported as information: a firmware that uses the older
// 20-byte form is worth being able to see, but it no longer changes how the map is
// read.
uint32 usable_entries(const MemoryMapEntry** first) {
    const BootInfo* info = boot_info();
    *first = info->memory_map;
    return info->memory_map_count > MEMORY_MAP_MAX_ENTRIES
               ? MEMORY_MAP_MAX_ENTRIES : info->memory_map_count;
}

const char* entry_kind(uint32 type) {
    switch (type) {
        case 1: return "usable";
        case 2: return "reserved";
        case 3: return "ACPI reclaimable";
        case 4: return "ACPI NVS";
        case 5: return "unusable";
        default: return "other";
    }
}

}  // namespace

uint32 mem_entry_count() {
    const MemoryMapEntry* first = nullptr;
    return usable_entries(&first);
}

uint32 mem_usable_kib() {
    const MemoryMapEntry* entries = nullptr;
    const uint32 count = usable_entries(&entries);
    uint64 kib = 0;
    for (uint32 i = 0; i < count; ++i) {
        if (entries[i].type == E820_TYPE_USABLE) {
            kib += entries[i].length / 1024;
        }
    }
    // 64-bit arithmetic on i386 compiles to two 32-bit halves; the value is
    // reported in KiB so 32 bits is plenty for any machine this kernel will see.
    return static_cast<uint32>(kib);
}

void mem_report() {
    const BootInfo* info = boot_info();
    const uint32 count = mem_entry_count();
    kprintf("%u KiB usable in %u firmware memory map entries\n",
            mem_usable_kib(), count);
    kprintf("loader reported %u-byte entries\n", info->memory_map_entry_size);

    const MemoryMapEntry* entries = info->memory_map;
    for (uint32 i = 0; i < count; ++i) {
        const uint64 base = entries[i].base;
        const uint64 length = entries[i].length;
        kprintf("  %08x%08x..%08x%08x %s\n",
                static_cast<uint32>(base >> 32), static_cast<uint32>(base),
                static_cast<uint32>((base + length) >> 32),
                static_cast<uint32>(base + length),
                entry_kind(entries[i].type));
    }

    kprintf("myos kernel image %u bytes at %p, stack inside its own .bss\n",
            image_header.size, KERNEL_LOAD_ADDRESS);
}

}  // namespace myos
