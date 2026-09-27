// myos 32-bit kernel: virtual memory.
//
// Two-level paging (page directory -> page table, 4 KiB pages, no PAE), built once
// at boot as an identity map covering all the RAM the allocator can hand out, and
// then edited a page at a time.  Identity first is the whole trick: the kernel keeps
// executing, its data keeps being addressable, and the page tables themselves (which
// live in physical pages the allocator gave us) stay reachable while they are built.
//
// Everything the kernel maps is mapped without PAGE_USER, so once a user task exists
// ring 3 cannot see any of it.  That is not a side effect, it is the point.

#pragma once

#include "pmm.h"        // PAGE_SIZE: a page is the unit both of these work in
#include "types.h"

namespace myos {

constexpr uint32 PAGE_PRESENT = 0x0001;
constexpr uint32 PAGE_WRITE = 0x0002;
constexpr uint32 PAGE_USER = 0x0004;
constexpr uint32 PAGE_ACCESSED = 0x0020;        // set by the CPU
constexpr uint32 PAGE_DIRTY = 0x0040;           // set by the CPU
constexpr uint32 PAGE_ADDRESS_MASK = 0xFFFFF000;
constexpr uint32 PAGE_ENTRIES = 1024;
constexpr uint32 PAGE_TABLE_SPAN = PAGE_ENTRIES * PAGE_SIZE;    // 4 MiB per table

// A range that stays unmapped until something touches it, and reads as zero when it
// finally does.  This is the one page fault the kernel can recover from today; the
// list is small because nothing needs more than a handful of them yet.
constexpr uint32 PAGING_MAX_LAZY = 8;

// CPU control that only assembly can express, kept beside the paging code that is
// the only user of it.
static inline uint32 read_cr0() {
    uint32 value;
    asm volatile("mov %%cr0, %0" : "=r"(value));
    return value;
}

static inline void write_cr0(uint32 value) {
    asm volatile("mov %0, %%cr0" : : "r"(value) : "memory");
}

static inline uint32 read_cr2() {
    uint32 value;
    asm volatile("mov %%cr2, %0" : "=r"(value));
    return value;
}

static inline uint32 read_cr3() {
    uint32 value;
    asm volatile("mov %%cr3, %0" : "=r"(value));
    return value;
}

static inline void write_cr3(uint32 value) {
    asm volatile("mov %0, %%cr3" : : "r"(value) : "memory");
}

static inline void invalidate_page(uint32 address) {
    asm volatile("invlpg (%0)" : : "r"(address) : "memory");
}

// Builds the page directory, identity-maps RAM, and turns paging on.  Must run after
// pmm_init() and after every reservation the kernel wants (the image, in practice).
void paging_init();

uint32 page_directory_physical();
uint32 paging_identity_end();
uint32 paging_mapped_pages();
uint32 paging_page_tables();
uint32 paging_faults();

// The directory every mapping call in this file edits, and the one CR3 is expected
// to hold.  They are two different questions -- "which directory does the kernel
// think it is building" and "which one is the CPU walking" -- and the self-test asks
// both, because a task switch that changed CR3 without changing this would leave the
// kernel editing a directory nothing was using.
uint32 paging_active_directory();

// A private copy of the active directory for a new task: one physical page of
// directory entries, sharing every page table with the active directory, so the
// kernel's own mappings come along unchanged and the task can have its own user part
// later.  Returns 0 when there is no page to spare.
uint32 paging_clone_directory();

// Makes a directory the active one and loads it into CR3.  Reloading CR3 flushes the
// whole TLB, which is exactly what is needed after the address space under the
// kernel's feet has changed.
void paging_switch_directory(uint32 directory);

uint32 page_translate(uint32 address);          // 0 when unmapped
uint32 page_flags(uint32 address);              // 0 when unmapped
bool page_is_mapped(uint32 address);
bool page_map(uint32 address, uint32 physical, uint32 flags);
bool page_unmap(uint32 address);
void page_tlb_flush(uint32 address);

// Reserves a range for demand-zero allocation.  Fails when the range is already
// mapped, because then the first touch would not fault and the promise would be a
// lie.
bool paging_reserve_lazy(uint32 address, uint32 bytes);

// The page-fault hook: true means "allocated and mapped; retry the instruction".
bool paging_handle_fault(uint32 address, uint32 error);
// Prints what the fault was about, then panics.  Never returns.
void paging_panic_fault(const struct Registers* regs, uint32 address, uint32 error);

void paging_report();

}  // namespace myos
