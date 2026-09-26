; myos 32-bit kernel: the assembly the C++ cannot express.
;
; Everything here is called from C++ with extern "C" linkage.  The file is
; assembled by nasm with -f win32, so the symbol names carry the leading
; underscore this toolchain uses and cofllink indexes both spellings.
;
; What lives here, and why:
;   * the image header, which has to sit at offset 0 of the image because that is
;     where the loader reads it from;
;   * the entry point, so that the stack is switched once, in assembly, before any
;     C++ runs;
;   * halting, which has to stop the CPU rather than spin in a C loop;
;   * the debug-exit write a self-test uses to end a headless run with a status.

[bits 32]

; The 16-byte header every myos kernel image starts with.  The entry offset, the
; size and the checksum cannot be written here -- two of them are only known after
; linking -- so build.py fills them in.  Its own section, placed first by
; cofllink.kernel_layout(), is what puts it at offset 0.
section .myos_header
align 16
global image_header
image_header:
    db 'MYOS'                       ; 0..3 magic
    db 2                            ; 4   architecture: 32-bit protected mode
    db 1                            ; 5   version
    db 0                            ; 6   flags
    db 0                            ; 7   checksum, patched by build.py
    dd 0                            ; 8   entry offset, patched by build.py
    dd 0                            ; 12  image size, patched by build.py

section .text

global kernel_entry
global hlt_forever
global debug_exit

; Defined in kernel32/kernel.cpp.  Declared extern because the entry stub is what
; *calls* it: the header's entry offset points here, not at the C++ function.
extern kmain

; kernel_entry: where the loader jumps, and the image's recorded entry point.
;
; The stack switch happens here, in assembly, and nowhere else.  Doing it from
; C++ (a function that sets ESP and then jumps back to its caller) looks neat and
; is a trap: the compiler's frame is abandoned mid-function, and gcc is entitled
; to keep live values in that frame or in callee-saved registers.  The symptom was
; a kernel that printed a lost address bias, misread structure fields, reported
; shifted string pointers and corrupted its own static counters -- and only in some
; builds, because whether gcc kept something important across the call depended on
; the code around it.
;
; The loader leaves ESP above the image (KERNEL32_STACK_LIN); this gives the kernel
; its own stack inside its own .bss before a single C++ instruction runs.
kernel_entry:
    mov esp, kernel_stack_top
    xor ebp, ebp                    ; no frame to walk: nothing called us in C++
    call kmain
    jmp hlt_forever                 ; kmain never returns; this is the backstop

; hlt_forever(): stop this CPU for good.  Interrupts are masked first: `hlt` with
; interrupts enabled would wake on the next tick and fall through into whatever
; follows.
hlt_forever:
    cli
.halt:
    hlt
    jmp .halt

; debug_exit(uint32 value): stop an emulator run with a status.
;
; QEMU's isa-debug-exit device turns a write to port 0xF4 into the process exit
; status (value << 1) | 1, which is how a self-test reports success or failure to
; whatever started the machine.  On real hardware the port is simply unused, so
; the write is harmless there.
debug_exit:
    mov al, [esp + 4]
    mov dx, 0xF4
    out dx, al
    jmp hlt_forever

; The kernel's stack, in its own section so that cofllink can place it *after*
; every other .bss object.  That order matters: the stack grows down from its top,
; so anything placed above the top is safe and anything placed inside the stack's
; range gets overwritten by ordinary function calls.  With the stack last, its top
; is the end of the image and the kernel's static variables sit below its bottom,
; which only a stack overflow can reach.
section .bss.stack

global kernel_stack_top
align 16
kernel_stack_bottom:
    resb 16 * 1024
kernel_stack_top:
