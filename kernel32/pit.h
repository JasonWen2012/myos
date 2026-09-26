// myos 32-bit kernel: the 8253/8254 programmable interval timer.

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 PIT_FREQUENCY = 1193182;   // the input clock, in Hz
constexpr uint32 PIT_TICK_HZ = 100;

// Programs channel 0 for PIT_TICK_HZ and registers the IRQ0 handler.  Does not
// enable interrupts; the caller decides when the machine may be interrupted.
void pit_init();

// Ticks counted so far.  This is the kernel's only notion of elapsed time, and
// the self-test's proof that interrupts actually arrive.
uint64 pit_ticks();
uint32 pit_ticks_per_second();

}  // namespace myos
