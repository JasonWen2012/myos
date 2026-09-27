// myos 32-bit kernel: tasks.
//
// A task is a stack, an address space, and the bookkeeping that says whether it is
// allowed to run.  Phase 2a has kernel tasks only: they share the kernel's address
// space (each has a private *page directory* whose kernel entries point at the same
// page tables), they are preempted by the timer, and they end by calling task_exit().
// User processes -- fork, copy-on-write, a task per ring-3 program -- are the next
// milestone; nothing here has to be undone for them, because the one thing they need
// that this does not have is a user half of the address space.
//
// Two decisions are worth knowing before reading the code:
//
//   * `Task::esp` is where switch_to() parks the stack pointer.  A task that has
//     never run does not have one yet, so task_create_kernel() *fabricates* a stack
//     that makes the switch into it behave like the return from a call to
//     task_trampoline().  A new task is therefore a context, not a special case in
//     the scheduler.
//   * Task 0 is the boot context (kmain, and later the shell).  It has no allocated
//     stack -- it is already running on the kernel's stack -- and it keeps the page
//     directory the kernel built rather than a copy, so the "the CPU is using the
//     directory the kernel built" check keeps meaning what it says.

#pragma once

#include "types.h"

namespace myos {

// Slots, not a growing list.  Sixteen is comfortably more than the demos use and
// keeps `ps` one screen; the array is the scheduler's whole data structure.
constexpr uint32 TASK_MAX = 16;
constexpr uint32 TASK_NAME_MAX = 12;            // stored, so 12 characters plus NUL
// Per-task kernel stack.  A task's whole life -- its entry function, its printf
// frames, and any interrupt it takes while running -- happens on these 8 KiB, so this
// is the number that decides how deep a kernel task may nest.  The canary below is
// what turns "too deep" from silent corruption into a failed check.
constexpr uint32 TASK_STACK_BYTES = 8 * 1024;
constexpr uint32 TASK_STACK_MAGIC = 0x4B534154u;    // "TASK", written at the bottom
constexpr uint32 TASK_NO_PARENT = 0xFFFFFFFFu;
// Bit 1 is always set on this CPU and bit 9 is IF.  A new task's fabricated stack
// carries this as its EFLAGS word (see switch.asm), which is what lets the first
// timer tick preempt it.
constexpr uint32 EFLAGS_INTERRUPT_ENABLE = 0x202u;

enum class TaskState : uint32 {
    Unused = 0,
    New,                // created, but its stack is not finished: not runnable yet
    Ready,              // runnable, waiting for the scheduler to pick it
    Running,            // the task the CPU is in right now
    Zombie,             // exited; its slot is kept until its parent sees the exit
};

const char* task_state_name(TaskState state);

struct Task {
    uint32 esp;                 // switch_to() saves/restores through this
    uint32 pid;
    uint32 parent;
    TaskState state;
    uint32 directory;           // the CR3 this task runs with
    uint32 stack_base;          // kmalloc'd kernel stack, low address (0 for task 0)
    uint32 stack_bytes;
    uint32 stack_top;           // what the TSS esp0 gets while this task runs
    uint32 ticks;               // timer ticks charged to this task
    uint32 switches;            // how many times the scheduler switched *into* it
    uint32 exit_code;
    uint32 created_tick;
    uint32 argument;            // handed to the entry function through the task
    bool kernel_task;           // false does not exist yet; ring-3 tasks are 2b
    bool joined;                // some task has already collected the exit code
    char name[TASK_NAME_MAX + 1];
    void (*entry)();            // what task_trampoline() calls for a new task
};

// Creates task 0 from the running context.  Call after heap_init() and after the TSS
// exists, because task 0's stack top is the kernel stack the TSS already names.
void task_init(uint32 boot_stack_top);

Task* task_current();
// The scheduler's one write into this file: which slot the CPU is in.  The table and
// the "current" index live together because they are one fact -- and because the bug
// that makes this necessary is subtle: a task switched into whose slot the task layer
// had not been told about would run, read task_current(), and find *the task it
// replaced* -- whose entry point is not even its own.
void task_set_current(Task* task);
uint32 task_pid();
Task* task_by_pid(uint32 pid);
// Slot access for `ps` and the self-test: index in 0..TASK_MAX-1, null when unused.
Task* task_slot(uint32 index);
uint32 task_live_count();       // slots in use, zombies included
uint32 task_created_count();    // tasks created since boot, reaped ones included
uint32 task_reaped_count();

// A new kernel task, in the `New` state: its stack is built and its pid is assigned,
// but it is not runnable until sched_add() is called.  That split is not decoration.
// A task becomes runnable the moment its state says so, and the timer does not wait
// for the code that is still filling the structure in: publishing `Ready` before
// `esp` had been set let a tick switch to a task whose stack pointer was still zero,
// which is a general protection fault somewhere far from the cause.  Create it, then
// hand it to the scheduler, and it starts from a stack that is finished.
//
// Returns null when the table is full or the stack cannot be allocated; the scheduler
// is not told about a task that never came to exist.  `argument` is passed to the
// entry function through the task, so one entry function can serve several tasks.
Task* task_create_kernel(const char* name, void (*entry)(), uint32 argument);

// Ends the current task: marks it a zombie, then switches away and never returns.
// A task whose entry function returns gets here too, through task_trampoline.
void task_exit(uint32 code);

// Waits for `pid` to exit and collects its code.  Returns false when there is no such
// task (or when a child that never exits used up the bounded wait).  This is the
// primitive phase 2b's `wait` syscall will call; today it waits by yielding, because
// there is no sleep queue yet.
//
// It does *not* free the task: the slot survives with the exit code in it, so a
// caller can still read how the task ended.  task_reap() is what gives the stack
// back.
bool task_join(uint32 pid, uint32* exit_code);

// Frees the stacks of zombies whose exit code has been collected, or whose parent is
// gone and so will never collect it.  Returns how many slots were freed.
//
// Task context only -- never from an interrupt.  Reaping walks the heap's free list,
// and a timer tick can land in the middle of somebody else's kmalloc; a handler that
// did heap work of its own could then re-enter a half-updated free list.  The
// scheduler deliberately does not reap, for exactly this reason.
uint32 task_reap();

// Puts esp0 back on the current task's stack.  exec_user() points the TSS at a
// temporary stack while a user program runs; when that returns, esp0 has to name the
// *task's* stack again, not the boot stack it named before tasks existed.
void task_restore_kernel_stack();

// The canary at the bottom of a task's stack, and whether it is still there.
bool task_stack_intact(const Task* task);

void task_report();

}  // namespace myos
