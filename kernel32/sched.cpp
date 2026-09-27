// myos 32-bit kernel: the scheduler behind kernel32/sched.h.

#include "sched.h"

#include "console.h"
#include "io.h"
#include "klog.h"
#include "paging.h"
#include "task.h"
#include "tss.h"

// kernel32/switch.asm.  In C++ terms this is a function call that may or may not
// return on the stack it was called on: the switch saves the caller's callee-saved
// registers and stack pointer into *save_esp and resumes whoever next_esp belongs to.
extern "C" void switch_to(myos::uint32* save_esp, myos::uint32 next_esp);

namespace myos {
namespace {

uint32 g_cursor = 0;            // slot of the task the round robin last picked
uint64 g_ticks = 0;             // ticks charged out to tasks
uint32 g_switches = 0;
uint32 g_paused = 0;

uint32 slot_index(const Task* task) {
    for (uint32 index = 0; index < TASK_MAX; ++index) {
        if (task_slot(index) == task) {
            return index;
        }
    }
    return TASK_MAX;
}

// Round robin: the next Ready slot after the one that ran last.  Scanning the table
// instead of keeping a queue means there is no queue to get out of step with the
// table, and TASK_MAX is sixteen.
Task* pick_next(Task* current) {
    for (uint32 step = 1; step <= TASK_MAX; ++step) {
        Task* candidate = task_slot((g_cursor + step) % TASK_MAX);
        if (candidate != nullptr && candidate->state == TaskState::Ready) {
            return candidate;
        }
    }
    // Nobody else is runnable.  The current task keeps the CPU if it still has a
    // claim to it; a zombie has none, which is how task_exit() ends up switching away
    // for good instead of returning to a stack it no longer owns.
    if (current != nullptr && current->state == TaskState::Running) {
        return current;
    }
    return nullptr;
}

void sched_switch_to(Task* next) {
    Task* current = task_current();
    if (next == nullptr) {
        return;
    }
    // Interrupts are off for everything below, and that is not a formality.  The
    // state machine here has several steps that must not be observed half-done: a
    // timer tick landing between "the old task is no longer running" and "the new
    // task's stack is loaded" would run the tick handler against a task table that
    // says task X is current while the CPU is still in task Y, and the handler is
    // entitled to switch again.  That corruption surfaces as two tasks that believe
    // they are running, or as a task resuming somebody else's stack -- both of which
    // this kernel has now seen once.
    //
    // Whoever resumes this context restores its own interrupt flag: from switch_to's
    // saved EFLAGS for a task parked in a yield, and from the interrupt frame's `iret`
    // for a task parked inside the timer's handler.
    interrupts_disable();

    if (current != nullptr && current->state == TaskState::Running) {
        current->state = TaskState::Ready;
    }
    next->state = TaskState::Running;
    ++next->switches;
    ++g_switches;
    g_cursor = slot_index(next);

    // Two pieces of CPU state belong to a task and have to be in place before its
    // stack is: esp0, because the first interrupt it takes from ring 3 must land on
    // *its* kernel stack, and CR3, because the address space it will execute in is
    // the one it last saw.  Neither can wait until after switch_to(), which is
    // already running in the next task by the time it returns.
    tss_set_kernel_stack(next->stack_top);
    paging_switch_directory(next->directory);

    // And the task layer has to be told, before the stack changes: the very first
    // thing a new task runs is task_trampoline(), which asks task_current() whose
    // entry point to call.  Leaving this out made every new task run the *previous*
    // task's entry (null, for task 0) and then exit as the wrong task -- a hang with
    // no output, which is the least helpful shape a scheduler bug can take.
    task_set_current(next);

    uint32 parked = 0;
    uint32* save = current != nullptr ? &current->esp : &parked;
    switch_to(save, next->esp);
    // Arriving here means some later switch picked this task again: `current` may be
    // a different task than it was, so nothing may be cached across the call.  The
    // caller (schedule()) restores this context's interrupt flag.
}

}  // namespace

void sched_init() {
    g_cursor = 0;
    g_ticks = 0;
    g_switches = 0;
    g_paused = 0;
}

void sched_add(Task* task) {
    if (task == nullptr) {
        return;
    }
    // The task's stack and address space were finished by task_create_kernel(); this
    // is the moment it may be picked.  Nothing else in the kernel turns a `New` task
    // into a runnable one, so there is exactly one place where "it is ready" is
    // decided -- and it is not a place the timer can interrupt halfway.
    //
    // Deliberately not logged: the kernel's log ring holds 32 lines, and a task
    // lifecycle is two events (created, exited) that both matter, so "ready" would
    // push the interesting ones out twice as fast.
    task->state = TaskState::Ready;
}

void schedule() {
    if (g_paused > 0) {
        return;
    }
    Task* current = task_current();
    Task* next = pick_next(current);
    if (next == nullptr || next == current) {
        return;
    }
    // This context's interrupt flag, captured *before* sched_switch_to() turns
    // interrupts off, and put back when this task is next resumed.  Without the
    // restore, a task that yielded with interrupts on would come back with them off
    // and would never be preempted again -- a bug that would look like "one task
    // hogs the CPU after its first yield".
    const uint32 flags = read_eflags();
    sched_switch_to(next);
    write_eflags(flags);
}

void sched_yield() {
    schedule();
}

void sched_tick() {
    ++g_ticks;
    Task* current = task_current();
    if (current != nullptr) {
        ++current->ticks;
    }
    if (g_paused > 0) {
        return;
    }
    if ((g_ticks % SCHED_QUANTUM_TICKS) != 0) {
        return;
    }
    schedule();
}

uint64 sched_ticks() {
    return g_ticks;
}

uint32 sched_switches() {
    return g_switches;
}

uint32 sched_current_pid() {
    return task_pid();
}

void sched_pause() {
    ++g_paused;
}

void sched_resume() {
    if (g_paused > 0) {
        --g_paused;
    }
}

bool sched_paused() {
    return g_paused > 0;
}

void sched_report() {
    Task* current = task_current();
    kprintf("sched: %u switch(es), %u tick(s) charged, quantum %u tick(s)\n",
            g_switches, static_cast<uint32>(g_ticks), SCHED_QUANTUM_TICKS);
    kprintf("sched: current pid %u (%s), preemption %s\n",
            current != nullptr ? current->pid : TASK_NO_PARENT,
            current != nullptr ? current->name : "none",
            g_paused > 0 ? "paused" : "on");
}

}  // namespace myos
