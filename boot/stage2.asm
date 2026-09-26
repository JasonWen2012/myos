; myos second-stage bootloader.
;
; Loaded by the first-stage boot sector at 0000:0700, which is also the address
; this file is assembled for (`org` below).  It has room to be careful, so
; everything the loader actually has to get right lives here:
;
;   * draw the boot banner by writing cells into the video buffer directly, so
;     something is on screen before depending on anything else;
;   * probe the drive geometry with INT 13h AH=08h, so one build boots from both
;     a floppy image and a partitioned hard disk image;
;   * read the kernel's first sector into a scratch buffer and validate its image
;     header (magic, architecture, and a self-checking checksum) *before* loading
;     the rest, so a bad image is reported rather than executed;
;   * enter the kernel at the offset the header records.  The header sits at
;     offset zero of the image, so jumping to the load address would execute the
;     header bytes themselves -- that bug is invisible until the kernel misbehaves;
;   * on a read error, report the BIOS error code and the failing CHS.  That one
;     detail is what makes a failed boot debuggable instead of mysterious.
;
; Memory while this runs:
;   0000:0700   this code
;   0000:3000   scratch, the kernel's first sector (segment 0x0300)
;   0000:7B00   stack top, growing down toward 0x0700
;   0000:7C00   the first-stage boot sector, untouched while this runs
;   0000:8000   kernel image, up to KERNEL_MAX_SECTORS sectors (24 KiB)

[bits 16]

%include "boot.inc"

; Assembled for the address the first stage loads it at.
[org STAGE2_LOAD_OFF]

%define KERNEL_DEST       (KERNEL_LOAD_SEG << 4) + KERNEL_LOAD_OFF
%define SCRATCH_DEST      (SCRATCH_SEG << 4) + SCRATCH_OFF

stage2_start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, BOOT_STACK_TOP
    cld
    sti

    mov [boot_drive], dl

    call serial_init
    call draw_banner
    call read_geometry

    ; ---------------------------------------------------- validate the header
    mov cx, 1
    mov dx, KERNEL_START_LBA
    mov edi, SCRATCH_DEST
    call read_sectors
    jc .load_failed

    ; ES is set here rather than inherited from the read: read_chunk now leaves ES
    ; pointing at the 64 KiB page holding the destination, which for the scratch
    ; buffer is segment 0.  check_header reads through ES:SI, so the pair has to be
    ; the buffer's, spelled out.
    mov ax, SCRATCH_SEG
    mov es, ax
    mov si, SCRATCH_OFF
    call check_header
    jc .header_failed
    call status_ok

    ; ------------------------------------------------------ load it all (arch)
    ; A 16-bit image runs where it is loaded, at 0x8000, and is entered with a far
    ; return.  A 32-bit image cannot run there -- it is linked for 1 MiB -- so it
    ; is staged below 1 MiB and copied up by the protected-mode entry point.
    mov cx, [sectors_to_read]
    mov dx, KERNEL_START_LBA
    cmp byte [kernel_arch], IMG_ARCH_32
    je .load_32

    mov edi, KERNEL_DEST
    call read_sectors
    jc .load_failed

    ; ------------------------------------------------------------ enter it
    ; The entry point is the load address plus the offset the header records.  It
    ; must come from the header, because the header sits at offset zero of the
    ; image -- jumping to the load address would run the header bytes as code.
    ;
    ; The handover uses a far return, which needs the return address on the stack.
    ; SS:SP is moved to the dedicated handover slot *first* (this is the whole
    ; point): if the stack were left wherever the loader had it, retf would pop
    ; from memory that has nothing to do with the values just pushed.  In bring-up
    ; that read 0:0x7C00, which is the loader's own code, and the kernel was
    ; entered at a random address.  Interrupts are still off here, so the SS:SP
    ; switch cannot be interrupted.
    cli
    mov ax, STACK_SEG
    mov ss, ax
    mov sp, STACK_OFF                       ; see boot.inc for why this address

    mov ax, SCRATCH_SEG
    mov es, ax
    mov si, SCRATCH_OFF
    mov ebx, [es:si + IMG_OFF_ENTRY]        ; dword, but a 16-bit image lives below 64 KiB
    add bx, KERNEL_LOAD_OFF                 ; BX = entry offset within segment 0
    mov dl, [boot_drive]
    push word KERNEL_LOAD_SEG               ; return segment
    push bx                                 ; return offset
    retf                                    ; CS:IP from the handover stack

.load_32:
    mov edi, KERNEL32_STAGE_LIN
    call read_sectors
    jc .load_failed
    call collect_boot_info
    call enable_a20
    jc .a20_failed
    jmp enter_protected_mode

; ---------------------------------------------------------------------- errors

.load_failed:
    mov si, msg_err_load
    call puts
    mov al, [disk_error]
    call puthex8
    mov si, msg_trace
    call puts
    mov al, [disk_cylinder]
    call puthex8
    mov al, [disk_head]
    call puthex8
    mov al, [disk_sector]
    call puthex8
    jmp fatal

.header_failed:
    mov si, msg_err_header
    call puts
    mov ax, SCRATCH_SEG
    mov es, ax
    mov si, SCRATCH_OFF
    mov al, [es:si + IMG_OFF_ARCH]
    call puthex8
    jmp fatal

.a20_failed:
    mov si, msg_err_a20
    call puts
    jmp fatal

fatal:
    mov si, msg_halt
    call puts
.halt_forever:
    cli
    hlt
    jmp .halt_forever

; -------------------------------------------------------------------- serial
;
; Everything this stage reports goes to COM1 as well as to the screen.  A
; headless run (QEMU with -display none) can only be read through the serial
; port, and bringing up protected mode is exactly the kind of work where the
; loader's own diagnostics are the only evidence of how far it got.

; serial_init: 115200 8N1, FIFO on.  Clobbers AX and DX.
serial_init:
    push ax
    push dx
    mov dx, COM1_BASE + 1
    xor al, al
    out dx, al                              ; no interrupts from the UART
    mov dx, COM1_BASE + 3
    mov al, 0x80                            ; DLAB: next two writes are the divisor
    out dx, al
    mov dx, COM1_BASE + 0
    mov al, 0x01                            ; divisor 1 -> 115200 baud
    out dx, al
    mov dx, COM1_BASE + 1
    xor al, al
    out dx, al
    mov dx, COM1_BASE + 3
    mov al, 0x03                            ; 8 bits, no parity, one stop, DLAB off
    out dx, al
    mov dx, COM1_BASE + 2
    mov al, 0xC7                            ; FIFO on, cleared, 14-byte threshold
    out dx, al
    mov dx, COM1_BASE + 4
    mov al, 0x0B                            ; DTR, RTS and OUT2
    out dx, al
    pop dx
    pop ax
    ret

; serial_putc: send AL, waiting for the transmit register.  Preserves AX and DX.
serial_putc:
    push ax
    push dx
    mov dx, COM1_BASE + 5
.wait:
    in al, dx
    test al, 0x20                           ; transmit holding register empty
    jz .wait
    pop dx
    pop ax
    push ax
    push dx
    mov dx, COM1_BASE
    out dx, al
    pop dx
    pop ax
    ret

; ------------------------------------------------------------------- banner
;
; A video cell is two bytes, character first, so a word written to the text
; buffer is (attribute << 8) | character.  Getting that order backwards fills the
; screen with attribute 0x00 -- black on black -- which looks exactly like a
; screen that was never drawn.
%define CELL(ch, attr)   (((attr) << 8) | (ch))
%define ATTR_BLUE_WHITE  0x1F            ; blue background, bright white text
%define ATTR_BLUE_GREY   0x17            ; blue background, light grey text
%define ATTR_BLUE_GREEN  0x1A            ; blue background, bright green text

; draw_banner: paint the whole screen, then the title, subtitle and status line,
; by writing video cells directly.  Works regardless of what INT 10h reports.
draw_banner:
    mov ax, VIDEO_SEG
    mov es, ax
    xor di, di
    ; CX first, then AX: the loop counter and the fill value both want a register
    ; and AX is the one that gets stored, so loading the count into AX would write
    ; the count as every cell's character/attribute pair.
    mov cx, VIDEO_COLS * VIDEO_ROWS
    mov ax, CELL(' ', ATTR_BLUE_WHITE)
.fill:
    mov [es:di], ax
    add di, 2
    loop .fill

    mov bh, ATTR_BLUE_WHITE
    mov di, (1 * VIDEO_COLS + 4) * 2
    mov si, text_title
    call paint

    mov bh, ATTR_BLUE_GREY
    mov di, (2 * VIDEO_COLS + 4) * 2
    mov si, text_subtitle
    call paint

    mov di, (4 * VIDEO_COLS + 4) * 2
    mov si, status_loading
    call paint
    ret

; status_ok: replace the status line with a success message.
status_ok:
    mov ax, VIDEO_SEG
    mov es, ax
    mov bh, ATTR_BLUE_GREEN
    mov di, (4 * VIDEO_COLS + 4) * 2
    mov si, status_ready
    call paint

    mov bh, ATTR_BLUE_GREY
    mov di, (5 * VIDEO_COLS + 4) * 2
    mov si, status_go
    call paint
    ret

; paint: write the NUL-terminated string at DS:SI starting at ES:DI, using BH as
; the attribute for every cell.  Clobbers AX, SI and DI.
paint:
.next:
    lodsb
    test al, al
    jz .done
    mov [es:di], al                          ; character goes in the low byte
    inc di
    mov [es:di], bh                          ; attribute goes in the high byte
    inc di
    jmp .next
.done:
    ret

; ------------------------------------------------------------------ geometry
;
; read_geometry: store the drive's CHS geometry in sectors_per_track /
; heads_per_drive / cylinders_per_drive.  Falls back to the standard 1.44 MiB
; floppy numbers when the BIOS call fails, and never leaves a zero behind -- a
; zero head or sector count would make the read loop never terminate.
read_geometry:
    push ax
    push bx
    push cx
    push dx
    mov ah, 0x08
    mov dl, [boot_drive]
    int 0x13
    jc .fallback
    test dh, dh
    jz .fallback

    mov al, cl
    and al, 0x3F                             ; sectors per track
    jz .fallback
    mov [sectors_per_track], al

    mov ax, cx                               ; AX = the raw CX from the BIOS
    mov ah, ch
    xor ch, ch
    shr ax, 6                                ; AL = cylinder bits 8-9
    or  ah, al                               ; AH = full 10-bit cylinder count
    mov [cylinders_per_drive], ah

    dec dh                                   ; DH was heads-1 on success
    mov [heads_per_drive], dh

    cmp byte [heads_per_drive], 0
    je .fallback
    cmp byte [cylinders_per_drive], 0
    je .fallback
    call sync_geometry_words
    jmp .done

.fallback:
    mov byte [sectors_per_track], DEFAULT_SECTORS_PER_TRACK
    mov byte [heads_per_drive], DEFAULT_HEADS
    mov byte [cylinders_per_drive], DEFAULT_CYLINDERS
    call sync_geometry_words
.done:
    pop dx
    pop cx
    pop bx
    pop ax
    ret

; sync_geometry_words: widen the byte geometry into the 16-bit values that
; lba_to_chs divides by.  Keeps one source of truth (the bytes) for both.
sync_geometry_words:
    push ax
    mov al, [sectors_per_track]
    xor ah, ah
    mov [sectors_per_track_16], ax
    mov al, [heads_per_drive]
    xor ah, ah
    mov [heads_per_drive_16], ax
    pop ax
    ret

; --------------------------------------------------------------- disk reading
;
; read_sectors: read CX sectors starting at logical block DX into the *linear*
; address EDI.  Splits the transfer at track boundaries, because the BIOS cannot
; read across one, and keeps each chunk inside a single 64 KiB page, because an
; ES:BX pair that straddles one cannot be handed to the floppy controller's DMA
; channel -- the BIOS answers such a read with error 09h, "data boundary error".
;
; The destination is linear rather than a caller-supplied ES:BX on purpose.  The
; 16-bit kernel is small enough to sit inside one segment, but the 32-bit image is
; staged at 0x10000 and runs to hundreds of kilobytes: with a fixed segment the
; offset would wrap at 64 KiB and the loader would write the image over itself,
; which looks exactly like a corrupt kernel instead of a loader bug.  ES:BX is
; therefore recomputed from EDI for every chunk.
;
; ES is aligned to a 64 KiB page and BX holds the offset inside it, so a chunk
; never crosses a page.  EDI is always a multiple of 512 here, so the offset in
; the page is a whole number of sectors and a chunk can use up to the 128 sectors
; the BIOS allows.  Deriving ES as `dest >> 4` instead looks equivalent and is not:
; it puts up to 15 bytes in BX and lets a 128-sector chunk run past a page
; boundary, which is invisible until an image grows past the first 64 KiB of the
; staging window -- exactly what happened when the kernel crossed 128 sectors.
;
; The starting CHS is derived from the LBA with lba_to_chs, so callers work in
; sector numbers and never have to think about heads and tracks.
; Clobbers AX, BX, CX, DX, SI, EDI and ES.
read_sectors:
    mov [disk_remaining], cx
    mov [disk_dest], edi                    ; 32-bit: the image may pass 64 KiB
    mov ax, dx                              ; AX = starting LBA
    call lba_to_chs                         ; fills disk_cylinder/head/sector

.next_chunk:
    cmp word [disk_remaining], 0
    je .done

    ; chunk = min(remaining, sectors left on this track)
    mov al, [disk_sector]
    neg al
    add al, [sectors_per_track]
    add al, 1
    mov bl, al
    xor bh, bh
    mov ax, [disk_remaining]
    cmp ax, bx
    jbe .size_ok
    mov ax, bx
.size_ok:
    ; ... and no more than is left in this 64 KiB page.  The offset inside the page
    ; is the low 16 bits of the linear destination, and it is a multiple of the
    ; sector size, so the sectors already used are a shift away:
    ;   sectors that fit = 128 - low16(dest) / 512
    mov bx, word [disk_dest]
    shr bx, 9
    neg bx
    add bx, 128
    cmp ax, bx
    jbe .capped
    mov ax, bx
.capped:
    cmp ax, 127                             ; the BIOS reads at most 128 sectors
    jbe .capped_ok
    mov ax, 127
.capped_ok:
    mov [chunk_count], al

    mov word [attempts_left], DISK_RETRY_COUNT
.attempt:
    call read_chunk
    jnc .chunk_done
    dec word [attempts_left]
    jnz .attempt

    xor ah, ah                              ; reset the controller, then retry
    mov dl, [boot_drive]
    int 0x13
    call read_chunk
    jc .hard_failure

.chunk_done:
    movzx eax, byte [chunk_count]
    sub [disk_remaining], ax
    shl eax, 9                              ; sectors -> bytes, in 32 bits
    add [disk_dest], eax

    mov al, [chunk_count]
    add [disk_sector], al
    mov al, [sectors_per_track]
    inc al
    cmp [disk_sector], al
    jb .next_chunk
    mov byte [disk_sector], 1
    inc byte [disk_head]
    mov al, [heads_per_drive]
    cmp [disk_head], al
    jb .next_chunk
    mov byte [disk_head], 0
    inc byte [disk_cylinder]
    mov al, [cylinders_per_drive]
    cmp [disk_cylinder], al
    jb .next_chunk

.hard_failure:
    mov byte [disk_error], 0x09             ; "data boundary error"
    stc
    ret

.done:
    clc
    ret

; lba_to_chs: AX is a logical block address; leaves the equivalent 1-based CHS in
; disk_cylinder / disk_head / disk_sector.  Clobbers AX, BX, DX.
;
;   LBA = (cylinder * heads + head) * sectors_per_track + (sector - 1)
lba_to_chs:
    xor dx, dx
    mov bx, [sectors_per_track_16]
    test bx, bx
    jz .guard
    div bx                                  ; AX = track, DX = sector-1
    mov bl, dl
    inc bl
    mov [disk_sector], bl

    xor dx, dx
    mov bx, [heads_per_drive_16]
    test bx, bx
    jz .guard
    div bx                                  ; AX = cylinder, DX = head
    mov [disk_cylinder], al
    mov [disk_head], dl
    ret
.guard:
    ; Geometry was not probed; report a boundary failure rather than looping.
    mov byte [disk_error], 0x09
    stc
    ret

; read_chunk: one INT 13h read of chunk_count sectors to the current linear
; disk_dest, at the current CHS.  Carries CF on failure and records AH in
; disk_error.
;
; ES:BX is derived here, per chunk, from the 32-bit disk_dest, with ES aligned to a
; 64 KiB page and BX holding the offset inside it: a transfer that crosses a page
; boundary is refused by the floppy controller's DMA channel with error 09h.  The
; linear address has to stay below 1 MiB plus a page for the shift to fit in a real
; segment register, which is why the staging window stops at 0x80000; build.py
; enforces the same ceiling.
read_chunk:
    mov eax, [disk_dest]
    shr eax, 16
    shl eax, 12                             ; segment of the 64 KiB page holding dest
    mov es, ax
    mov bx, [disk_dest]                     ; low 16 bits: the offset in that page
    mov ah, 0x02
    mov al, [chunk_count]
    mov ch, [disk_cylinder]
    mov cl, [disk_sector]
    mov dh, [disk_head]
    mov dl, [boot_drive]
    int 0x13
    jnc .ok
    mov [disk_error], ah
    stc
    ret
.ok:
    clc
    ret

; -------------------------------------------------------------- header check
;
; check_header: ES:SI points at the loaded first sector.  Verifies the magic, the
; architecture byte and the self-checking header checksum, records which kernel
; this is and how many sectors the whole image needs.  Carries CF on a bad image.
check_header:
    cmp byte [es:si + IMG_OFF_MAGIC + 0], IMG_MAGIC_0
    jne .bad
    cmp byte [es:si + IMG_OFF_MAGIC + 1], IMG_MAGIC_1
    jne .bad
    cmp byte [es:si + IMG_OFF_MAGIC + 2], IMG_MAGIC_2
    jne .bad
    cmp byte [es:si + IMG_OFF_MAGIC + 3], IMG_MAGIC_3
    jne .bad

    mov al, [es:si + IMG_OFF_ARCH]
    cmp al, IMG_ARCH_16
    je .arch_ok
    cmp al, IMG_ARCH_32
    jne .bad
.arch_ok:
    mov [kernel_arch], al

    xor al, al                              ; all 16 header bytes sum to zero
    mov cx, IMG_HEADER_SIZE
    mov bx, si
.sum:
    add al, [es:bx]
    inc bx
    loop .sum
    test al, al
    jnz .bad

    mov eax, [es:si + IMG_OFF_SIZE]          ; dword: a 32-bit image dwarfs a word
    add eax, SECTOR_SIZE - 1
    shr eax, 9
    test eax, eax
    jz .bad
    ; Each architecture has its own ceiling: the 16-bit image is loaded into one
    ; segment below the video buffer, the 32-bit one into the staging window.
    mov bx, MAX_KERNEL_SECTORS
    cmp byte [kernel_arch], IMG_ARCH_32
    jne .check_cap
    mov bx, MAX_KERNEL32_SECTORS
.check_cap:
    cmp eax, ebx
    ja .bad
    mov [sectors_to_read], ax
    clc
    ret
.bad:
    stc
    ret

; ------------------------------------------------------------------- helpers

; puts: write the NUL-terminated string at DS:SI through the BIOS teletype and to
; COM1.  Clobbers AX, BX and SI.
puts:
    push ax
    push bx
.next:
    lodsb
    test al, al
    jz .done
    call serial_putc
    mov ah, 0x0e
    mov bx, 0x0007
    int 0x10
    jmp .next
.done:
    pop bx
    pop ax
    ret

; ------------------------------------------------- 32-bit kernel bring-up
;
; Everything below runs only for an IMG_ARCH_32 image.  The order matters: the
; image is staged below 1 MiB, the memory map is collected while the BIOS is still
; callable, A20 is opened, and only then does the CPU leave real mode -- once CR0
; is set, no BIOS service can be called again.

; collect_boot_info: write the block the kernel reads at BOOT_INFO_LIN.
;
;   +0  "MYBI"    +4 version    +5 boot drive   +6 VGA mode   +7 VGA page
;   +8  memory map entry count (dword)   +12 entry size (dword)
;   +16 reserved
;   +32.. up to 32 raw INT 15h E820 entries
;
; A firmware without E820 leaves the count at zero, which the kernel reports
; honestly rather than inventing a map.
collect_boot_info:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    push es

    mov ax, BOOT_INFO_LIN >> 4
    mov es, ax
    xor di, di
    mov byte [es:di + 0], 'M'
    mov byte [es:di + 1], 'Y'
    mov byte [es:di + 2], 'B'
    mov byte [es:di + 3], 'I'
    mov byte [es:di + 4], 1                 ; version
    mov al, [boot_drive]
    mov byte [es:di + 5], al
    mov byte [es:di + 6], 3                 ; assume 80x25 colour until the BIOS says
    mov byte [es:di + 7], 0
    mov dword [es:di + 8], 0                ; entry count
    mov dword [es:di + 12], 24              ; entry size
    mov dword [es:di + 16], 0

    mov ah, 0x0f                            ; current video mode and page
    int 0x10
    mov [es:di + 6], al
    mov [es:di + 7], bh

    ; INT 15h AX=E820: the real memory map.  EBX=0 starts the walk and the BIOS
    ; returns the next continuation in EBX; ECX comes back as the size of the
    ; entry it actually wrote, which is the stride to advance by (some firmware
    ; writes 20 bytes, the extended form 24).
    xor si, si                              ; entries stored
    xor ebx, ebx
    mov di, 32                              ; right after the header
.entry:
    mov eax, 0xE820
    mov edx, 0x534D4150                     ; 'SMAP'
    mov ecx, 24
    int 0x15
    jc .store
    cmp eax, 0x534D4150                     ; some firmware returns without setting it
    jne .store
    cmp ecx, 20                             ; 20 bytes is the minimum a BIOS may write
    jb .store
    cmp ecx, 24                             ; never stride past the space we reserved
    jbe .stride_ok
    mov ecx, 24
.stride_ok:
    mov [es:12], ecx                        ; what the firmware is really using
    ; The kernel reads entries at a fixed 24-byte stride, so a firmware that
    ; writes the older 20-byte form gets its slot's tail zeroed and the walk still
    ; advances a whole slot.  Striding by the firmware's own count instead packed
    ; the entries 20 bytes apart while the kernel indexed them 24 apart -- and the
    ; kernel then read a memory map of zero entries.
    cmp ecx, 24
    jae .slot_ready
    mov dword [es:di + 20], 0
.slot_ready:
    inc si
    cmp si, BOOT_INFO_MAX_ENTRIES
    jae .store
    add di, 24
    test ebx, ebx
    jnz .entry

.store:
    ; SI is a 16-bit register and the field is a dword whose upper half is already
    ; zero, so this writes the count and nothing else.
    mov [es:8], si
    pop es
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

; enable_a20: open the gate and *verify* it, because a method reporting success
; without opening anything is a classic.  CF set means it is still shut.
;
; The check writes two different values to the same low offset with and without
; A20: if the gate is shut, the address wraps and the second write lands on the
; first.
enable_a20:
    push ax
    push bx
    push si
    push di
    push ds
    push es

    mov ax, A20_BIOS_FN                     ; 1: the firmware's own service
    mov bx, 1
    int 0x15

    call a20_is_open
    jnc .open

    in al, A20_FAST_PORT                    ; 2: the fast A20 port on the chipset
    or al, 0x02
    and al, 0xFE                            ; keep bit 0 (fast reset) clear
    out A20_FAST_PORT, al

    call a20_is_open
    jnc .open
    stc
    jmp .done

.open:
    clc
.done:
    pop es
    pop ds
    pop di
    pop si
    pop bx
    pop ax
    ret

; a20_is_open: CF clear when writes to 0x5000 and 0x105000 do not alias.
; Preserves every register it touches.
a20_is_open:
    push ax
    push ds
    push es
    xor ax, ax
    mov ds, ax
    mov ax, 0xFFFF
    mov es, ax                              ; 0xFFFF0: the top of the low megabyte

    mov al, [0x5000]                        ; save what is there
    push ax
    mov byte [0x5000], 0x00
    mov byte [es:0x5010], 0xFF              ; 0xFFFF:0x5010 = linear 0x105000
    mov al, [0x5000]
    pop bx
    mov [0x5000], bl                        ; restore
    cmp al, 0x00
    jne .shut
    clc
    jmp .done
.shut:
    stc
.done:
    pop es
    pop ds
    pop ax
    ret

; enter_protected_mode: never returns.
;
; Loads the flat GDT, sets CR0.PE, and far-jumps to the 32-bit entry.  The far
; jump is emitted as raw bytes rather than `jmp seg:label`: nasm resolves that
; form from the start of the output file rather than from the org (the same trap
; the boot sector documents), and the offset here has to be a 32-bit one, which
; needs the 0x66 operand-size prefix anyway.  build.py checks these exact bytes.
enter_protected_mode:
    mov si, status_32
    call puts

    cli
    lgdt [gdtr]

    mov eax, cr0
    or eax, 1                               ; CR0.PE: protected mode
    mov cr0, eax

    db 0x66, 0xEA                           ; far jump, 32-bit offset
    dd pm32_entry                           ; offset within segment 0
    dw GDT_SEL_CODE                         ; CS: the flat code descriptor

[bits 32]
pm32_entry:
    mov ax, GDT_SEL_DATA
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
    mov ss, ax
    mov esp, KERNEL32_STACK_LIN             ; requires A20, checked before we got here

    ; Copy the staged image up to the address it was linked for.  The size comes
    ; from the image's own header, which is still at the start of the staging
    ; window, so the loader never has to pass anything to the kernel.
    mov esi, KERNEL32_STAGE_LIN
    mov edi, KERNEL32_LOAD_LIN
    mov ecx, [KERNEL32_STAGE_LIN + IMG_OFF_SIZE]
    add ecx, 3
    shr ecx, 2                              ; dwords, rounded up
    cld
    rep movsd

    mov eax, [KERNEL32_STAGE_LIN + IMG_OFF_ENTRY]
    add eax, KERNEL32_LOAD_LIN
    jmp eax                                 ; into the kernel; it never returns
[bits 16]

; puthex8: print AL as two hex digits.  Clobbers AX.
;
; The byte is parked in CH, not in AH.  .digit needs AH = 0x0E for the teletype,
; so a value kept in AH is destroyed by the first digit and the second one prints
; as 'E' every time -- "bad kernel image, arch 0E" instead of "... arch 01".
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
    call serial_putc
    mov ah, 0x0e
    mov bx, 0x0007
    int 0x10
    ret

; ------------------------------------------------------------------------ data

boot_drive:          db 0
kernel_arch:         db IMG_ARCH_16
disk_error:          db 0
disk_cylinder:       db 0
disk_head:           db 0
disk_sector:         db 0
; 32-bit on purpose: the 32-bit image is staged across many 64 KiB windows, so a
; 16-bit destination would wrap and the loader would overwrite its own progress.
disk_dest:           dd 0
disk_remaining:      dw 0
chunk_count:         db 0
attempts_left:       dw 0
sectors_to_read:     dw 0

sectors_per_track:   db DEFAULT_SECTORS_PER_TRACK
heads_per_drive:     db DEFAULT_HEADS
cylinders_per_drive: db DEFAULT_CYLINDERS

; 16-bit copies of the geometry, because lba_to_chs divides by them and `div`
; needs a 16-bit divisor.  read_geometry keeps them in step with the bytes above.
sectors_per_track_16: dw DEFAULT_SECTORS_PER_TRACK
heads_per_drive_16:   dw DEFAULT_HEADS

; Flat descriptor table for the protected-mode entry.  Base 0 and a 4 GiB limit
; in both directions, so a linear address is the effective address and the kernel
; never has to think about segmentation again.  The descriptors are assembled as
; two dwords each because that is what the CPU reads.
align 8
gdt_descriptors:
    dd 0x00000000, 0x00000000               ; null descriptor
    dd 0x0000FFFF, 0x00CF9A00               ; 0x08: 32-bit code, base 0, limit 4 GiB
    dd 0x0000FFFF, 0x00CF9200               ; 0x10: 32-bit data, base 0, limit 4 GiB
gdt_end:
gdtr:
    dw gdt_end - gdt_descriptors - 1
    dd gdt_descriptors                      ; org 0x700 and segment 0, so a linear address

text_title:      db 'myos', 0
text_subtitle:   db 'a small operating system', 0
status_loading:  db 'loading kernel...', 0
; Padded to the same width as status_loading: these two share a screen line, and
; the shorter one left the tail of the longer one visible ("kernel loadedl...").
status_ready:    db 'kernel loaded    ', 0
status_go:       db 'starting kernel...', 0
status_32:       db 'entering protected mode...', 0
msg_err_load:    db 13, 10, 'BOOT ERROR: disk read failed, code ', 0
msg_trace:       db ' at CHS ', 0
msg_err_header:  db 13, 10, 'BOOT ERROR: bad kernel image, arch ', 0
msg_err_a20:     db 13, 10, 'BOOT ERROR: the A20 gate will not open; the 32-bit '
                 db 'kernel needs memory above 1 MiB', 0
msg_halt:        db 13, 10, 'System halted. Reset the machine to retry.', 13, 10, 0
