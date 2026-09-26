// myos 32-bit kernel: the 8259 programmable interrupt controller pair.

#include "pic.h"

#include "idt.h"
#include "io.h"
#include "types.h"

namespace myos {
namespace {

constexpr uint16 PIC1_COMMAND = 0x20;
constexpr uint16 PIC1_DATA = 0x21;
constexpr uint16 PIC2_COMMAND = 0xA0;
constexpr uint16 PIC2_DATA = 0xA1;
constexpr uint8 PIC_EOI = 0x20;

// The ICW sequence is the documented one, and the io_wait() calls are not
// decoration: the 8259 needs a moment between commands on an ISA bus, and a
// missing wait shows up as a PIC that ignores the rest of the initialisation.
void remap(uint8 offset1, uint8 offset2) {
    const uint8 mask1 = inb(PIC1_DATA);
    const uint8 mask2 = inb(PIC2_DATA);

    outb(PIC1_COMMAND, 0x11);               // ICW1: initialise, expect ICW4
    io_wait();
    outb(PIC2_COMMAND, 0x11);
    io_wait();
    outb(PIC1_DATA, offset1);               // ICW2: vector offset
    io_wait();
    outb(PIC2_DATA, offset2);
    io_wait();
    outb(PIC1_DATA, 0x04);                  // ICW3: slave on IRQ2
    io_wait();
    outb(PIC2_DATA, 0x02);                  // ICW3: cascade identity
    io_wait();
    outb(PIC1_DATA, 0x01);                  // ICW4: 8086 mode
    io_wait();
    outb(PIC2_DATA, 0x01);
    io_wait();

    outb(PIC1_DATA, mask1);                 // restore what was there, then let
    outb(PIC2_DATA, mask2);                 // pic_init decide what to unmask
}

}  // namespace

void pic_init() {
    remap(IRQ_BASE, static_cast<uint8>(IRQ_BASE + 8));

    // Everything masked, then exactly the three sources this kernel drives.  The
    // slave is masked entirely: nothing on IRQ8..15 is wired up here, and an
    // unclaimed interrupt would be acknowledged with no handler behind it.
    outb(PIC1_DATA, 0xFF);
    outb(PIC2_DATA, 0xFF);
    pic_set_mask(PIC_IRQ_TIMER, false);
    pic_set_mask(PIC_IRQ_KEYBOARD, false);
    pic_set_mask(PIC_IRQ_SERIAL, false);
}

void pic_set_mask(uint8 irq, bool masked) {
    const uint16 port = irq < 8 ? PIC1_DATA : PIC2_DATA;
    const uint8 line = static_cast<uint8>(irq < 8 ? irq : irq - 8);
    uint8 value = inb(port);
    if (masked) {
        value = static_cast<uint8>(value | (1 << line));
    } else {
        value = static_cast<uint8>(value & ~(1 << line));
    }
    outb(port, value);
}

void pic_send_eoi(uint8 irq) {
    if (irq >= 8) {
        outb(PIC2_COMMAND, PIC_EOI);        // the slave first, then the master
    }
    outb(PIC1_COMMAND, PIC_EOI);
}

}  // namespace myos
