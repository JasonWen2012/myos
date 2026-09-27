// myos 32-bit kernel: stopping with a reason.
//
// Everything that used to end in "print a line and halt" goes through here, for one
// reason: a report written before the halt is evidence, and a triple fault is the
// absence of it.  The failing status also gives a headless run a verdict instead of
// a timeout, so a test can tell "the kernel said what went wrong" from "the kernel
// disappeared".

#pragma once

#include "idt.h"
#include "types.h"

namespace myos {

// Prints the reason and stops the CPU.  Never returns.
void panic(const char* reason);

// The same, with the register frame an exception arrived in.
void panic_registers(const Registers* regs, const char* reason);

}  // namespace myos
