// myos 32-bit kernel: paging behind kernel32/paging.h.

#include "paging.h"

#include "console.h"
#include "idt.h"
#include "libc.h"
#include "panic.h"
#include "pmm.h"

namespace myos {
namespace {

// CR0 bits this file sets.
constexpr uint32 CR0_PAGING = 0x80000000;
constexpr uint32 CR0_WRITE_PROTECT = 0x00010000;

uint32* g_directory = nullptr;
uint32 g_directory_physical = 0;
// The directory this file's mapping calls edit.  It starts out as the kernel's own
// directory and follows CR3 as tasks switch: a task that mapped a page would
// otherwise put it in whatever directory happened to be lying around in a variable
// rather than in the one the CPU is walking.
uint32 g_active = 0;
uint32 g_identity_end = 0;
uint32 g_mapped_pages = 0;
uint32 g_page_tables = 0;
uint32 g_faults = 0;

struct LazyRegion {
    bool used;
    uint32 first;               // page-aligned
    uint32 last;                // exclusive
};

LazyRegion g_lazy[PAGING_MAX_LAZY];

uint32 directory_index(uint32 address) {
    return address >> 22;
}

uint32 table_index(uint32 address) {
    return (address >> 12) & (PAGE_ENTRIES - 1);
}

// The entries this file edits.  Before paging_init() there is nothing to edit at all;
// after it, g_active names the directory the current task runs in.
uint32* active_entries() {
    if (g_active != 0) {
        return reinterpret_cast<uint32*>(g_active);
    }
    return g_directory;
}

uint32* table_for(uint32 address, bool create) {
    uint32* directory = active_entries();
    if (directory == nullptr) {
        return nullptr;
    }
    const uint32 index = directory_index(address);
    const uint32 entry = directory[index];
    if ((entry & PAGE_PRESENT) != 0) {
        return reinterpret_cast<uint32*>(entry & PAGE_ADDRESS_MASK);
    }
    if (!create) {
        return nullptr;
    }
    const uint32 physical = pmm_alloc_page();
    if (physical == 0) {
        return nullptr;
    }
    uint32* table = reinterpret_cast<uint32*>(physical);
    memset(table, 0, PAGE_SIZE);
    // Kernel-only, like every other mapping the kernel makes.
    directory[index] = physical | PAGE_PRESENT | PAGE_WRITE;
    ++g_page_tables;
    return table;
}

// One page table covers 4 MiB, so the identity map is filled a table at a time: 1024
// PTEs written in a loop instead of 1024 calls to page_map(), and no `invlpg`,
// because paging is not on yet and the TLB cannot hold anything stale.
uint32 identity_map(uint32 end) {
    for (uint32 base = 0; base < end; base += PAGE_TABLE_SPAN) {
        const uint32 physical = pmm_alloc_page();
        if (physical == 0) {
            return base;                // out of memory: map what we can, report it
        }
        uint32* table = reinterpret_cast<uint32*>(physical);
        for (uint32 index = 0; index < PAGE_ENTRIES; ++index) {
            table[index] = (base + index * PAGE_SIZE) | PAGE_PRESENT | PAGE_WRITE;
        }
        g_directory[directory_index(base)] = physical | PAGE_PRESENT | PAGE_WRITE;
        ++g_page_tables;
        g_mapped_pages += PAGE_ENTRIES;
    }
    return end;
}

}  // namespace

void paging_init() {
    const uint32 directory = pmm_alloc_page();
    if (directory == 0) {
        panic("no physical memory for the page directory");
    }
    g_directory_physical = directory;
    g_directory = reinterpret_cast<uint32*>(directory);
    memset(g_directory, 0, PAGE_SIZE);
    // Task 0 -- the boot context -- runs in the kernel's own directory.  Every task
    // created later gets a copy of it.
    g_active = directory;

    // Cover every page the allocator can hand out, and at least the first 16 MiB:
    // below that live the video buffer, the boot info block and everything else the
    // kernel takes for granted.
    uint32 end = pmm_stats().highest_usable;
    const uint32 minimum = 16u * 1024u * 1024u;
    const uint32 cap = PMM_MAX_PAGES * PAGE_SIZE;
    if (end < minimum) {
        end = minimum;
    }
    if (end > cap) {
        end = cap;
    }
    end = (end + PAGE_TABLE_SPAN - 1) & ~(PAGE_TABLE_SPAN - 1);
    g_identity_end = identity_map(end);

    write_cr3(g_directory_physical);
    write_cr0(read_cr0() | CR0_PAGING | CR0_WRITE_PROTECT);
}

uint32 page_directory_physical() {
    return g_directory_physical;
}

uint32 paging_active_directory() {
    return g_active;
}

uint32 paging_clone_directory() {
    uint32* source = active_entries();
    if (source == nullptr) {
        return 0;
    }
    const uint32 physical = pmm_alloc_page();
    if (physical == 0) {
        return 0;
    }
    uint32* copy = reinterpret_cast<uint32*>(physical);
    // A shallow copy on purpose.  Page tables are shared: both directories point at
    // the same tables, so the kernel's identity map is the same memory in every
    // address space and switching CR3 does not take the kernel out from under itself.
    // What is *not* shared is the directory itself, which is where a task's own
    // mappings (a user image, later a private stack) will go.
    for (uint32 index = 0; index < PAGE_ENTRIES; ++index) {
        copy[index] = source[index];
    }
    return physical;
}

void paging_switch_directory(uint32 directory) {
    if (directory == 0 || directory == g_active) {
        return;
    }
    g_active = directory;
    write_cr3(directory);
}

uint32 paging_identity_end() {
    return g_identity_end;
}

uint32 paging_mapped_pages() {
    return g_mapped_pages;
}

uint32 paging_page_tables() {
    return g_page_tables;
}

uint32 paging_faults() {
    return g_faults;
}

uint32 page_translate(uint32 address) {
    if (g_directory == nullptr) {
        return 0;
    }
    const uint32* table = table_for(address, false);
    if (table == nullptr) {
        return 0;
    }
    const uint32 entry = table[table_index(address)];
    if ((entry & PAGE_PRESENT) == 0) {
        return 0;
    }
    return (entry & PAGE_ADDRESS_MASK) | (address & (PAGE_SIZE - 1));
}

uint32 page_flags(uint32 address) {
    if (g_directory == nullptr) {
        return 0;
    }
    const uint32* table = table_for(address, false);
    if (table == nullptr) {
        return 0;
    }
    const uint32 entry = table[table_index(address)];
    return (entry & PAGE_PRESENT) != 0 ? entry : 0;
}

bool page_is_mapped(uint32 address) {
    return page_translate(address) != 0;
}

bool page_map(uint32 address, uint32 physical, uint32 flags) {
    uint32* table = table_for(address, true);
    if (table == nullptr) {
        return false;
    }
    const uint32 index = table_index(address);
    if ((table[index] & PAGE_PRESENT) == 0) {
        ++g_mapped_pages;
    }
    table[index] = (physical & PAGE_ADDRESS_MASK) |
                   (flags & ~PAGE_ADDRESS_MASK) | PAGE_PRESENT;
    if ((flags & PAGE_USER) != 0) {
        // Both levels have to allow the access.  A present, user-accessible page under
        // a supervisor-only page directory entry is a page ring 3 cannot touch, and the
        // fault it takes is reported as "present, user" -- which points at the page and
        // not at the directory entry that actually refused it.  That cost one debugging
        // round; the panic's error code is what gave it away.
        uint32* directory = active_entries();
        if (directory != nullptr) {
            directory[directory_index(address)] |= PAGE_USER;
        }
    }
    page_tlb_flush(address);
    return true;
}

bool page_unmap(uint32 address) {
    uint32* table = table_for(address, false);
    if (table == nullptr) {
        return false;
    }
    const uint32 index = table_index(address);
    if ((table[index] & PAGE_PRESENT) == 0) {
        return false;
    }
    table[index] = 0;
    if (g_mapped_pages > 0) {
        --g_mapped_pages;
    }
    // The page table itself is left in place: an empty table costs 4 KiB and is
    // exactly what a later mapping of the same 4 MiB would ask for again.
    page_tlb_flush(address);
    return true;
}

void page_tlb_flush(uint32 address) {
    if (address != 0) {
        invalidate_page(address);
        return;
    }
    // Reloading CR3 flushes the whole TLB, which is what "flush address 0" means:
    // there is no way to say "everything" to invlpg.
    write_cr3(g_active != 0 ? g_active : g_directory_physical);
}

bool paging_reserve_lazy(uint32 address, uint32 bytes) {
    if (bytes == 0) {
        return false;
    }
    const uint32 first = address & PAGE_ADDRESS_MASK;
    const uint32 last = (address + bytes + PAGE_SIZE - 1) & PAGE_ADDRESS_MASK;

    // Reserving the same range twice is not an error: the self-test does exactly
    // that when it is run a second time, and the promise ("it is not mapped yet") is
    // still true for pages nothing has touched.
    for (const LazyRegion& region : g_lazy) {
        if (region.used && region.first == first && region.last == last) {
            return true;
        }
    }
    for (uint32 index = first; index < last; index += PAGE_SIZE) {
        if (page_is_mapped(index)) {
            return false;               // it would never fault, so nothing to promise
        }
    }
    for (LazyRegion& region : g_lazy) {
        if (!region.used) {
            region.used = true;
            region.first = first;
            region.last = last;
            return true;
        }
    }
    return false;
}

bool paging_handle_fault(uint32 address, uint32 error) {
    // Present but refused: a write to a read-only page, which is copy-on-write's
    // business and not something this handler can invent an answer for.
    if ((error & PAGE_PRESENT) != 0) {
        return false;
    }
    for (const LazyRegion& region : g_lazy) {
        if (!region.used || address < region.first || address >= region.last) {
            continue;
        }
        const uint32 physical = pmm_alloc_page();
        if (physical == 0) {
            return false;               // out of memory is not a recoverable fault
        }
        // Clear it *before* it becomes reachable: the identity map makes it
        // writable right away, and a page handed to a faulting read has to read as
        // zero, not as whatever the last owner left there.
        memset(reinterpret_cast<void*>(physical), 0, PAGE_SIZE);
        if (!page_map(address & PAGE_ADDRESS_MASK, physical,
                      PAGE_PRESENT | PAGE_WRITE)) {
            pmm_free_page(physical);
            return false;
        }
        ++g_faults;
        return true;
    }
    return false;
}

void paging_panic_fault(const Registers* regs, uint32 address, uint32 error) {
    // The error code says more than the address does: which access, from which ring,
    // and whether the page was there at all.  Printing it is the difference between
    // "a page fault" and "a write into unmapped memory from the kernel".
    kprintf("  cr2 %p (%s, %s, %s)\n", address,
            (error & PAGE_PRESENT) != 0 ? "present" : "not present",
            (error & 0x2) != 0 ? "write" : "read",
            (error & 0x4) != 0 ? "user" : "kernel");
    panic_registers(regs, "page fault");
}

void paging_report() {
    if (g_directory == nullptr) {
        console_puts("paging: off (no page directory)\n");
        return;
    }
    kprintf("paging: cr3 %p, %u page tables, %u pages mapped\n",
            g_active != 0 ? g_active : g_directory_physical, g_page_tables,
            g_mapped_pages);
    kprintf("paging: identity map 00000000..%p, %u fault(s) handled\n",
            g_identity_end, g_faults);
    uint32 regions = 0;
    for (const LazyRegion& region : g_lazy) {
        if (region.used) {
            ++regions;
        }
    }
    kprintf("paging: %u demand-zero region(s)\n", regions);
}

}  // namespace myos
