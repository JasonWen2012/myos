; myos 32-bit kernel: entering ring 3, and coming back.
;
; Two directions, and only one of them is a normal call:
;
;   run_user_image   -- called by the kernel, builds an iret frame by hand and drops
;                       to privilege level 3.  It does not return; the kernel is
;                       re-entered later, in the middle of this function, by
;                       return_to_kernel.
;   return_to_kernel -- called from the syscall handler when the user program exits.
;                       It restores the kernel stack that run_user_image saved and
;                       returns to the instruction after that call, carrying the
;                       exit code in EAX.
;
; That pair is a hand-written setjmp/longjmp, and it is written in assembly on
; purpose: doing it from C++ means taking the address of a label and hoping the
; compiler kept the frame the way the source implies.  Here the only state is one
; saved ESP, in a variable that lives in this file.

[bits 32]

extern syscall_dispatch

section .data

; The kernel ESP to return to when the user program exits: written by
; run_user_image just before it leaves ring 0, read by return_to_kernel.
g_user_kernel_esp: dd 0

section .rodata

; The DPL-3 selectors.  GDT_SELECTOR_USER_CODE/DATA from gdt.h plus the requestor
; privilege level of 3; spelled out here because the assembler cannot share a C++
; constexpr.
USER_CODE_SELECTOR equ 0x18 | 3
USER_DATA_SELECTOR equ 0x20 | 3

section .text

; ------------------------------------------------------------------ the syscall

global syscall_stub
syscall_stub:
    push dword 0                            ; no error code on this vector
    push dword 0x80                         ; the vector number, so the frame matches
    pushad
    push gs
    push fs
    push es
    push ds

    ; The kernel's data selector while kernel code runs.  The user's would work by
    ; accident -- both descriptors are flat -- but "works by accident" is not what a
    ; privilege boundary should rest on.
    mov ax, 0x10
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax

    push esp
    call syscall_dispatch
    add esp, 4

    pop ds
    pop es
    pop fs
    pop gs
    popad
    add esp, 8                              ; the vector and the dummy error code
    iret

; ------------------------------------------------------- ring 3 and back again

global run_user_image
global return_to_kernel

; uint32 run_user_image(uint32 entry, uint32 user_stack_top)
;
; Saves the kernel's stack *and its callee-saved registers*, then `iret`s into ring 3
; with the user's stack and the interrupt flag on.  The saved stack is where
; return_to_kernel resumes, so the two functions have to agree on it -- they do,
; through g_user_kernel_esp.
;
; The registers matter as much as the stack: `exec_user` is a C++ function that had
; live values in EBX/ESI/EDI/EBP when it called this, and the path back arrives from
; ring 3 with those registers holding whatever the user program left there.  Without
; the save and restore, the compiler's locals come back as user data.
run_user_image:
    push ebx
    push esi
    push edi
    push ebp
    mov eax, [esp + 20]                     ; entry point (4 saved registers + 4)
    mov ecx, [esp + 24]                     ; user stack top
    mov [g_user_kernel_esp], esp

    ; The data segments have to be the user's before ring 3 starts: a program that
    ; begins with the kernel's DPL-0 selector still in DS faults on its first global
    ; access, and it cannot load that selector itself at privilege level 3.
    mov dx, USER_DATA_SELECTOR
    mov ds, dx
    mov es, dx
    mov fs, dx
    mov gs, dx

    push edx                                ; ss (the same selector)
    push ecx                                ; esp
    pushfd                                  ; eflags
    or dword [esp], 0x200                   ; interrupts on in user mode, as they
                                            ; are in the kernel by this point
    mov dx, USER_CODE_SELECTOR
    push edx                                ; cs
    push eax                                ; eip
    iret                                    ; privilege level 3, here we come

; void return_to_kernel(uint32 exit_code)
;
; Never returns to its caller: it returns to whatever run_user_image was called
; from, with the exit code in EAX.  Interrupts are left disabled on the way out --
; the shell is about to print a line and the timer can wait.
return_to_kernel:
    mov eax, [esp + 4]                      ; the exit code
    cli
    mov esp, [g_user_kernel_esp]
    pop ebp
    pop edi
    pop esi
    pop ebx
    ret
