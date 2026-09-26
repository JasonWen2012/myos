// myos 32-bit kernel: the interrupt descriptor table.

#include "idt.h"

#include "console.h"
#include "cpu.h"
#include "gdt.h"
#include "pic.h"
#include "types.h"

extern "C" {
// Filled in by kernel32/isr.asm: one stub per vector, in vector order.
extern myos::uint32 isr_stub_table[];
void idt_load(const myos::IdtPointer* pointer);
void read_idtr(myos::IdtPointer* out);
}

namespace myos {
namespace {

constexpr uint32 IDT_ENTRIES = 256;
constexpr uint8 GATE_INTERRUPT_32 = 0x8E;
IdtEntry idt[IDT_ENTRIES];
IdtPointer idt_pointer;
IrqHandler irq_handlers[IRQ_COUNT];

const char* const exception_names[EXCEPTION_COUNT] = {
    "divide error", "debug", "non-maskable interrupt", "breakpoint",
    "overflow", "bound range", "invalid opcode", "device not available",
    "double fault", "coprocessor segment", "invalid TSS", "segment not present",
    "stack fault", "general protection", "page fault", "reserved",
    "x87 floating point", "alignment check", "machine check", "SIMD floating point",
    "virtualisation", "control protection", "reserved", "reserved",
    "reserved", "reserved", "reserved", "reserved",
    "hypervisor", "VMM communication", "security", "reserved",
};

}  // namespace

void idt_set_gate(uint8 vector, uint32 handler, uint16 selector, uint8 flags) {
    IdtEntry& gate = idt[vector];
    gate.offset_low = static_cast<uint16>(handler & 0xFFFF);
    gate.offset_high = static_cast<uint16>((handler >> 16) & 0xFFFF);
    gate.selector = selector;
    gate.zero = 0;
    gate.flags = flags;
}

void idt_init() {
    for (uint32 vector = 0; vector < IDT_ENTRIES; ++vector) {
        idt_set_gate(static_cast<uint8>(vector), 0, 0, 0);
    }
    // isr_stub_table covers vectors 0..47: the 32 exceptions and the 16 IRQs,
    // which the PIC is remapped to right after this.
    for (uint32 vector = 0; vector < EXCEPTION_COUNT + IRQ_COUNT; ++vector) {
        idt_set_gate(static_cast<uint8>(vector), isr_stub_table[vector],
                     GDT_SELECTOR_CODE, GATE_INTERRUPT_32);
    }
    idt_pointer.limit = static_cast<uint16>(sizeof(idt) - 1);
    idt_pointer.base = reinterpret_cast<uint32>(&idt[0]);
    idt_load(&idt_pointer);
}

void idt_read_loaded(IdtPointer* out) {
    read_idtr(out);
}

uint32 idt_address() {
    return idt_pointer.base;
}

uint32 idt_size() {
    return sizeof(idt);
}

void irq_install_handler(uint8 irq, IrqHandler handler) {
    if (irq < IRQ_COUNT) {
        irq_handlers[irq] = handler;
    }
}

extern "C" void isr_dispatch(Registers* regs) {
    const char* name = "unknown";
    if (regs->vector < EXCEPTION_COUNT) {
        name = exception_names[regs->vector];
    }
    // Printed before halting, because there is no IDT entry that can fail here and
    // a triple fault would erase the evidence.
    kprintf("\nEXCEPTION %u (%s) at %p, error %x\n", regs->vector, name,
            regs->eip, regs->error);
    kprintf("  eax %x ebx %x ecx %x edx %x\n", regs->eax, regs->ebx,
            regs->ecx, regs->edx);
    kprintf("  esi %x edi %x ebp %x esp %x\n", regs->esi, regs->edi,
            regs->ebp, regs->esp_unused);
    kprintf("  cs %x ds %x eflags %x\n", regs->cs, regs->ds, regs->eflags);
    kprintf("system halted.\n");
    hlt_forever();
}

extern "C" void irq_dispatch(Registers* regs) {
    const uint8 irq = static_cast<uint8>(regs->vector - IRQ_BASE);
    if (irq < IRQ_COUNT && irq_handlers[irq] != nullptr) {
        irq_handlers[irq]();
    }
    // Always, even with no handler: a maskable interrupt that is never
    // acknowledged is never delivered again, so the timer would stop dead the
    // first time an unclaimed IRQ arrived.
    pic_send_eoi(irq);
}

}  // namespace myos
