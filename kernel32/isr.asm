; myos 32-bit kernel: interrupt entry stubs.
;
; One stub per vector, because the CPU tells a handler which vector fired only by
; *which* gate it went through.  Each stub pushes the vector number (and a dummy
; error code where the CPU does not push one, so the frame has one shape), then
; falls into a common path that saves the registers and calls into C++.
;
; The frame the C++ side sees, from the lowest address up, is exactly
; myos::Registers in kernel32/idt.h:
;
;   gs fs es ds | edi esi ebp esp ebx edx ecx eax | vector error | eip cs eflags
;
; `pushad` pushes EAX first and EDI last, so EDI ends up lowest; the error code and
; vector go on after that; and the last three words are the CPU's own frame.
;
; Everything here runs with interrupts already disabled: the IDT is built with
; interrupt gates (flags 0x8E), so no `cli` is needed and an exception cannot be
; interrupted by the timer halfway through reporting itself.

[bits 32]

extern isr_dispatch
extern irq_dispatch

section .text

; An exception that does not push an error code: the stub pushes a zero so that
; every frame has the same layout.
%macro ISR_NO_ERROR 1
global isr%1
isr%1:
    push dword 0
    push dword %1
    jmp isr_common
%endmacro

; An exception that does push one (8, 10..14, 17, 21, 29, 30): pushing another
; would shift the whole frame and the C++ side would read the wrong fields.
%macro ISR_WITH_ERROR 1
global isr%1
isr%1:
    push dword %1
    jmp isr_common
%endmacro

%macro IRQ_STUB 1
global irq%1
irq%1:
    push dword 0
    push dword (32 + %1)
    jmp irq_common
%endmacro

ISR_NO_ERROR 0
ISR_NO_ERROR 1
ISR_NO_ERROR 2
ISR_NO_ERROR 3
ISR_NO_ERROR 4
ISR_NO_ERROR 5
ISR_NO_ERROR 6
ISR_NO_ERROR 7
ISR_WITH_ERROR 8
ISR_NO_ERROR 9
ISR_WITH_ERROR 10
ISR_WITH_ERROR 11
ISR_WITH_ERROR 12
ISR_WITH_ERROR 13
ISR_WITH_ERROR 14
ISR_NO_ERROR 15
ISR_NO_ERROR 16
ISR_WITH_ERROR 17
ISR_NO_ERROR 18
ISR_NO_ERROR 19
ISR_NO_ERROR 20
ISR_WITH_ERROR 21
ISR_NO_ERROR 22
ISR_NO_ERROR 23
ISR_NO_ERROR 24
ISR_NO_ERROR 25
ISR_NO_ERROR 26
ISR_NO_ERROR 27
ISR_NO_ERROR 28
ISR_WITH_ERROR 29
ISR_WITH_ERROR 30
ISR_NO_ERROR 31

%assign irq_index 0
%rep 16
IRQ_STUB irq_index
%assign irq_index irq_index+1
%endrep

; Both common paths do the same three things -- save everything, hand the frame to
; C++ with flat segments in place, restore and return -- so the saving is one macro
; with the dispatcher name and the epilogue as parameters.
%macro SAVE_FRAME 0
    pushad
    push ds
    push es
    push fs
    push gs
    mov ax, 0x10                            ; the kernel's flat data selector
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
%endmacro

%macro RESTORE_FRAME 0
    pop gs
    pop fs
    pop es
    pop ds
    popad
    add esp, 8                              ; drop the vector and the error code
%endmacro

isr_common:
    SAVE_FRAME
    push esp                                ; Registers*
    call isr_dispatch
    add esp, 4
    RESTORE_FRAME
    iret                                    ; an exception handler that returns

irq_common:
    SAVE_FRAME
    push esp
    call irq_dispatch
    add esp, 4
    RESTORE_FRAME
    iret                                    ; the timer and the keyboard come back

; The loader needs one stub address per vector, in vector order.  A table in
; assembly keeps 48 extern declarations out of the C++ and makes the ordering
; impossible to get wrong by hand.
;
; Note the counter names.  They must not be called `irq` or `isr`: a preprocessor
; variable of that name shadows the *label* prefix in `dd irq %+ index`, and the
; token gets expanded to a number before `%+` ever concatenates.  The entries then
; hold raw section offsets with no relocation at all, which assembles, links and
; boots -- and jumps into low memory the first time an interrupt arrives.
section .rodata

global isr_stub_table
isr_stub_table:
%assign stub_index 0
%rep 32
    dd isr %+ stub_index
%assign stub_index stub_index+1
%endrep
%assign stub_index 0
%rep 16
    dd irq %+ stub_index
%assign stub_index stub_index+1
%endrep

section .text

; ------------------------------------------------------------------ cpu control

global gdt_load
global idt_load
global read_gdtr
global read_idtr

; read_gdtr(GdtPointer*) / read_idtr(IdtPointer*): what the CPU actually has
; loaded, so a test can prove that lgdt/lidt took effect rather than trusting them.
read_gdtr:
    mov eax, [esp + 4]
    sgdt [eax]
    ret

read_idtr:
    mov eax, [esp + 4]
    sidt [eax]
    ret

; gdt_load(const GdtPointer* pointer, uint16 code, uint16 data)
;
; Reloading CS cannot be done with a move: the only way to change it is a far
; transfer, so a far return is faked with the selector and the continuation address.
gdt_load:
    mov eax, [esp + 4]
    lgdt [eax]

    mov ax, [esp + 12]                      ; data selector
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
    mov ss, ax

    mov eax, [esp + 8]                      ; code selector
    push eax
    push .reloaded
    retf                                    ; pops offset then selector, reloading CS
.reloaded:
    ret

; idt_load(const IdtPointer* pointer)
idt_load:
    mov eax, [esp + 4]
    lidt [eax]
    ret
