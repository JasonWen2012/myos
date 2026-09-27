// myos 32-bit kernel: physical memory.
//
// One bit per 4 KiB page, filled in from the E820 map the loader collected in real
// mode.  Only memory at or above 1 MiB is managed: everything below it holds the
// interrupt vector table, the BIOS data area, the loader's staging copy of the
// kernel image and the video buffer, none of which the allocator has any business
// handing out.
//
// The bitmap is sized rather than dynamic, because the allocator runs before there
// is any memory to allocate from.  A machine with more RAM than the bitmap covers
// is not a problem: the extra is counted and reported as ignored, and the kernel
// keeps working on the part it can manage.

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 PAGE_SIZE = 4096;
// 128 MiB of physical memory, one bit per page: 4 KiB of bitmap in .bss.  QEMU is
// given 32 MiB here, so this is five times what the tests need and still a size
// that costs nothing to carry.
constexpr uint32 PMM_MAX_PAGES = (128u * 1024u) / 4u;
constexpr uint32 PMM_MIN_ADDRESS = 0x00100000;      // nothing below 1 MiB is managed

struct PmmStats {
    uint32 managed_pages;       // usable RAM pages the allocator started with
    uint32 free_pages;
    uint32 used_pages;          // managed minus free
    uint32 ignored_pages;       // usable RAM above PMM_MAX_PAGES, reported not used
    uint32 highest_usable;      // exclusive end of usable RAM, in bytes, clamped
};

// Reads the E820 map, marks every usable page at or above 1 MiB free and everything
// else used.  Safe to call twice (the second call re-reads the map and starts over),
// though nothing does.
void pmm_init();

// Reserve a range the kernel already occupies (the image, for example).  Call after
// pmm_init() and before the first allocation.
void pmm_reserve(uint32 address, uint32 bytes);

// A physical address, or 0 for "no memory left" -- page 0 is never handed out, so
// zero is unambiguous.
uint32 pmm_alloc_page();
// `count` *contiguous* pages, or 0.  The heap asks for its arena this way, because a
// heap whose pages are scattered is not a heap.
uint32 pmm_alloc_pages(uint32 count);
void pmm_free_page(uint32 address);
void pmm_free_pages(uint32 address, uint32 count);

// True when the allocator manages this page: aligned, inside the cap, and not below
// the 1 MiB line.
bool pmm_owns(uint32 address);

PmmStats pmm_stats();
void pmm_report();

}  // namespace myos
