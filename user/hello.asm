; The first myos user program: write a line through the kernel and exit.
;
; It exists to prove the syscall ABI end to end, and it is written in assembly on
; purpose: if this works, the calling convention is what the kernel and a C++ program
; both think it is, rather than a convention two C++ compilations happen to agree on.
;
; The ABI (docs/abi.md): the call number in EAX, arguments in EBX, ECX, EDX, the
; result in EAX, and `int 0x80` to get in.
;
;   write(fd, buffer, count)   number 4
;   exit(code)                 number 1

[bits 32]

section .text

global user_entry
user_entry:
    ; write(1, message, message_end - message)
    mov eax, 4
    mov ebx, 1
    mov ecx, message
    mov edx, message_end - message
    int 0x80

    ; exit(0)
    mov eax, 1
    xor ebx, ebx
    int 0x80

    ; The kernel is supposed to have taken control back by now; if it has not, this
    ; is where the program would be, so make the state obvious rather than running
    ; into whatever follows.
    jmp $

section .rodata

message: db 'hello from ring 3', 10
message_end:
