; myos 16-bit kernel: master assembly unit.
;
; Why everything is assembled as one unit
; ---------------------------------------
; The COFF object format this toolchain can produce carries only 32-bit
; relocations.  Every 16-bit relocation is rejected by nasm outright ("COFF
; format does not support 16-bit relocations") and the in-tree linker has no way
; to express one either.  That rules out `call external_function`, `mov si,
; string`, `mov ax, [symbol]` and `dw symbol - other_symbol` across sections.
;
; The kernel is therefore a single translation unit organised so that no
; relocation is needed at all:
;
;   * one .text section holds all code, one .data section holds every byte of
;     data, including the image header itself.  References within a section are
;     displacements the assembler resolves on its own;
;   * the header lives at offset 0 of .data, and the linker places .data first,
;     so the loader finds the header exactly where it expects it;
;   * state is reached through ESI as a base pointer and strings through EDI, so
;     no memory operand is ever an absolute 16-bit address.
;
; Module separation is kept as %include files, and each module prefixes its
; labels.  The includes must come before .data because they define the CODE_SIZE
; style constants that the data block uses.

[bits 16]

%include "boot.inc"
%include "kernel.inc"

; Console state block offsets.  A cell word is (attribute << 8) | character, and
; a video cell is two bytes, so these are the dd fields console.asm reaches as
; [esi + OFF_...].
%define OFF_ATTRIBUTE     0
%define OFF_CURSOR_ROW    4
%define OFF_CURSOR_COL    8
%define CONSOLE_DATA_SIZE 12

; ---------------------------------------------------------------- constants
%define KERNEL_STACK_TOP 0xA000       ; above the image, below the video buffer
%define KEYBOARD_VECTOR  0x09         ; the IVT entry the kernel takes over
%define KBD_DATA_PORT    0x60         ; 8042 output buffer (scancodes)
%define PIC_COMMAND_PORT 0x20         ; 8259 command register
%define PIC_EOI          0x20         ; end-of-interrupt
%define KBD_FLAGS_LIN    0x417        ; BIOS keyboard flags, 0040:0017
%define BDA_MEMORY_KB    0x413        ; BIOS memory size in KiB
%define BDA_TICKS        0x46C        ; BIOS tick counter, 18.2 Hz

%define LINE_BUFFER_SIZE  80
%define TOKEN_BUFFER_SIZE 16
%define KEY_BUFFER_SIZE   64
%define COMMAND_ENTRY_SIZE   10       ; 8-byte name + handler pointer
%define COMMAND_HANDLER_OFFSET 8

; Keyboard state block (see keyboard.asm).  Offsets, because every field is
; reached as [reg + KEY_...] -- a 16-bit absolute operand would need a
; relocation COFF cannot express.
%define KEY_BIOS_OFFSET   0
%define KEY_BIOS_SEGMENT  2
%define KEY_WRITE         4
%define KEY_COUNT         6
%define KEY_BUFFER        8

; Shell state block (see shell.asm).
%define SHELL_LINE_LEN    0
%define SHELL_ARGS        4           ; dd: pointer into the line buffer
%define SHELL_LINE        8
%define SHELL_TOKEN       (SHELL_LINE + LINE_BUFFER_SIZE)

; ---------------------------------------------------------------- kernel code

section .text

global kernel_main

kernel_main:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, KERNEL_STACK_TOP
    cld

    call console_init

    mov edi, msg_banner
    mov al, ATTR_BANNER
    call console_puts_colored

    call keyboard_init

    mov edi, msg_version
    call console_puts

    mov edi, msg_ready
    mov al, ATTR_OK
    call console_puts_colored

    ; Reported straight out of the image header, so the console output is itself
    ; evidence that the header the loader validated is the one now running.
    mov edi, msg_probe
    call console_puts
    lea_off eax, image_header
    mov bx, [eax + IMG_OFF_SIZE]
    mov ax, bx
    call console_put_dec
    mov edi, msg_bytes
    call console_puts
    mov ax, sp
    call console_put_hex16
    mov edi, msg_newline
    call console_puts

    ; Interrupts stay DISABLED for the whole life of this kernel, and that is a
    ; decision, not an oversight.  The kernel installs no interrupt vector of its
    ; own -- INT 09h deliberately stays the firmware's (see keyboard.asm), and
    ; nothing else is hooked -- so IF=1 would let the first hardware interrupt
    ; jump through an IVT entry that is still 0:0, i.e. straight into the
    ; interrupt table and the BIOS data area.  That is exactly what happened while
    ; bringing this up: a keystroke raised IRQ1 and the kernel ran off into low
    ; memory.  With interrupts off, the firmware still latches every scancode and
    ; INT 16h still returns it, which is all the shell needs.
    ;
    ; Putting this back is a later step, and it goes together with installing real
    ; handlers: an IVT the kernel owns, an IRQ1 handler that reads port 0x60 and
    ; sends EOI to the PIC, and a timer handler for the 18.2 Hz tick.

    ; The shell never returns: it prints a prompt, reads a line and dispatches.
    call shell_run

.idle:
    hlt                                          ; unreachable, but a safe park
    jmp .idle

%include "console.asm"
%include "keyboard.asm"
%include "shell.asm"

; -------------------------------------------------------------- command table
;
; The table lives in .text (read-only data costs nothing here) and every handler
; entry is stored as a *relative* offset from COMMAND_TABLE_BASE.
;
; That is not a style choice.  An absolute `dw cmd_help` needs a 16-bit
; relocation even when both symbols are in the same section, and COFF rejects it.
; A difference between two symbols in one section is a pure number the assembler
; resolves itself, so `dw cmd_help - command_table` assembles cleanly; the
; dispatcher adds COMMAND_TABLE_BASE back at run time.
;
; COMMAND_ENTRY_SIZE bytes per entry: a NUL-padded 8-byte name followed by the
; handler offset.  A zero first byte ends the table.  Keep this in step with
; text_help.
align 2
command_table:
    db 'help', 0, 0, 0, 0
    dw cmd_help    - command_table
    db 'echo', 0, 0, 0, 0
    dw cmd_echo    - command_table
    db 'clear', 0, 0, 0
    dw cmd_clear   - command_table
    db 'info', 0, 0, 0, 0
    dw cmd_info    - command_table
    db 'mem', 0, 0, 0, 0, 0
    dw cmd_mem     - command_table
    db 'ticks', 0, 0, 0
    dw cmd_ticks   - command_table
    db 'fact', 0, 0, 0, 0
    dw cmd_fact    - command_table
    db 'keylog', 0, 0
    dw cmd_keylog  - command_table
    db 'reboot', 0, 0
    dw cmd_reboot  - command_table
    db 0                                          ; end of table

; ------------------------------------------------- image header and kernel data

section .data

; First bytes of the image, exactly 16 of them.  The second-stage loader checks
; the magic, the architecture byte and a checksum over all 16 bytes, so a
; truncated or mis-built image reports itself instead of executing garbage.
; build.py fills in the entry offset, the size and the checksum.
;
;    0  magic "MYOS"       4  architecture (1 = 16-bit real mode)
;    5  version            6  flags (0)
;    7  checksum           8  entry offset from the image start (dword)
;   12  total image size (dword)
;
; The entry and the size are dwords so that one header format serves both the
; 16-bit kernel and the 32-bit one, which is far larger than the word-sized
; fields this header started with could describe.
image_header:
    db IMG_MAGIC_0, IMG_MAGIC_1, IMG_MAGIC_2, IMG_MAGIC_3
    db IMG_ARCH_16
    db 0x01                                       ; version 0.1
    db 0                                          ; flags
    db 0                                          ; checksum, patched by build.py
    dd 0                                          ; entry offset, patched by build.py
    dd 0                                          ; image size, patched by build.py
%if ($ - image_header) != IMG_HEADER_SIZE
%error "image_header must be exactly IMG_HEADER_SIZE bytes"
%endif

; Console state.  One contiguous block so a single base register reaches every
; field, and 32 bits wide so the accesses stay 32-bit (see console.asm).
console_data:
console_attribute:  dd ATTR_DEFAULT                ; +0
console_cursor_row: dd 0                           ; +4
console_cursor_col: dd 0                           ; +8
%if ($ - console_data) != (OFF_CURSOR_COL + 4)
%error "console_data layout does not match the OFF_* offsets used by console.asm"
%endif

; ------------------------------------------------------------ keyboard state
;
; One block so a single base register reaches every field.  keyboard_init and the
; INT 09h handler load keyboard_data into a 32-bit register and use [reg + KEY_...].
keyboard_data:
    dw 0                                   ; KEY_BIOS_OFFSET: BIOS INT 09h offset
    dw 0                                   ; KEY_BIOS_SEGMENT
    dw 0                                   ; KEY_WRITE: ring buffer write index
    dw 0                                   ; KEY_COUNT: scancodes recorded
    times KEY_BUFFER_SIZE db 0             ; KEY_BUFFER
%if ($ - keyboard_data) != (KEY_BUFFER + KEY_BUFFER_SIZE)
%error "keyboard_data layout does not match the KEY_* offsets used by keyboard.asm"
%endif

; --------------------------------------------------------------- shell state
;
; The field offsets are hand-written constants in shell.asm, so the block has to
; be exactly the size they assume -- checked at the end of it, below.  The padding
; after SHELL_LINE_LEN is load-bearing: SHELL_ARGS is addressed as a dword at
; offset 4, so a `dd` placed straight after the length word would sit at offset 2
; and shift every later field by two bytes.  That is not hypothetical either: with
; the padding missing, the line and token buffers were two bytes lower than the
; code believed, the token buffer's tail landed on msg_banner, and a 15-character
; command word overwrote the banner text.
shell_data:
    dw 0                                   ; SHELL_LINE_LEN: characters on the line
    dw 0                                   ; padding, so SHELL_ARGS is at offset 4
    dd 0                                   ; SHELL_ARGS: pointer past the command
    times LINE_BUFFER_SIZE db 0            ; SHELL_LINE: the line being edited
    times TOKEN_BUFFER_SIZE db 0           ; SHELL_TOKEN: the command word
%if ($ - shell_data) != (SHELL_TOKEN + TOKEN_BUFFER_SIZE)
%error "shell_data layout does not match the SHELL_* offsets used by shell.asm"
%endif


; -------------------------------------------------------------------- strings
msg_banner:   db 'myos 16-bit kernel', 13, 10, 0
msg_version:  db 'version 0.1 -- real mode, no BIOS calls after boot', 13, 10, 0
msg_ready:    db 'kernel started successfully', 13, 10, 0
msg_probe:    db 'image ', 0
msg_bytes:    db ' bytes, stack 0x', 0
msg_newline:  db 13, 10, 0

text_shell_banner: db 13, 10, 'Type `help` for a list of commands.', 13, 10, 13, 10, 0
text_prompt:       db 'myos> ', 0
text_unknown:      db 'unknown command: ', 0
text_unknown_hint: db '  (try `help`)', 13, 10, 0

text_help:
    db 'commands', 13, 10
    db '  help          this list', 13, 10
    db '  echo <text>   print the text back', 13, 10
    db '  clear         blank the screen', 13, 10
    db '  info          kernel version, image size, stack, int 09h vector', 13, 10
    db '  mem           memory the firmware reports and what myos uses', 13, 10
    db '  ticks         BIOS tick counter (18.2 Hz)', 13, 10
    db '  fact <0-8>    factorial, computed recursively', 13, 10
    db '  keylog        which handler owns int 09h, and why that matters', 13, 10
    db '  reboot        restart through the firmware', 13, 10, 0

text_info_version: db 'myos 0.1, 16-bit real mode', 13, 10, 0
text_info_image:   db 'kernel image ', 0
text_info_bytes:   db ' bytes', 13, 10, 0
text_info_stack:   db 'stack pointer 0x', 0
text_info_es:      db ', es 0x', 0
text_info_kbd:     db 'int 09h vector ', 0
text_info_kbd_bios:  db ' (unchanged since boot; keys come in via int 16h)', 0
text_info_kbd_other: db ' (replaced by something else)', 0

text_mem_conv:     db 'firmware reports ', 0
text_mem_kib:      db ' KiB conventional', 13, 10, 0
text_mem_layout:   db 'myos kernel image is ', 0
text_mem_layout2:  db ' bytes at 0x8000; kernel stack top 0xA000', 13, 10, 0

text_ticks:        db 'ticks ', 0
text_ticks_hex:    db ' (0x', 0
text_ticks_close:  db ')', 13, 10, 0
text_fact_usage:   db 'usage: fact <0-8>', 13, 10, 0
text_fact_range:   db 'fact: only 0-8 is supported (9! overflows 16 bits)', 13, 10, 0
text_keylog:       db 'int 09h handler ', 0
text_keylog2:      db ' -- the kernel does not own this vector; int 16h serves the keys', 13, 10, 0
text_reboot:       db 'rebooting...', 13, 10, 0
