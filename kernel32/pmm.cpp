// myos 32-bit kernel: the physical page allocator behind kernel32/pmm.h.

#include "pmm.h"

#include "bootinfo.h"
#include "console.h"
#include "libc.h"

namespace myos {
namespace {

constexpr uint32 BITMAP_WORDS = PMM_MAX_PAGES / 32;
uint32 g_bitmap[BITMAP_WORDS];

uint32 g_managed = 0;               // pages that started free
uint32 g_free = 0;
uint32 g_ignored = 0;               // usable pages the bitmap cannot cover
uint32 g_highest_usable = 0;
uint32 g_hint = 0;                  // allocation starts here and only moves forward

bool page_used(uint32 page) {
    return (g_bitmap[page / 32] >> (page % 32)) & 1u;
}

void set_page_used(uint32 page, bool used) {
    const uint32 mask = 1u << (page % 32);
    if (used) {
        g_bitmap[page / 32] |= mask;
    } else {
        g_bitmap[page / 32] &= ~mask;
    }
}

void mark_free_range(uint32 first_page, uint32 last_page) {
    for (uint32 page = first_page; page < last_page; ++page) {
        if (page_used(page)) {
            set_page_used(page, false);
            ++g_free;
            ++g_managed;
        }
    }
}

// Marks every page the byte range touches.  A range that starts or ends inside a
// page takes the whole page: a reserved half-page is not a page anyone can use.
void mark_used_range(uint32 address, uint32 bytes) {
    if (bytes == 0) {
        return;
    }
    uint32 first = address / PAGE_SIZE;
    const uint32 last = (address + bytes + PAGE_SIZE - 1) / PAGE_SIZE;
    if (last <= first) {
        return;                     // wrapped: the caller passed nonsense
    }
    for (uint32 page = first; page < last && page < PMM_MAX_PAGES; ++page) {
        if (!page_used(page)) {
            set_page_used(page, true);
            if (g_free > 0) {
                --g_free;
            }
        }
    }
}

}  // namespace

void pmm_init() {
    memset(g_bitmap, 0xFF, sizeof(g_bitmap));       // everything used to begin with
    g_managed = 0;
    g_free = 0;
    g_ignored = 0;
    g_highest_usable = 0;
    g_hint = 0;

    const BootInfo* info = boot_info();
    for (uint32 index = 0; index < info->memory_map_count; ++index) {
        const MemoryMapEntry& entry = info->memory_map[index];
        if (entry.type != 1) {                      // 1 == usable RAM
            continue;
        }
        // The kernel is 32-bit: memory whose end does not fit in 32 bits is not
        // memory this kernel can address, so it is ignored rather than truncated
        // into a wrong range.
        if (entry.base > 0xFFFFFFFFull) {
            continue;
        }
        uint64 end64 = entry.base + entry.length;
        if (end64 > 0x100000000ull) {
            end64 = 0x100000000ull;
        }
        if (end64 > g_highest_usable) {
            g_highest_usable = static_cast<uint32>(end64);
        }
        uint32 start = static_cast<uint32>(entry.base);
        uint32 end = static_cast<uint32>(end64);
        if (start < PMM_MIN_ADDRESS) {
            start = PMM_MIN_ADDRESS;                // the low megabyte is spoken for
        }
        const uint32 cap = PMM_MAX_PAGES * PAGE_SIZE;
        if (start >= cap) {
            g_ignored += (end - start) / PAGE_SIZE;
            continue;
        }
        if (end > cap) {
            g_ignored += (end - cap) / PAGE_SIZE;
            end = cap;
        }
        if (end > start) {
            mark_free_range(start / PAGE_SIZE, (end + PAGE_SIZE - 1) / PAGE_SIZE);
        }
    }
}

void pmm_reserve(uint32 address, uint32 bytes) {
    mark_used_range(address, bytes);
}

uint32 pmm_alloc_page() {
    for (uint32 page = g_hint; page < PMM_MAX_PAGES; ++page) {
        if (!page_used(page)) {
            set_page_used(page, true);
            --g_free;
            g_hint = page;
            return page * PAGE_SIZE;
        }
    }
    // Nothing above the hint: a freed page below it is still a perfectly good page,
    // and refusing to look is how an allocator runs out of memory it has.
    for (uint32 page = 0; page < g_hint; ++page) {
        if (!page_used(page)) {
            set_page_used(page, true);
            --g_free;
            return page * PAGE_SIZE;
        }
    }
    return 0;
}

uint32 pmm_alloc_pages(uint32 count) {
    if (count == 0 || count > PMM_MAX_PAGES) {
        return 0;
    }
    uint32 run = 0;
    for (uint32 page = g_hint; page < PMM_MAX_PAGES; ++page) {
        if (page_used(page)) {
            run = 0;
            continue;
        }
        if (++run == count) {
            const uint32 first = page + 1 - count;
            for (uint32 index = first; index <= page; ++index) {
                set_page_used(index, true);
                --g_free;
            }
            g_hint = first;
            return first * PAGE_SIZE;
        }
    }
    return 0;
}

void pmm_free_page(uint32 address) {
    if (!pmm_owns(address)) {
        return;
    }
    const uint32 page = address / PAGE_SIZE;
    if (page_used(page)) {
        set_page_used(page, false);
        ++g_free;
        if (page < g_hint) {
            g_hint = page;
        }
    }
}

void pmm_free_pages(uint32 address, uint32 count) {
    for (uint32 index = 0; index < count; ++index) {
        pmm_free_page(address + index * PAGE_SIZE);
    }
}

bool pmm_owns(uint32 address) {
    if (address < PMM_MIN_ADDRESS || address >= PMM_MAX_PAGES * PAGE_SIZE) {
        return false;
    }
    if (address % PAGE_SIZE != 0) {
        return false;
    }
    return true;
}

PmmStats pmm_stats() {
    PmmStats stats;
    stats.managed_pages = g_managed;
    stats.free_pages = g_free;
    stats.used_pages = g_managed >= g_free ? g_managed - g_free : 0;
    stats.ignored_pages = g_ignored;
    stats.highest_usable = g_highest_usable;
    return stats;
}

void pmm_report() {
    const PmmStats stats = pmm_stats();
    kprintf("pmm: %u pages (%u KiB) managed, %u free, %u used\n",
            stats.managed_pages, stats.managed_pages * (PAGE_SIZE / 1024),
            stats.free_pages, stats.used_pages);
    kprintf("pmm: usable RAM ends at %p", stats.highest_usable);
    if (stats.ignored_pages != 0) {
        kprintf(", %u page(s) above the %u KiB cap are ignored",
                stats.ignored_pages, (PMM_MAX_PAGES * PAGE_SIZE) / 1024);
    }
    console_putc('\n');
}

}  // namespace myos
