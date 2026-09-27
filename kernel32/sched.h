// myos 32-bit kernel: the scheduler.
//
// Round robin over the task table, preempted by the timer and offered voluntarily by
// `sched_yield()`.  That is the whole policy, and the point of phase 2a is that it is
// the *whole* mechanism: there is no sleep queue, no priority, and no idle task yet,
// because a policy that is not there cannot be subtly wrong.
//
// Three things are worth knowing:
//
//   * `schedule()` is not reentrant and runs with interrupts disabled in the one place
//     it matters (the timer's interrupt gate cleared IF before the handler ran).  A
//     switch from ordinary kernel code happens with the caller's own IF, and the task
//     switched to inherits the CPU's flags, so a context parked inside the timer
//     handler resumes and finishes with the `iret` that restores its flags.  This is
//     what makes "switch in interrupt context" work without a separate mechanism.
//   * A brand new task starts in task_trampoline(), which enables interrupts itself.
//     See the comment there: `ret` does not restore EFLAGS, so a new task would
//     otherwise inherit IF=0 from the timer tick that started it.
//   * sched_pause()/sched_resume() exist for exactly one caller today: exec_user(),
//     which runs a ring-3 program synchronously on a temporary kernel stack.  Until
//     user processes are tasks of their own (phase 2b), preempting that path would
//     mean switching away from a context whose stack the TSS no longer names.  Ticks
//     are still counted while scheduling is paused; nothing else changes.

#pragma once

#include "types.h"

namespace myos {

// How long a task may run before the timer takes the CPU away.  Two ticks at 100 Hz
// is 20 ms: long enough that the switch itself is not the workload, short enough that
// the demos finish while a person is still watching.
constexpr uint32 SCHED_QUANTUM_TICKS = 2;

void sched_init();

// Adds a task created by task_create_kernel().  It becomes runnable immediately: the
// next tick, or the next yield, may pick it.
void sched_add(struct Task* task);

// Picks the next runnable task and switches to it.  Returns without switching when
// there is nothing else to run, so the caller keeps the CPU rather than the kernel
// dropping into nothing.
void schedule();

// The voluntary form, for a task that is waiting for something.  Identical to
// schedule() today; it exists as a name so that "waiting" and "being preempted" do
// not have to be spelled the same way at every call site.
void sched_yield();

// Called from the timer interrupt: charges the tick to the running task and, every
// SCHED_QUANTUM_TICKS, switches.  Never blocks and never allocates -- it runs in
// interrupt context, where the only allowed work is bookkeeping.
void sched_tick();

uint64 sched_ticks();           // ticks the scheduler has charged out
uint32 sched_switches();        // context switches performed
uint32 sched_current_pid();

// Suspends preemption for the current task.  Nests: each pause needs a matching
// resume, because the two callers that will exist in phase 2b (a syscall that must
// not be interrupted, and exec_user) are not mutually exclusive.
void sched_pause();
void sched_resume();
bool sched_paused();

void sched_report();

}  // namespace myos
