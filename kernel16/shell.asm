; myos 16-bit kernel: command shell.
;
; %include'd by main.asm; code only, all state lives in the .data section there.
;
; Reads a line from the BIOS key queue, then dispatches the first word against a
; table of commands.  Matching is case-insensitive, so `HELP` and `help` are the
; same thing.
;
; Addressing: COFF cannot express a 16-bit relocation, so no state field is ever
; named as a 16-bit memory operand.  A 32-bit base register is loaded with the
; block address and the fields are reached as [reg + SHELL_...].  Strings are
; passed to the console in EDI.
;
; Requires ES = VIDEO_SEG and ESI = console data.

; shell_run: never returns.  Prompt, read a line, run the command, repeat.
shell_run:
    mov edi, text_shell_banner
    mov al, ATTR_DIM
    call console_puts_colored
.prompt:
    mov edi, text_prompt
    mov al, ATTR_OK
    call console_puts_colored
    call shell_readline
    mov edi, msg_newline
    call console_puts
    call shell_dispatch
    jmp .prompt

; --------------------------------------------------------------- line input
;
; shell_readline: read into the line buffer until Enter, echoing as it goes and
; handling backspace.  Leaves the line NUL-terminated and updates SHELL_LINE_LEN.
; The buffer is a fixed size and a longer line is refused rather than allowed to
; run into the console state that follows it in .data.
shell_readline:
    push ebx
    lea_off ebx, shell_data
    ; The length is reset exactly once, here.  Note where this line sits: if the
    ; reset were inside the `.read` loop below, every keystroke would wipe the
    ; line and only the newest character would survive -- which looks like the
    ; kernel dropping characters rather than the shell clearing its own buffer.
    mov word [ebx + SHELL_LINE_LEN], 0
.read:
    call console_getkey
    test al, al
    jz .read                                  ; no ASCII form (arrows, F-keys)

    cmp al, 13
    je .enter
    cmp al, 8
    je .backspace
    cmp al, 32
    jb .read                                  ; other control characters
    cmp al, 126
    ja .read                                  ; DEL and the high half

    movzx ecx, word [ebx + SHELL_LINE_LEN]
    cmp ecx, LINE_BUFFER_SIZE - 1
    jae .read                                 ; full: refuse, do not wrap
    mov [ebx + SHELL_LINE + ecx], al
    inc ecx
    mov [ebx + SHELL_LINE_LEN], cx
    call console_putc                         ; echo it
    jmp .read

.backspace:
    movzx ecx, word [ebx + SHELL_LINE_LEN]
    test ecx, ecx
    jz .read
    dec ecx
    mov [ebx + SHELL_LINE_LEN], cx
    call console_putc                         ; AL is still 8, so this rubs it out
    jmp .read

.enter:
    movzx ecx, word [ebx + SHELL_LINE_LEN]
    mov byte [ebx + SHELL_LINE + ecx], 0      ; terminate
    pop ebx
    ret

; ---------------------------------------------------------------- dispatch
;
; shell_dispatch: split the first word of the line into the token buffer and call
; the matching handler.  Handlers see the rest of the line through SHELL_ARGS.
shell_dispatch:
    push ebx
    lea_off ebx, shell_data

    lea_off eax, shell_data                   ; EAX walks the line buffer
    add eax, SHELL_LINE
    call shell_skip_spaces

    mov edi, ebx
    add edi, SHELL_TOKEN
    xor ecx, ecx
.token_loop:
    mov dl, [eax]
    test dl, dl
    jz .token_done
    cmp dl, ' '
    je .token_done
    cmp ecx, TOKEN_BUFFER_SIZE - 1
    jae .skip_char
    mov [edi], dl
    inc edi
    inc ecx
.skip_char:
    inc eax
    jmp .token_loop
.token_done:
    mov byte [edi], 0
    mov [ebx + SHELL_ARGS], eax               ; arguments follow the first space
    test ecx, ecx
    jz .done                                  ; empty line: prompt again

    lea_off ebp, command_table
.find:
    cmp byte [ebp], 0                         ; zero-length name ends the table
    je .unknown
    lea_off eax, shell_data
    add eax, SHELL_TOKEN
    mov edi, ebp
    call shell_name_equal
    jc .found
    add ebp, COMMAND_ENTRY_SIZE
    jmp .find

.found:
    ; The table stores each handler as an offset from command_table, so the table
    ; *base* is what has to go back in -- not EBP, which by now points at the
    ; matched entry.  Adding EBP worked for the first entry only, because there the
    ; two happened to be the same address; every other command landed a few bytes
    ; inside its own handler.  `help` looked fine while `echo` printed nothing but
    ; its trailing newline, and commands further down the table jumped into
    ; unrelated code.  Adding the base in 16 bits also gives the address wrap that
    ; a real-mode instruction pointer has.
    mov bx, [ebp + COMMAND_HANDLER_OFFSET]
    lea_off eax, command_table
    add bx, ax
    movzx ebx, bx
    call ebx
    pop ebx
    ret

.unknown:
    mov edi, text_unknown
    call console_puts
    lea_off edi, shell_data
    add edi, SHELL_TOKEN
    call console_puts
    mov edi, text_unknown_hint
    call console_puts

.done:
    pop ebx
    ret

; shell_skip_spaces: EAX = the first non-space byte at or after EAX.
shell_skip_spaces:
    mov dl, [eax]
    cmp dl, ' '
    jne .done
    inc eax
    jmp shell_skip_spaces
.done:
    ret

; shell_name_equal: compare the NUL-terminated token at EAX with the 8-byte table
; name at EDI, ignoring case.  CF set when they are equal.
shell_name_equal:
    push eax
    push edi
    push ecx
    xor ecx, ecx
.loop:
    mov dl, [eax]
    mov dh, [edi + ecx]
    cmp dl, 'a'                               ; uppercase both sides
    jb .dl_ready
    cmp dl, 'z'
    ja .dl_ready
    sub dl, 32
.dl_ready:
    cmp dh, 'a'
    jb .dh_ready
    cmp dh, 'z'
    ja .dh_ready
    sub dh, 32
.dh_ready:
    cmp dl, dh
    jne .not_equal
    test dl, dl
    jz .equal                                 ; both ended together
    inc eax
    inc ecx
    cmp ecx, 8
    jb .loop
    cmp byte [eax], 0                         ; table name is full width
    jne .not_equal
.equal:
    pop ecx
    pop edi
    pop eax
    stc
    ret
.not_equal:
    pop ecx
    pop edi
    pop eax
    clc
    ret

; shell_args: EAX = pointer to the arguments that follow the command word, with
; leading spaces skipped.  Handlers use this instead of touching the block.
shell_args:
    push ebx
    lea_off ebx, shell_data
    mov eax, [ebx + SHELL_ARGS]
    pop ebx
    jmp shell_skip_spaces

; ---------------------------------------------------------------- commands
;
; Handlers are called with ESI = console data and ES = VIDEO_SEG, and must
; preserve ESI, ES, SS and SP.

; cmd_help: list the commands.  The text and the command table are separate, so
; keep the two in step by hand.
cmd_help:
    mov edi, text_help
    call console_puts
    ret

; cmd_echo: print everything after the command word.
cmd_echo:
    call shell_args
    mov edi, eax
    call console_puts
    mov edi, msg_newline
    call console_puts
    ret

; cmd_clear: blank the screen and home the cursor.
cmd_clear:
    call console_clear
    ret

; cmd_info: what the kernel can say about itself without a filesystem.
cmd_info:
    mov edi, text_info_version
    call console_puts

    mov edi, text_info_image
    call console_puts
    lea_off eax, image_header                 ; image size, from the header
    mov eax, [eax + IMG_OFF_SIZE]             ; dword; build.py keeps this image under 64 KiB
    call console_put_dec
    mov edi, text_info_bytes
    call console_puts

    mov edi, text_info_stack
    call console_puts
    mov ax, sp
    call console_put_hex16

    mov edi, text_info_es
    call console_puts
    mov ax, es
    call console_put_hex16
    mov edi, msg_newline
    call console_puts

    mov edi, text_info_kbd
    call console_puts
    call keyboard_vector
    xchg ax, dx                                   ; print segment:offset
    call console_put_hex16
    mov al, ':'
    call console_putc
    xchg ax, dx
    call console_put_hex16

    call keyboard_vector_is_bios
    jc .kbd_changed
    mov edi, text_info_kbd_bios
    call console_puts
    jmp .kbd_done
.kbd_changed:
    mov edi, text_info_kbd_other
    call console_puts
.kbd_done:
    mov edi, msg_newline
    call console_puts
    mov ax, VIDEO_SEG                         ; keyboard_vector used ES
    mov es, ax
    ret

; cmd_keylog: report the INT 16h key queue state the firmware exposes.
;
; This is deliberately not a scancode counter owned by the kernel: the kernel
; does not intercept INT 09h (see keyboard.asm for why).  What this shows is that
; the firmware handler is installed and answering, which is what makes typing work.
cmd_keylog:
    mov edi, text_keylog
    call console_puts
    call keyboard_vector
    xchg ax, dx
    call console_put_hex16
    mov al, ':'
    call console_putc
    xchg ax, dx
    call console_put_hex16
    mov edi, text_keylog2
    call console_puts
    ret

; cmd_mem: what the firmware reports, and what myos occupies.
cmd_mem:
    mov edi, text_mem_conv
    call console_puts
    mov ebx, BDA_MEMORY_KB                    ; BIOS memory size, 0040:0013
    mov ax, [ebx]
    call console_put_dec
    mov edi, text_mem_kib
    call console_puts

    mov edi, text_mem_layout
    call console_puts
    lea_off eax, image_header
    mov eax, [eax + IMG_OFF_SIZE]             ; dword, as in cmd_info
    call console_put_dec
    mov edi, text_mem_layout2
    call console_puts
    ret

; cmd_ticks: the BIOS 18.2 Hz tick counter at 0040:006C, linear 0x46C.
cmd_ticks:
    mov edi, text_ticks
    call console_puts
    mov ebx, BDA_TICKS
    mov ax, [ebx]
    call console_put_dec
    mov edi, text_ticks_hex
    call console_puts
    mov ax, [ebx]
    call console_put_hex16
    mov edi, text_ticks_close
    call console_puts
    ret

; cmd_fact: recursive factorial, to exercise the stack and the call convention.
; Arguments above 8 are refused because 9! does not fit in 16 bits.
cmd_fact:
    call shell_args
    call shell_parse_dec                      ; AX = value, CF set on garbage
    jc .bad
    cmp ax, 8
    ja .too_big
    mov bx, ax
    mov ax, bx
    call fact_recursive
    call console_put_dec
    mov edi, msg_newline
    call console_puts
    ret
.bad:
    mov edi, text_fact_usage
    call console_puts
    ret
.too_big:
    mov edi, text_fact_range
    call console_puts
    ret

; fact_recursive: AX = n!, by calling itself down to 0.
fact_recursive:
    test ax, ax
    jz .one
    push ax
    dec ax
    call fact_recursive
    pop bx
    mul bx                                    ; DX:AX = AX * BX
    ret
.one:
    mov ax, 1
    ret

; cmd_reboot: restart through the firmware.  INT 09h is still the BIOS handler,
; so there is nothing of the kernel's to uninstall first.
cmd_reboot:
    mov edi, text_reboot
    call console_puts
    int 0x19
    ret

; -------------------------------------------------------------- number input

; shell_parse_dec: read a decimal number from the string at EAX.
; Returns AX = value with CF clear, or CF set when the text is empty or not a
; number at all.  Stops at the first non-digit.
shell_parse_dec:
    push ebx
    push ecx
    push edx
    xor ecx, ecx                              ; digits seen
    xor ebx, ebx                              ; accumulator
.loop:
    mov edx, [eax]                            ; low byte is the character
    cmp dl, '0'
    jb .end
    cmp dl, '9'
    ja .end
    sub edx, '0'
    imul ebx, ebx, 10
    add ebx, edx
    inc ecx
    inc eax
    cmp ebx, 0xFFFF
    jbe .loop
    jmp .bad                                  ; would not fit in 16 bits
.end:
    test ecx, ecx
    jz .bad
    mov eax, ebx
    pop edx
    pop ecx
    pop ebx
    clc
    ret
.bad:
    pop edx
    pop ecx
    pop ebx
    stc
    ret
