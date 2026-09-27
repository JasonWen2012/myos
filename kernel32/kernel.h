// myos 32-bit kernel: the kernel's own self-test, callable from the shell.

#pragma once

#include "types.h"

namespace myos {

// Runs every in-guest check, printing one line per check, and returns how many
// failed.  Called at boot for the log and by the `check` and `selftest` commands,
// which turn the result into a verdict.
uint32 kernel_self_test();
uint32 kernel_checks_run();
uint32 kernel_checks_skipped();

// Runs the scheduler checks on their own and returns how many failed, so that
// `schedtest` can create, preempt and reap tasks in a session that is already up
// without running the other sixty-odd checks around it.
uint32 kernel_scheduler_test();

}  // namespace myos
