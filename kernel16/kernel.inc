; myos kernel-side shared definitions.
;
; Why this file exists: the COFF object format supports only 32-bit
; relocations.  Any instruction that takes a 16-bit immediate address -- most
; commonly `mov si, message` -- produces a 16-bit relocation that nasm refuses
; to emit at all ("COFF format does not support non-32-bit relocations"), and
; even if it did, the in-tree linker would have no way to express it.
;
; The fix is to reference addresses through 32-bit immediates and then truncate
; to the 16-bit offset the CPU needs, which is exactly what mov_off does.  DS
; stays 0 throughout the kernel and every kernel symbol lives in the first
; 64 KiB, so the truncated value is the correct offset and also, with DS=0, the
; correct linear address.

%ifndef MYOS_KERNEL_INC
%define MYOS_KERNEL_INC

; lea_off reg32, symbol
;   Load the address of `symbol` into a 32-bit register.
;
;   The register MUST be a 32-bit one (esi, edi, ebx, ...).  Writing
;   `mov si, symbol` would ask nasm for a 16-bit relocation, which the COFF
;   object format cannot represent at all ("COFF format does not support 16-bit
;   relocations"), so addresses always travel in 32-bit registers.
;
;   The kernel keeps DS = 0, and kernel images are linked below 0x10000, so the
;   loaded value is simultaneously the link address, the offset and the linear
;   address -- which is why no truncation is needed here.
%macro lea_off 2
    mov %1, %2
%endmacro

; lea_off32 reg, symbol
;   Load the full 32-bit address of a symbol into a 32-bit register, keeping
;   the upper half.  Used for the protected-mode kernel where the base address
;   genuinely matters.
%macro lea_off32 2
    mov %1, %2
%endmacro

; --------------------------------------------------------------- VGA colours
; The colour numbers themselves live in boot/boot.inc, which every module
; includes; only the pre-composed attribute bytes myos uses are defined here.

; attribute byte = (background << 4) | foreground
%define ATTR_DEFAULT        ((COLOR_BLACK << 4) | COLOR_LIGHT_GREY)
%define ATTR_BANNER         ((COLOR_BLACK << 4) | COLOR_LIGHT_CYAN)
%define ATTR_OK             ((COLOR_BLACK << 4) | COLOR_LIGHT_GREEN)
%define ATTR_WARN           ((COLOR_BLACK << 4) | COLOR_YELLOW)
%define ATTR_ERROR          ((COLOR_BLACK << 4) | COLOR_LIGHT_RED)
%define ATTR_DIM            ((COLOR_BLACK << 4) | COLOR_DARK_GREY)
%define ATTR_ON_BLUE        ((COLOR_BLUE << 4) | COLOR_WHITE)
%define ATTR_ON_BLUE_DIM    ((COLOR_BLUE << 4) | COLOR_LIGHT_GREY)

%endif ; MYOS_KERNEL_INC
