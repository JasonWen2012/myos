; Every myos user program starts with this 16-byte header, exactly like a kernel image
; does.  The kernel reads it before it maps a single page: magic, architecture (3 for
; a ring-3 program), a checksum over the header itself, the entry offset and the image
; size.  A file without it is refused rather than executed.
;
; The entry offset, the size and the checksum are patched in by build.py after
; linking, because none of them is known until then.  It lives in its own section so
; that cofllink's user_layout() can place it first.

[bits 32]

section .myos_header
align 16
global user_header
user_header:
    db 'MYOS'                       ; 0..3 magic
    db 3                            ; 4   architecture: ring-3 user program
    db 1                            ; 5   version
    db 0                            ; 6   flags
    db 0                            ; 7   checksum, patched by build.py
    dd 0                            ; 8   entry offset, patched by build.py
    dd 0                            ; 12  image size, patched by build.py
