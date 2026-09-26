// myos 32-bit kernel: the 8259 programmable interrupt controller pair.

#pragma once

#include "types.h"

namespace myos {

constexpr uint8 PIC_IRQ_TIMER = 0;
constexpr uint8 PIC_IRQ_KEYBOARD = 1;
constexpr uint8 PIC_IRQ_SERIAL = 4;         // COM1, when the UART is wired to it

// Remaps the pair to vectors IRQ_BASE..IRQ_BASE+15 and unmasks the three sources
// this kernel handles: the timer, the keyboard and COM1.  Returns nothing and
// reports nothing -- a PIC that did not take the commands shows up as interrupts
// that never arrive, which is why the self-test counts timer ticks.
void pic_init();
void pic_send_eoi(uint8 irq);
void pic_set_mask(uint8 irq, bool masked);

}  // namespace myos
