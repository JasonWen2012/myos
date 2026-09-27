// myos 32-bit kernel: the interrupt descriptor table.

#pragma once

#include "types.h"

namespace myos {

// The 8259 pair is remapped to these, so the vectors the CPU sees do not collide
// with the exceptions below them (0..31) or with the BIOS's own vectors.
constexpr uint8 IRQ_BASE = 32;
constexpr uint8 IRQ_COUNT = 16;
constexpr uint8 EXCEPTION_COUNT = 32;

// One interrupt gate.  Flags 0x8E is "present, ring 0, 32-bit interrupt gate":
// an interrupt gate clears IF on entry, which keeps an exception handler from
// being interrupted by the timer halfway through reporting itself.
struct IdtEntry {
    uint16 offset_low;
    uint16 selector;
    uint8 zero;
    uint8 flags;
    uint16 offset_high;
};
static_assert(sizeof(IdtEntry) == 8, "an IDT gate is eight bytes");

struct IdtPointer {
    uint16 limit;
    uint32 base;
} __attribute__((packed));
static_assert(sizeof(IdtPointer) == 6, "lidt reads six bytes");

// What the assembly stubs leave on the stack when they call the dispatcher.
// The field order is the push order in kernel32/isr.asm, from the lowest address
// up: segment registers, then the eight general registers pushed by `pushad`
// (EDI last, so lowest), then the vector and error code the stub pushed, then the
// frame the CPU pushed itself.
struct Registers {
    uint32 gs, fs, es, ds;
    uint32 edi, esi, ebp, esp_unused, ebx, edx, ecx, eax;
    uint32 vector, error;
    uint32 eip, cs, eflags;
};

void idt_init();
void idt_set_gate(uint8 vector, uint32 handler, uint16 selector, uint8 flags);
// What the CPU has loaded, read back with sidt: proves lidt took effect.
void idt_read_loaded(IdtPointer* out);
// The kernel's IDT: its linear address and its size in bytes.
uint32 idt_address();
uint32 idt_size();
// A gate's descriptor privilege level and present bit, read from the table the
// kernel built: the self-test's way of asking "can ring 3 actually use int 0x80?"
// rather than "did we pass the right flags to the function that built it?".
uint8 idt_gate_dpl(uint8 vector);
bool idt_gate_present(uint8 vector);

// A device driver registers what to run when its IRQ arrives; the dispatcher
// knows nothing about timers or keyboards, which is what keeps it short enough to
// be obviously right.  The handler runs with interrupts disabled and must not
// block.  Sending the end-of-interrupt to the PIC is the dispatcher's job, so a
// driver cannot forget it and wedge the whole interrupt system.
using IrqHandler = void (*)();
void irq_install_handler(uint8 irq, IrqHandler handler);

// Called from the assembly stubs in kernel32/isr.asm, so these two keep C
// linkage: the stubs name them directly and cannot spell a mangled name.
extern "C" {
void isr_dispatch(Registers* regs);
void irq_dispatch(Registers* regs);
}

}  // namespace myos
