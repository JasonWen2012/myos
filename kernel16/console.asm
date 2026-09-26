; myos 16-bit kernel: text console.
;
; %include'd by main.asm, so this file contains only code: every byte of state
; lives in the single .data section in main.asm, and the section directives that
; bracket it are there too.  Assembling the kernel as one unit is not a style
; choice -- see the header comment in main.asm for why COFF forces it.
;
; Writes straight into the VGA text buffer at B800:0000 instead of calling the
; BIOS, which is what a real kernel does and what keeps the console working once
; the kernel stops trusting the BIOS data area.
;
; Register convention
; -------------------
;   DS  = 0                    always, so a linear address is the effective one
;   ES  = VIDEO_SEG            an invariant, set once by console_init
;   ESI = address of console_data, set by console_init and preserved by every
;         routine here, so all state is reached as [esi+OFF_...]
;   EDI = string pointer argument for console_puts / console_write, advancing
;         past the NUL as it goes
;
; Segment registers and SP are preserved.  Each routine lists the general
; registers it clobbers; console_putc, the primitive everything else is built on,
; clobbers none of them at all -- see its comment for why that matters.

; ------------------------------------------------------------- internal helpers

; con_cell_addr: DI = buffer offset of the current cursor cell.
; Requires ESI = console data; clobbers AX, DX and DI.
con_cell_addr:
    mov ax, [esi + OFF_CURSOR_ROW]
    mov dx, VIDEO_COLS * 2
    mul dx                                       ; DX:AX = row * 160
    mov di, ax
    mov ax, [esi + OFF_CURSOR_COL]
    shl ax, 1                                    ; two bytes per cell
    add di, ax
    ret

; con_advance: move the cursor one column right, wrapping and scrolling.
; Requires ESI = data base; clobbers EAX and, through console_newline, EAX.
; NOTE: this uses the full 32-bit EAX because the cursor fields are dd.  Never
; call it while a half-built cell word is live in AX -- it will overwrite it.
con_advance:
    mov eax, [esi + OFF_CURSOR_COL]
    inc eax
    mov [esi + OFF_CURSOR_COL], eax
    cmp eax, VIDEO_COLS
    jb .done
    mov dword [esi + OFF_CURSOR_COL], 0
    call console_newline
.done:
    ret

; ----------------------------------------------------------------- console API

; console_init: locate the state block, point ES at the video buffer, blank the
; screen, home the cursor.
;
; ES = VIDEO_SEG is established here and is an invariant for the rest of the
; console.  Every other routine relies on it rather than saving and restoring ES
; around each video access: a BIOS interrupt taken while ES sat on the stack
; pushed its own frame over the saved value, and the `pop es` then restored
; garbage, sending every subsequent store to linear 0 instead of the screen.
console_init:
    mov esi, console_data
    mov ax, VIDEO_SEG
    mov es, ax
    call console_clear
    ret

; console_clear: fill the screen with spaces in the active attribute.
; Requires ESI = console_data and ES = VIDEO_SEG.
console_clear:
    push ax
    push cx
    push di
    xor di, di
    mov cx, VIDEO_COLS * VIDEO_ROWS
    mov al, [esi + OFF_ATTRIBUTE]
    mov ah, al
    mov al, ' '
.fill:
    mov [es:di], ax
    add di, 2
    loop .fill
    mov dword [esi + OFF_CURSOR_ROW], 0
    mov dword [esi + OFF_CURSOR_COL], 0
    pop di
    pop cx
    pop ax
    ret

; console_newline: next line, scrolling when the cursor runs off the bottom.
; Requires ESI = console_data; clobbers AX.
console_newline:
    mov eax, [esi + OFF_CURSOR_ROW]
    inc eax
    mov [esi + OFF_CURSOR_ROW], eax
    cmp eax, VIDEO_ROWS
    jb .done
    call console_scroll
    mov dword [esi + OFF_CURSOR_ROW], VIDEO_ROWS - 1
.done:
    ret

; console_scroll: scroll the screen up one line.  The top 24 rows shift up by
; one row's worth of bytes, copied forward so the overlap is safe, and the last
; row is filled with spaces.
; Requires ESI = console_data and ES = VIDEO_SEG.
console_scroll:
    push ax
    push cx
    push si
    push di
    xor di, di
    mov si, VIDEO_COLS * 2                       ; source is one row lower
    mov cx, (VIDEO_ROWS - 1) * VIDEO_COLS * 2    ; bytes to move
.move:
    mov ax, [es:si]
    mov [es:di], ax
    add si, 2
    add di, 2
    loop .move

    mov al, [esi + OFF_ATTRIBUTE]
    mov ah, al                                   ; character and attribute
    mov al, ' '
    mov di, (VIDEO_ROWS - 1) * VIDEO_COLS * 2
    mov cx, VIDEO_COLS
.fill:
    mov [es:di], ax
    add di, 2
    loop .fill
    pop di
    pop si
    pop cx
    pop ax
    ret

; console_putc: write AL at the cursor and advance.  Handles CR, LF and BS.
;
; Requires ESI = console data AND ES = VIDEO_SEG.  PRESERVES EVERY GENERAL
; REGISTER.
;
; "Preserves everything" is not politeness, it is the fix for a bug that made the
; shell look like it was dropping keystrokes.  The line editor keeps the address
; of the shell data block in EBX while it echoes the character it just stored, and
; an earlier version of this routine parked the character in BL during the address
; computation.  BL is the low byte of EBX, so each echo added the character to
; that pointer: the next keystroke was written 0x68 bytes too high, the length
; field was updated in the wrong place, and `help` came back as
;
;     unknown command: hl
;
; A console primitive that quietly eats a register is a trap for every caller, so
; this one eats nothing and callers are free to keep live state in any register.
;
; The cell word (attribute in BH, character in BL) is assembled in BX because
; con_cell_addr needs AX for the address arithmetic.
;
; ES is an invariant for the whole console, set once by console_init.  Saving it
; on the stack around the address computation looks tidier but is unsafe: a BIOS
; interrupt taken during that window pushes its own frame over the saved value,
; so the `pop es` restores garbage and every subsequent store lands at linear 0
; instead of in the video buffer.
console_putc:
    cmp al, 13                                   ; CR: column 0, same row
    je .carriage_return
    cmp al, 10                                   ; LF: next row
    je .line_feed
    cmp al, 8                                    ; BS: step back and rub out
    je .backspace

    push eax
    push ebx
    push ecx
    push edx
    push edi
    movzx ebx, al                                ; BL = character
    mov al, [esi + OFF_ATTRIBUTE]
    mov bh, al                                   ; BH = attribute: BH:BL is the cell
    call con_cell_addr                           ; clobbers AX, DX, DI
    mov [es:di], bx
    call con_advance                             ; clobbers EAX
    pop edi
    pop edx
    pop ecx
    pop ebx
    pop eax
    ret

.carriage_return:
    mov dword [esi + OFF_CURSOR_COL], 0
    ret

.line_feed:
    push eax
    call console_newline
    pop eax
    ret

; Backspace erases as well as moving: the shell echoes the key it just consumed,
; so a console that only stepped the cursor back left the deleted character on the
; screen and the user's line no longer matched what they could see.
.backspace:
    cmp dword [esi + OFF_CURSOR_COL], 0
    je .backspace_done
    dec dword [esi + OFF_CURSOR_COL]
    push eax
    push ebx
    push edx
    push edi
    mov al, [esi + OFF_ATTRIBUTE]
    mov bh, al
    mov bl, ' '                                  ; attribute:space overwrites the cell
    call con_cell_addr
    mov [es:di], bx
    pop edi
    pop edx
    pop ebx
    pop eax
.backspace_done:
    ret

; console_puts: write the NUL-terminated string at DS:EDI, advancing EDI past the
; terminator so successive calls behave like a character stream.
;
; Requires ESI = console data block and preserves it.  Clobbers AX and EDI.
;
; ESI must stay on the console data block for the whole call: console_putc reads
; the active attribute from [esi + OFF_ATTRIBUTE], so pointing ESI at the message
; makes it fetch the attribute from inside the string.  During bring-up that wrote
; a stray byte into the message and filled the screen with the wrong colours.
; The string cursor therefore stays in EDI; console_putc preserves EDI for as long
; as that promise holds, the loop below is just a load, a store and a call.
console_puts:
.string:
    mov al, [edi]
    test al, al
    jz .done
    inc edi
    call console_putc
    jmp .string
.done:
    ret

; console_write: write ECX bytes from DS:EDI, advancing EDI past them.
; Requires ESI = console data block and preserves it; clobbers AX and EDI.
console_write:
    test ecx, ecx
    jz .done
.loop:
    mov al, [edi]
    inc edi
    call console_putc
    dec ecx
    jnz .loop
.done:
    ret

; console_puts_colored: print the string at DS:EDI using AL as the attribute for
; the duration of the call, then restore the previous attribute.
; Requires ESI = console_data; clobbers AX and EDI.
;
; The previous attribute is held in a register, not left in memory.  Writing a
; character at screen offset 0x1D lands on the attribute byte of cell 14, so a
; memory-based save/restore reads back a corrupted value and the whole console
; ends up with attribute 0 -- black text on black, indistinguishable from a
; screen that was never drawn.
console_puts_colored:
    push bx
    mov bl, al                              ; BL = the attribute to use
    xchg bl, [esi + OFF_ATTRIBUTE]          ; BL = the previous attribute
    call console_puts
    mov [esi + OFF_ATTRIBUTE], bl           ; restore from the register
    pop bx
    ret

; console_set_color: AL becomes the new attribute byte.
console_set_color:
    mov [esi + OFF_ATTRIBUTE], al
    ret

; console_get_color: AL = the current attribute byte.
console_get_color:
    mov al, [esi + OFF_ATTRIBUTE]
    ret

; console_set_cursor: DH = row, DL = column.
console_set_cursor:
    movzx eax, dh
    mov [esi + OFF_CURSOR_ROW], eax
    movzx eax, dl
    mov [esi + OFF_CURSOR_COL], eax
    ret

; console_get_cursor: row in DH, column in DL.
console_get_cursor:
    mov al, [esi + OFF_CURSOR_ROW]
    mov dh, al
    mov al, [esi + OFF_CURSOR_COL]
    mov dl, al
    ret

; console_put_hex8: AL as two hex digits.  Clobbers AX.
console_put_hex8:
    push ax
    mov ah, al
    shr al, 4
    call .digit
    mov al, ah
    call .digit
    pop ax
    ret
.digit:
    and al, 0x0F
    cmp al, 10
    jb .numeric
    add al, 'A' - 10
    jmp .emit
.numeric:
    add al, '0'
.emit:
    call console_putc
    ret

; console_put_hex16: AX as four hex digits.  Clobbers AX.
console_put_hex16:
    push ax
    mov al, ah
    call console_put_hex8
    pop ax
    call console_put_hex8
    ret

; console_put_hex32: DX:AX as eight hex digits, DX being the high half.
console_put_hex32:
    push ax
    push dx
    mov ax, dx
    call console_put_hex16
    pop dx
    pop ax
    call console_put_hex16
    ret

; console_put_dec: AX as unsigned decimal, no leading zeros.  Clobbers AX.
console_put_dec:
    push bx
    push cx
    mov bx, 10
    xor cx, cx
.divide:
    xor dx, dx
    div bx                                       ; AX = quotient, DX = remainder
    push dx
    inc cx
    test ax, ax
    jnz .divide
.emit:
    pop ax
    add al, '0'
    call console_putc
    loop .emit
    pop cx
    pop bx
    ret
