; myos 32-bit kernel: the context switch itself.
;
; Everything else about switching tasks -- who runs next, when, and what the TSS
; should point at -- is C++ in kernel32/sched.cpp.  This file is the part that cannot
; be written in C++ honestly: saving the callee-saved registers and the stack pointer
; of the running context and handing the CPU to another one.
;
; The contract, in both directions, is that of an ordinary function call, plus one
; word that an ordinary call does not carry:
;
;   switch_to(uint32* save_esp, uint32 next_esp)
;
;   - the caller's ebp/ebx/esi/edi, its EFLAGS and its return address stay on *its
;     own* stack;
;   - its stack pointer is written to *save_esp;
;   - esp becomes next_esp, whose stack is expected to hold the same six words in
;     the same order, so the pops and the `ret` below walk straight into the context
;     that was saved the last time that task was switched away from.
;
; EFLAGS rides along because the interrupt flag is part of a context.  schedule()
; disables interrupts around the switch -- a timer tick landing between "this task is
; no longer running" and "that task's stack is loaded" corrupts the task table in ways
; that show up far away -- and without saving EFLAGS a task parked inside schedule()
; would be resumed with interrupts still off and would then never be preempted again.
; Each context therefore carries its own IF: 0 for one parked inside the timer's
; interrupt gate (its `iret` turns interrupts back on), 1 for one parked in a
; voluntary yield.
;
; The stack of a *brand new* task is built by task_create_kernel() to match: six
; words, the top one being the address of task_trampoline (C++, in kernel32/task.cpp)
; and the EFLAGS word carrying IF=1.  That is why a task starts life as the return
; from a function call it never made.

[bits 32]

section .text

global switch_to

; void switch_to(uint32* save_esp, uint32 next_esp)
;
; The two arguments are at [esp+24] and [esp+28] after the five pushes: the call
; pushed a return address at [esp+20] when esp pointed at the saved edi.
switch_to:
    push ebp
    push ebx
    push esi
    push edi
    pushfd

    mov eax, [esp + 24]                     ; save_esp
    mov [eax], esp                          ; the running context is now parked

    mov esp, [esp + 28]                     ; next_esp

    popfd                                   ; the next context's interrupt flag
    pop edi
    pop esi
    pop ebx
    pop ebp
    ret
