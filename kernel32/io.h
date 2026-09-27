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

// The interrupt flag as part of a saved context.  The scheduler needs both halves:
// it reads EFLAGS before it disables interrupts and writes the same value back when
// the task is resumed, so that a task parked in a voluntary yield comes back with
// interrupts on and one parked inside an interrupt handler comes back with them off
// (its own `iret` restores them from the frame).  Saving the flag inside switch_to
// instead would be too late: by then `cli` has already cleared it.
static inline uint32 read_eflags(void) {
    uint32 value;
    asm volatile("pushfl; popl %0" : "=r"(value));
    return value;
}

static inline void write_eflags(uint32 value) {
    asm volatile("pushl %0; popfl" : : "r"(value) : "memory");
}

}  // namespace myos
