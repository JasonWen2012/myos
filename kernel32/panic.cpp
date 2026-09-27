// myos 32-bit kernel: the stopping report behind kernel32/panic.h.

#include "panic.h"

#include "console.h"
#include "cpu.h"

namespace myos {
namespace {

// Matches DEBUG_EXIT_FAIL in boot/boot.inc: QEMU's isa-debug-exit device turns the
// write into a process status of (value << 1) | 1, so a panic ends a headless run
// with a failing status instead of a timeout.
constexpr uint32 DEBUG_EXIT_FAIL = 0x02;

void halt_with_failure() {
    debug_exit(DEBUG_EXIT_FAIL);
    // On real hardware the port above is inert, so the machine has to actually stop.
    hlt_forever();
}

}  // namespace

void panic(const char* reason) {
    kprintf("\nKERNEL PANIC: %s\n", reason);
    halt_with_failure();
}

void panic_registers(const Registers* regs, const char* reason) {
    kprintf("\nKERNEL PANIC: %s\n", reason);
    kprintf("  vector %u at %p, error %x\n", regs->vector, regs->eip, regs->error);
    kprintf("  eax %x ebx %x ecx %x edx %x\n", regs->eax, regs->ebx,
            regs->ecx, regs->edx);
    kprintf("  esi %x edi %x ebp %x esp %x\n", regs->esi, regs->edi,
            regs->ebp, regs->esp_unused);
    kprintf("  cs %x ds %x eflags %x\n", regs->cs, regs->ds, regs->eflags);
    halt_with_failure();
}

}  // namespace myos
