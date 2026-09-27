// myos 32-bit kernel: the interrupt descriptor table.

#include "idt.h"

#include "console.h"
#include "cpu.h"
#include "gdt.h"
#include "panic.h"
#include "paging.h"
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
// The same gate with DPL 3, so ring 3 is allowed through it: bit 5 (0x20) of the
// flags byte is the second bit of the descriptor privilege level.  A gate without it
// raises a general protection fault for the *caller*, which looks like a broken
// syscall instruction rather than a missing permission.
constexpr uint8 GATE_INTERRUPT_32_USER = 0xEE;
constexpr uint8 SYSCALL_VECTOR = 0x80;
IdtEntry idt[IDT_ENTRIES];
IdtPointer idt_pointer;
IrqHandler irq_handlers[IRQ_COUNT];

extern "C" void syscall_stub();

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
    // The syscall gate.  Its stub lives in user.asm rather than in the stub table:
    // it is not a vector 0..47 and there is exactly one of it.
    idt_set_gate(SYSCALL_VECTOR, reinterpret_cast<uint32>(&syscall_stub),
                 GDT_SELECTOR_CODE, GATE_INTERRUPT_32_USER);
    idt_pointer.limit = static_cast<uint16>(sizeof(idt) - 1);
    idt_pointer.base = reinterpret_cast<uint32>(&idt[0]);
    idt_load(&idt_pointer);
}

// The privilege level a gate allows, which the self-test reads back: "id_set_gate was
// called with 0xEE" and "ring 3 can actually use the gate" are different claims.
uint8 idt_gate_dpl(uint8 vector) {
    return static_cast<uint8>((idt[vector].flags >> 5) & 0x03);
}

bool idt_gate_present(uint8 vector) {
    return (idt[vector].flags & 0x80) != 0;
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
    // A page fault is the one exception the kernel can recover from.  The handler
    // allocates and maps the page, and returning from here lets `iret` retry the
    // instruction that faulted -- which is exactly what "demand zero" means.
    if (regs->vector == 14) {
        const uint32 address = read_cr2();
        if (paging_handle_fault(address, regs->error)) {
            return;
        }
        paging_panic_fault(regs, address, regs->error);
        return;
    }

    const char* name = "unknown";
    if (regs->vector < EXCEPTION_COUNT) {
        name = exception_names[regs->vector];
    }
    panic_registers(regs, name);
}

extern "C" void irq_dispatch(Registers* regs) {
    const uint8 irq = static_cast<uint8>(regs->vector - IRQ_BASE);
    // The end-of-interrupt goes out *before* the handler runs, and that order is not
    // a style choice.  A handler may switch tasks -- the timer's does, every
    // scheduler quantum -- and a switch leaves the handler suspended on the stack of
    // the task it interrupted, to be finished whenever that task next runs.  The
    // 8259 will not deliver another interrupt on a line that is still marked
    // in-service, so an EOI sent after the handler would be sent *tens of
    // milliseconds late*: the timer would tick once, the scheduler would switch, and
    // the clock would then stop until the interrupted task happened to be scheduled
    // again.  It looks exactly like a task that runs forever without being
    // preempted.
    //
    // Sending it first is safe because an interrupt gate has already cleared IF: the
    // handler cannot be re-entered by the same line, and a switch inside it runs the
    // next task with interrupts on and a clean in-service register.
    //
    // Always sent, even with no handler: a maskable interrupt that is never
    // acknowledged is never delivered again.
    pic_send_eoi(irq);
    if (irq < IRQ_COUNT && irq_handlers[irq] != nullptr) {
        irq_handlers[irq]();
    }
}

}  // namespace myos
