// myos 32-bit kernel: port I/O.
//
// These are inline rather than calls into boot.asm on purpose: a driver that has
// to call a function to read a status byte reads worse, and the compiler emits the
// same single instruction either way.

#pragma once

#include "types.h"

namespace myos {

static inline void outb(uint16 port, uint8 value) {
    asm volatile("outb %0, %1" : : "a"(value), "Nd"(port));
}

static inline uint8 inb(uint16 port) {
    uint8 value;
    asm volatile("inb %1, %0" : "=a"(value) : "Nd"(port));
    return value;
}

static inline void outw(uint16 port, uint16 value) {
    asm volatile("outw %0, %1" : : "a"(value), "Nd"(port));
}

static inline uint16 inw(uint16 port) {
    uint16 value;
    asm volatile("inw %1, %0" : "=a"(value) : "Nd"(port));
    return value;
}

// Waste a little time on an I/O port that ignores writes.  The 8259 and the 8042
// need a moment between commands, and "a moment" here means "wait for the bus".
static inline void io_wait(void) {
    outb(0x80, 0);
}

static inline void interrupts_enable(void) {
    asm volatile("sti");
}

static inline void interrupts_disable(void) {
    asm volatile("cli");
}

}  // namespace myos
