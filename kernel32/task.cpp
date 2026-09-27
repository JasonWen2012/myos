// myos 32-bit kernel: tasks behind kernel32/task.h.

#include "task.h"

#include "console.h"
#include "cpu.h"
#include "heap.h"
#include "io.h"
#include "klog.h"
#include "libc.h"
#include "paging.h"
#include "panic.h"
#include "sched.h"
#include "tss.h"

namespace myos {
namespace {

Task g_tasks[TASK_MAX];
uint32 g_current = 0;               // slot index, not a pid
uint32 g_next_pid = 1;              // pid 0 is the boot context
uint32 g_created = 0;               // tasks that have ever been created
uint32 g_reaped = 0;
uint32 g_boot_stack_top = 0;

void copy_name(char* destination, const char* source) {
    uint32 index = 0;
    if (source != nullptr) {
        while (index < TASK_NAME_MAX && source[index] != '\0') {
            destination[index] = source[index];
            ++index;
        }
    }
    destination[index] = '\0';
}

Task* free_slot() {
    for (uint32 index = 0; index < TASK_MAX; ++index) {
        if (g_tasks[index].state == TaskState::Unused) {
            return &g_tasks[index];
        }
    }
    return nullptr;
}

uint32 slot_index_of(const Task* task) {
    for (uint32 index = 0; index < TASK_MAX; ++index) {
        if (&g_tasks[index] == task) {
            return index;
        }
    }
    return TASK_MAX;
}

}  // namespace

const char* task_state_name(TaskState state) {
    switch (state) {
        case TaskState::New:
            return "new";
        case TaskState::Ready:
            return "ready";
        case TaskState::Running:
            return "running";
        case TaskState::Zombie:
            return "zombie";
        default:
            return "unused";
    }
}

void task_init(uint32 boot_stack_top) {
    memset(g_tasks, 0, sizeof(g_tasks));
    g_boot_stack_top = boot_stack_top;
    g_current = 0;
    g_next_pid = 1;
    g_created = 0;
    g_reaped = 0;

    Task& boot = g_tasks[0];
    boot.pid = 0;
    boot.parent = TASK_NO_PARENT;
    boot.state = TaskState::Running;
    // The boot context keeps the directory the kernel built; it *is* the kernel.
    boot.directory = paging_active_directory();
    boot.stack_base = 0;                    // nothing was allocated for it
    boot.stack_bytes = 0;
    boot.stack_top = boot_stack_top;
    boot.esp = 0;                           // written the first time it is switched away
    boot.kernel_task = true;
    boot.entry = nullptr;
    copy_name(boot.name, "kmain");
}

Task* task_current() {
    if (g_current >= TASK_MAX) {
        return nullptr;
    }
    Task* task = &g_tasks[g_current];
    return task->state == TaskState::Unused ? nullptr : task;
}

uint32 task_pid() {
    const Task* task = task_current();
    return task != nullptr ? task->pid : TASK_NO_PARENT;
}

void task_set_current(Task* task) {
    const uint32 index = task == nullptr ? TASK_MAX : slot_index_of(task);
    if (index < TASK_MAX) {
        g_current = index;
    }
}

Task* task_by_pid(uint32 pid) {
    for (uint32 index = 0; index < TASK_MAX; ++index) {
        if (g_tasks[index].state != TaskState::Unused && g_tasks[index].pid == pid) {
            return &g_tasks[index];
        }
    }
    return nullptr;
}

Task* task_slot(uint32 index) {
    return index < TASK_MAX ? &g_tasks[index] : nullptr;
}

uint32 task_live_count() {
    uint32 live = 0;
    for (const Task& task : g_tasks) {
        if (task.state != TaskState::Unused) {
            ++live;
        }
    }
    return live;
}

uint32 task_created_count() {
    return g_created;
}

uint32 task_reaped_count() {
    return g_reaped;
}

// The first thing a brand new task runs.  It arrives here as the `ret` at the end of
// switch_to(), on the stack task_create_kernel() fabricated.
//
// The `sti` is belt and braces: the fabricated stack already carries IF=1 in its
// EFLAGS word, and switch_to() pops it into the CPU before the `ret` that lands here.
// It stays because the failure it guards against is invisible -- a task running with
// the timer masked is simply never preempted -- and because reading it next to the
// code that enters a task is worth more than the byte it costs.
extern "C" void task_trampoline() {
    interrupts_enable();
    Task* self = task_current();
    if (self != nullptr && self->entry != nullptr) {
        self->entry();
    }
    // An entry function that returns is not an error: it exits with 0, which is what
    // a `main` that falls off its end would mean.
    if (self != nullptr) {
        task_exit(0);
    }
    // No current task at all: nothing sensible to return to.
    for (;;) {
        hlt_forever();
    }
}

Task* task_create_kernel(const char* name, void (*entry)(), uint32 argument) {
    Task* task = free_slot();
    if (task == nullptr) {
        return nullptr;
    }
    void* stack = kmalloc(TASK_STACK_BYTES);
    if (stack == nullptr) {
        return nullptr;
    }
    const uint32 directory = paging_clone_directory();
    if (directory == 0) {
        kfree(stack);
        return nullptr;
    }

    const uint32 base = reinterpret_cast<uint32>(stack);
    const uint32 top = base + TASK_STACK_BYTES;
    memset(task, 0, sizeof(*task));
    task->pid = g_next_pid++;
    const Task* parent = task_current();
    task->parent = parent != nullptr ? parent->pid : TASK_NO_PARENT;
    // `New`, not `Ready`: everything below this line is still being written, and a
    // task the scheduler could pick right now would be picked with a stack pointer of
    // zero.  sched_add() is what makes it runnable, once all of this is true.
    task->state = TaskState::New;
    task->directory = directory;
    task->stack_base = base;
    task->stack_bytes = TASK_STACK_BYTES;
    task->stack_top = top;
    task->created_tick = static_cast<uint32>(sched_ticks());
    task->kernel_task = true;
    task->entry = entry;
    task->argument = argument;
    copy_name(task->name, name);
    // The canary sits at the very bottom of the stack, which is where a stack that
    // grows too deep arrives first.
    *reinterpret_cast<uint32*>(base) = TASK_STACK_MAGIC;

    // Fabricate the frame switch_to() expects, from the top down: the return address
    // it will `ret` into, the EFLAGS word it will `popfd`, then the four callee-saved
    // registers it will pop, in the reverse of the order it pushes them (ebp, ebx,
    // esi, edi -- so edi ends up lowest, which is stack order).
    //
    // The flags word carries IF.  A brand new task has no saved flags of its own, and
    // it is entered from whichever context switched to it -- which may be the timer's
    // interrupt gate, where IF is 0.  Without this bit the task would run with the
    // timer masked and could never be preempted: a scheduler that looks broken.
    uint32* sp = reinterpret_cast<uint32*>(top);
    *--sp = reinterpret_cast<uint32>(&task_trampoline);
    *--sp = EFLAGS_INTERRUPT_ENABLE;
    *--sp = 0;                                  // ebp
    *--sp = 0;                                  // ebx
    *--sp = 0;                                  // esi
    *--sp = 0;                                  // edi
    task->esp = reinterpret_cast<uint32>(sp);

    ++g_created;
    klog("task created");
    return task;
}

void task_exit(uint32 code) {
    Task* self = task_current();
    if (self == nullptr) {
        // Nothing to exit: the kernel is not running a task, so there is nowhere to
        // return to and no bookkeeping to do.
        hlt_forever();
        return;
    }
    self->state = TaskState::Zombie;
    self->exit_code = code;
    klog("task exited");
    // The scheduler never picks a zombie, so this loop cannot come back here after a
    // successful switch -- and if it does, nothing else was runnable at all, which is
    // a deadlock rather than a task to return to.  Reporting that is the whole point
    // of the loop: an unbounded `for(;;)` here would hang a session with no output,
    // which is the least useful shape a scheduler bug can take.
    for (;;) {
        schedule();
        if (task_current() == self) {
            panic("no runnable task: the scheduler had nothing to switch to");
        }
    }
}

bool task_join(uint32 pid, uint32* exit_code) {
    Task* child = task_by_pid(pid);
    if (child == nullptr || child->pid == 0) {
        return false;
    }
    // Bounded, like every other wait in this kernel: a child that loops forever must
    // make `join` report that it gave up, not wedge the caller.  Each round yields,
    // so the child (and everyone else) does get the CPU in between.
    constexpr uint32 SPIN_LIMIT = 200000;
    uint32 spins = 0;
    while (child->state != TaskState::Zombie) {
        if (++spins > SPIN_LIMIT) {
            return false;
        }
        sched_yield();
    }
    if (exit_code != nullptr) {
        *exit_code = child->exit_code;
    }
    // The exit code has been collected, so the slot is now only waiting to be freed.
    // (A real `wait` syscall will copy the code out to user memory first; the order
    // matters and is the same.)  Freeing itself is task_reap()'s job, because it also
    // has to happen for orphans and it must never happen in interrupt context.
    child->joined = true;
    return true;
}

uint32 task_reap() {
    uint32 reaped = 0;
    for (uint32 index = 0; index < TASK_MAX; ++index) {
        Task& task = g_tasks[index];
        if (task.state != TaskState::Zombie) {
            continue;
        }
        // Two reasons a zombie may go: its parent has collected the code, or it has
        // no parent left to collect it and its stack would otherwise leak for the
        // rest of the session.  A live parent keeps the slot so that it can still
        // ask how its child ended -- which is what task_join() does.
        bool orphan = true;
        if (task.parent != TASK_NO_PARENT) {
            const Task* parent = task_by_pid(task.parent);
            if (parent != nullptr && parent->state != TaskState::Zombie) {
                orphan = false;
            }
        }
        if (!task.joined && !orphan) {
            continue;
        }
        if (task.stack_base != 0) {
            kfree(reinterpret_cast<void*>(task.stack_base));
        }
        memset(&task, 0, sizeof(task));
        ++reaped;
        ++g_reaped;
    }
    return reaped;
}

void task_restore_kernel_stack() {
    Task* self = task_current();
    tss_set_kernel_stack(self != nullptr && self->stack_top != 0 ? self->stack_top
                                                                : g_boot_stack_top);
}

bool task_stack_intact(const Task* task) {
    if (task == nullptr || task->stack_base == 0) {
        return true;                    // nothing was allocated, nothing to corrupt
    }
    return *reinterpret_cast<const uint32*>(task->stack_base) == TASK_STACK_MAGIC;
}

void task_report() {
    uint32 live = 0;
    for (const Task& task : g_tasks) {
        if (task.state != TaskState::Unused) {
            ++live;
        }
    }
    kprintf("ps: %u task(s) in the table, %u created, %u reaped, %u switch(es)\n",
            live, g_created, g_reaped, sched_switches());
    console_puts("ps: pid state ticks switches parent name\n");
    for (const Task& task : g_tasks) {
        if (task.state == TaskState::Unused) {
            continue;
        }
        kprintf("ps: %u %s %u %u ", task.pid, task_state_name(task.state),
                task.ticks, task.switches);
        if (task.parent == TASK_NO_PARENT) {
            console_puts("none");
        } else {
            kprintf("%u", task.parent);
        }
        kprintf(" %s", task.name);
        if (task.state == TaskState::Zombie) {
            kprintf(" (exit %u)", task.exit_code);
        }
        if (!task_stack_intact(&task)) {
            console_puts(" STACK-CANARY-LOST");
        }
        console_putc('\n');
    }
}

}  // namespace myos
