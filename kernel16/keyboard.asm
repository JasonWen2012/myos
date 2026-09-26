; myos 16-bit kernel: keyboard driver.
;
; %include'd by main.asm; code only, all state lives in the .data section there.
;
; Why the kernel does *not* take over INT 09h
; -------------------------------------------
; The obvious design is to install a handler on INT 09h, read the scancode from
; port 0x60, acknowledge the PIC and return.  That design is wrong here, and it
; cost real debugging time:
;
;   * a handler that reads port 0x60 *consumes* the keystroke, so the BIOS
;     handler never runs and the BIOS key queue that INT 16h reads stays empty.
;     The shell then blocks forever waiting for a key that the firmware never
;     saw -- and it looks exactly like a shell that does not work.
;
;   * chaining to the BIOS handler to fix that is worse.  The BIOS routine ends
;     with IRET, but a chain entered by `push seg:off` + `retf` has already left
;     the stack pointing at the CPU's interrupt frame, so the IRET returns to the
;     *interrupted instruction in the kernel* instead of to the BIOS caller.  The
;     kernel restarts from its entry point on the first keypress.  Making the
;     chain correct requires the frame layout the BIOS expects and a saved copy
;     of the frame tail, which is a lot of fragile machinery for no benefit.
;
; So the kernel leaves INT 09h to the firmware and reads keys through INT 16h.
; That is the honest choice for a real-mode kernel still running on the BIOS: the
; firmware has already done the scancode-to-ASCII work, including Shift and Caps
; Lock, and it drains the same 8042 that IRQ1 services.  The vector is captured so
; `info` can report it and `reboot` can confirm the firmware still owns it.
;
; Requires ES = VIDEO_SEG for console calls.

; --------------------------------------------------------------- vector state

; keyboard_init: remember the BIOS INT 09h vector and leave it installed.
;
; The captured value is what `info` prints, so a user can see that the firmware
; handler is in place rather than assuming it.
keyboard_init:
    push es
    push eax
    push ebx
    xor ax, ax
    mov es, ax
    lea_off ebx, keyboard_data
    mov ax, [es:KEYBOARD_VECTOR * 4]              ; BIOS handler offset
    mov [ebx + KEY_BIOS_OFFSET], ax
    mov ax, [es:KEYBOARD_VECTOR * 4 + 2]          ; BIOS handler segment
    mov [ebx + KEY_BIOS_SEGMENT], ax
    pop ebx
    pop eax
    pop es
    clc
    ret

; keyboard_vector_is_bios: CF clear when INT 09h still points where the firmware
; put it, CF set when something else has replaced it.
keyboard_vector_is_bios:
    push es
    push eax
    push ebx
    xor ax, ax
    mov es, ax
    lea_off ebx, keyboard_data
    mov ax, [es:KEYBOARD_VECTOR * 4]
    cmp ax, [ebx + KEY_BIOS_OFFSET]
    jne .changed
    mov ax, [es:KEYBOARD_VECTOR * 4 + 2]
    cmp ax, [ebx + KEY_BIOS_SEGMENT]
    jne .changed
    pop ebx
    pop eax
    pop es
    clc
    ret
.changed:
    pop ebx
    pop eax
    pop es
    stc
    ret

; ------------------------------------------------------------------ key input

; console_getkey: return the next key, blocking until one is available.
; Returns AL = ASCII (0 when the key has no ASCII form) and AH = scan code.
; Requires ESI = console data.
;
; INT 16h is the single source of truth for input, so nothing is consumed twice
; and Shift/Caps handling comes from the firmware.
console_getkey:
    mov ah, 0x00
    int 0x16
    ret

; console_key_available: ZF = 1 when no key is waiting; when one is, ZF = 0 and
; AL = ASCII, AH = scan code.
console_key_available:
    mov ah, 0x01
    int 0x16
    ret

; console_shift_state: AL = the BIOS keyboard flag byte at 0000:0417.
; Bit 0 right shift, bit 1 left shift, bit 2 ctrl, bit 3 alt, bit 6 caps lock.
; The BIOS data area is 0040:0017, i.e. linear 0x417 -- reading offset 0x017
; instead is an easy and completely silent mistake.
console_shift_state:
    push ebx
    mov ebx, KBD_FLAGS_LIN
    mov al, [ebx]
    pop ebx
    ret

; keyboard_vector: DX:AX = the INT 09h vector currently installed.
keyboard_vector:
    push es
    push ebx
    xor bx, bx
    mov es, bx
    mov ax, [es:KEYBOARD_VECTOR * 4]
    mov dx, [es:KEYBOARD_VECTOR * 4 + 2]
    pop ebx
    pop es
    ret
