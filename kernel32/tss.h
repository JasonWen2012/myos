// myos 32-bit kernel: the task state segment.
//
// Only two fields of it matter yet: `ss0` and `esp0`, which the CPU loads when an
// interrupt or a trap takes it from ring 3 to ring 0.  Without them a syscall from
// user mode would push its frame onto the *user's* stack -- the stack the user
// program controls -- and the kernel would be running on memory the program it is
// supposed to be protecting itself from can write.
//
// The rest of the structure is the 386's idea of hardware task switching, which this
// kernel does not use: it switches stacks and page directories by hand because that
// is what it can explain.  The I/O map base is set past the end of the structure,
// which means "no bitmap", and with IOPL 0 in the user's flags that denies ring 3
// every `in`/`out` instruction there is.

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 TSS_KERNEL_STACK_PAGES = 4;        // 16 KiB per user context

struct Tss {
    uint32 previous_task;
    uint32 esp0;                // the stack an interrupt from ring 3 switches to
    uint32 ss0;
    uint32 esp1;
    uint32 ss1;
    uint32 esp2;
    uint32 ss2;
    uint32 cr3;
    uint32 eip;
    uint32 eflags;
    uint32 eax, ecx, edx, ebx;
    uint32 esp, ebp, esi, edi;
    uint32 es, cs, ss, ds, fs, gs;
    uint32 ldt;
    uint16 trap;
    uint16 iomap_base;
} __attribute__((packed));
static_assert(sizeof(Tss) == 104, "a 32-bit TSS is 104 bytes");

// Fills the TSS in, points ss0/esp0 at the kernel stack the kernel may use while a
// user program is running, and loads it with `ltr`.
void tss_init(uint32 kernel_stack_top);
// Moves the ring-0 stack the CPU will use.  Every context switch does this: the
// scheduler points esp0 at the stack of the task it is switching to, and exec_user()
// points it at a temporary stack for the ring-3 program it runs.  Both have to, for
// the same reason -- a trap taken from ring 3 must land on the kernel stack of
// whatever is actually running.
void tss_set_kernel_stack(uint32 kernel_stack_top);
// Puts esp0 back where tss_init() left it.  Called when a user program exits: leaving
// it at zero would mean the TSS names no stack at all, and the next trap taken from
// ring 3 would push its frame at address zero.
void tss_restore_boot_stack();
uint32 tss_kernel_stack();
uint16 tss_selector();
uint32 tss_address();
// The selector the CPU actually has loaded, read with `str`: a self-test that only
// checked the variable would be testing the kernel's bookkeeping, not the CPU.
uint16 tss_loaded_selector();

}  // namespace myos
