; A user program that asks the kernel to do something it must refuse.
;
; `write` takes a pointer, and a pointer is a number the program chose.  Handing it
; the kernel's own memory is the smallest interesting attack there is, and the answer
; has to be a refusal from the user-copy check -- not a byte of kernel memory printed
; on the console, and not a crash of the kernel.
;
; The exit code is the errno, made positive, so the shell and the tests can read the
; result without parsing prose: 37 is E_FAULT, which is what a refused pointer is
; supposed to return.  If the syscall had succeeded, the exit code would be the
; negative byte count instead, which is a number no test accepts.

[bits 32]

section .text

global user_entry
user_entry:
    mov eax, 4                      ; write
    mov ebx, 1                      ; stdout
    mov ecx, 0x00100000             ; the kernel image: not the user's memory
    mov edx, 8
    int 0x80                        ; EAX = -37 (E_FAULT) or the byte count

    mov ebx, eax
    neg ebx                         ; 37 for E_FAULT
    mov eax, 1                      ; exit
    int 0x80
    jmp $
