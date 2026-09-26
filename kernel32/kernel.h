// myos 32-bit kernel: the kernel's own self-test, callable from the shell.

#pragma once

#include "types.h"

namespace myos {

// Runs every in-guest check, printing one line per check, and returns how many
// failed.  Called at boot for the log and by the `selftest` command, which turns
// the result into an exit status so a headless run has a verdict instead of a
// timeout.
uint32 kernel_self_test();
uint32 kernel_checks_run();

}  // namespace myos
