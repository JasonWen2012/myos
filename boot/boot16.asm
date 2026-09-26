; myos first-stage boot sector (exactly 512 bytes).
;
; Being a valid boot sector means being exactly 512 bytes, which is the whole
; reason this file exists separately from stage2.  The BIOS loads it at
; 0000:7C00 and passes the boot drive in DL; all it does is relocate itself out
; of the way, read the second stage, and jump to it.  Everything else -- banner,
; geometry probing, kernel loading, header validation, error reporting -- lives
; in stage2, which has room to be careful.
;
; See boot.inc for the memory map: this sector relocates to 0x0600 so that
; 0x7C00 is free as a read buffer, and stage2 lands at 0x0700.
;
; Jump style
; ----------
; Every jump inside this file is *near*, because CS stays 0 throughout.  The one
; exception is the handoff to stage2, which has to be a far jump; it is emitted
; as raw bytes with an explicit comment.  Do not write `jmp seg:label`: nasm
; resolves the offset of that form from the start of the output file rather than
; from the org, which produced `EA 25 00 00 00` during bring-up -- a jump into
; the interrupt vector table that looked exactly like a failed disk read.
; build.py verifies the emitted bytes.
;
; The sector used to copy itself to 0x0600 and carry on from the BIOS copy at
; 0x7C00, which never worked as intended: nothing ever used 0x7C00 as a read
; buffer, the relocated copy was never executed, and with `org` missing every
; string this file prints was read from linear 0x1xx -- inside the interrupt
; vector table -- so "myos boot" was never seen.  It now stays where the BIOS put
; it and is assembled for that address.

[bits 16]

%include "boot.inc"

; Assembled for the address it runs at: the BIOS load address, where it stays.
[org BOOT_LOAD_OFF]

STAGE2_SECTORS  equ 8                     ; 4 KiB is ample for the second stage

jmp short boot_start
nop

boot_start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, BOOT_STACK_TOP
    cld
    sti

    mov [boot_drive], dl

    ; Report immediately: if stage2 never loads, this is the only evidence that
    ; the boot sector itself ran.
    mov si, msg_stage1
    call puts

    call load_stage2
    jc .failed

    mov dl, [boot_drive]
    ; ------------------------------------------------------------------
    ; Hand off to the second stage: far jump to 0000:0700.
    ;
    ; Written as raw bytes because nasm's `jmp seg:label` form resolves the
    ; offset from the output file start (see the note at the top), so both halves
    ; are spelled out and build.py checks these exact bytes with ndisasm.
    ; ------------------------------------------------------------------
    db 0xEA
    dw STAGE2_LOAD_OFF                    ; offset
    dw STAGE2_LOAD_SEG                    ; segment

.failed:
    mov si, msg_error
    call puts
    mov al, [disk_error]
    call puthex8
    mov si, msg_halt
    call puts
.halt_forever:
    cli
    hlt
    jmp .halt_forever

; ---------------------------------------------------------------- disk reading
;
; load_stage2: read STAGE2_SECTORS sectors starting at CHS (0,0,2) into the
; stage2 load address.  Retries each attempt and resets the controller between
; batches, because a real BIOS does fail reads that succeed after a reset.
load_stage2:
    mov word [disk_dest], STAGE2_LOAD_OFF
    mov word [disk_remaining], STAGE2_SECTORS
    mov byte [disk_cylinder], 0
    mov byte [disk_head], 0
    mov byte [disk_sector], 2               ; sector 1 is this boot sector
    mov word [attempts_left], DISK_RETRY_COUNT

.retry:
    mov ah, 0x02
    mov al, 1
    mov ch, [disk_cylinder]
    mov cl, [disk_sector]
    mov dh, [disk_head]
    mov dl, [boot_drive]
    mov bx, [disk_dest]
    int 0x13
    jnc .read_ok

    mov [disk_error], ah
    dec word [attempts_left]
    jz .reset
    jmp .retry

.reset:
    xor ah, ah                              ; reset disk system, then try once more
    mov dl, [boot_drive]
    int 0x13
    mov ah, 0x02
    mov al, 1
    mov ch, [disk_cylinder]
    mov cl, [disk_sector]
    mov dh, [disk_head]
    mov dl, [boot_drive]
    mov bx, [disk_dest]
    int 0x13
    jnc .read_ok

    mov [disk_error], ah
    stc
    ret

.read_ok:
    add word [disk_dest], SECTOR_SIZE
    inc byte [disk_sector]
    cmp byte [disk_sector], DEFAULT_SECTORS_PER_TRACK + 1
    jb .next
    mov byte [disk_sector], 1
    inc byte [disk_head]
    cmp byte [disk_head], DEFAULT_HEADS
    jb .next
    mov byte [disk_head], 0
    inc byte [disk_cylinder]
.next:
    dec word [disk_remaining]
    jnz .retry
    clc
    ret

; --------------------------------------------------------------------- helpers

; puts: write the NUL-terminated string at DS:SI through the BIOS teletype.
; Clobbers AX, BX and SI.
puts:
    push ax
    push bx
.next:
    lodsb
    test al, al
    jz .done
    mov ah, 0x0e
    mov bx, 0x0007
    int 0x10
    jmp .next
.done:
    pop bx
    pop ax
    ret

; puthex8: print AL as two hex digits.  Clobbers AX.
;
; The byte is parked in CH, not in AH: .digit sets AH = 0x0E for the teletype, so
; a value kept in AH is destroyed by the first digit and the second one always
; prints as 'E'.  That turned a disk error code into "code 0E".
puthex8:
    push cx
    mov ch, al
    shr al, 4
    call .digit
    mov al, ch
    call .digit
    pop cx
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
    mov ah, 0x0e
    mov bx, 0x0007
    int 0x10
    ret

; ------------------------------------------------------------------------ data

boot_drive:       db 0
disk_error:       db 0
disk_cylinder:    db 0
disk_head:        db 0
disk_sector:      db 0
disk_dest:        dw 0
disk_remaining:   dw 0
attempts_left:    dw 0

msg_stage1:       db 'myos boot', 13, 10, 0
msg_error:        db 'BOOT ERROR: cannot load stage2, code ', 0
msg_halt:         db 13, 10, 'System halted.', 13, 10, 0

    times 510 - ($ - $$) db 0
    dw 0xAA55
