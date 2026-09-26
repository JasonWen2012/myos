; myos -- shared build-time constants.
;
; This file is included by the boot sectors and the kernels so that the disk
; geometry, load addresses and image-header layout are defined exactly once.
; Change a number here and every consumer follows, which is the only reliable
; way to keep the bootloader and the kernel from disagreeing about where things
; live.

%ifndef MYOS_BOOT_INC
%define MYOS_BOOT_INC

; ---------------------------------------------------------------- memory map
;
; The layout has to keep stage2 and the kernel from overwriting each other; they
; did overlap at 0x7E00/0x8000 on the first attempt, and the symptom was a kernel
; that ran halfway into the loader's own code.
;
;   0x0000:0500    BIOS data area used by the emulator and real firmware
;   0x0000:0700    second stage, up to STAGE2_MAX_SECTORS (4 KiB)
;   0x0000:3000    scratch: the kernel's first sector, for the header check
;   0x0000:7B00    stack top used only to hand control to the kernel, and the
;                  stack top while the loader runs (the same stack, reused)
;   0x0000:7C00    the boot sector, where the BIOS loads it and where it stays
;   0x0000:8000    kernel image, up to KERNEL_MAX_SECTORS (24 KiB)
;   0x0000:A000    the kernel's stack top, growing down toward its own image
;
; Every loader image is assembled with `org` set to the address it executes at,
; and that is load-bearing rather than tidiness: with no `org`, nasm resolves an
; absolute address from the start of the file, so a stage loaded at 0x700 read
; its strings from linear 0x3xx -- inside the interrupt vector table -- and wrote
; its variables there too.  Windows of the IVT are executable memory, so the only
; symptom was boot diagnostics that printed nothing at all.
;
; The kernel is deliberately kept inside segment 0 below the video buffer at
; 0xB8000, so its bytes can be loaded with plain segment-0 addressing.
%define REALMODE_TOP        0xE000      ; must stay below the video buffer

%define BOOT_LOAD_SEG       0x0000      ; where the BIOS puts the boot sector
%define BOOT_LOAD_OFF       0x7C00
%define STAGE2_LOAD_SEG     0x0000
%define STAGE2_LOAD_OFF     0x0700
%define SCRATCH_SEG         0x0300      ; 0x3000, free between stage2 and 0x7C00
%define SCRATCH_OFF         0x0000
; The stack used to hand control over.  It sits just below the loader's own
; stack at 0x7C00, which keeps it local to the loader and well clear of both the
; kernel's stack (0xA000) and the video buffer (0xB8000).  An earlier value of
; 0x3000:0x8000 put the top at 0xB000 -- below the kernel's own ceiling of
; 0xE000, i.e. a handover stack that the kernel image could grow into.
%define STACK_SEG           0x0000
%define STACK_OFF           0x7B00
%define KERNEL_LOAD_SEG     0x0000
%define KERNEL_LOAD_OFF     0x8000
%define KERNEL_LOAD_LIN     0x00008000
%define KERNEL_STACK_TOP    0xA000      ; kernel grows down to 0x8000+image size

; ------------------------------------------------- 32-bit kernel (stage two)
;
; The 32-bit image cannot be loaded where it runs: it lives at 1 MiB, which real
; mode cannot write to directly, and it is far too large for one ES:BX pair.  So
; the loader reads it into a staging window below 1 MiB and the protected-mode
; entry copies it up, after A20 is open.
;
;   0x00010000..0x00080000   staging window for the 32-bit image (448 KiB)
;   0x00006000..             boot info block handed to the kernel
;   0x00200000               stack the loader sets up before entering the kernel
;
; 0x80000 is where the extended BIOS data area starts, so the staging window stops
; there; the boot info block sits below it, clear of the loader's own stack at
; 0x7B00 and of the staging window.
%define KERNEL32_LOAD_LIN   0x00100000  ; where the image runs; needs A20
%define KERNEL32_STAGE_LIN  0x00010000  ; where the loader reads it first
%define KERNEL32_STAGE_END  0x00080000  ; first byte the staging window may not use
%define KERNEL32_STACK_LIN  0x00200000  ; bootstrap stack, above the loaded image
%define BOOT_INFO_LIN       0x00006000
%define BOOT_INFO_MAX_ENTRIES 32        ; must match kernel32/bootinfo.h

; Descriptor selectors built by the loader's flat GDT: base 0, limit 4 GiB.
%define GDT_SEL_CODE        0x0008
%define GDT_SEL_DATA        0x0010

; A20 gate.  The methods are tried in this order and the result is verified
; rather than assumed.
%define A20_BIOS_FN         0x2401
%define A20_FAST_PORT       0x92

; COM1, so the loader can say what it is doing where a headless run can read it.
%define COM1_BASE           0x03F8

; QEMU's isa-debug-exit device: writing a value here stops the emulator with
; status (value << 1) | 1.  The kernel uses it to end a self-test deterministically.
%define DEBUG_EXIT_PORT     0xF4
%define DEBUG_EXIT_PASS     0x10
%define DEBUG_EXIT_FAIL     0x02

%define BOOT_STACK_TOP      0x7C00
%define VIDEO_SEG           0xB800
%define VIDEO_OFF           0x0000
%define VIDEO_BASE          0xB8000     ; linear address of the text buffer
%define VIDEO_COLS          80
%define VIDEO_ROWS          25
%define SECTOR_SIZE         512

; VGA text attribute nibbles.  Defined here because both the boot banner and the
; kernel console need them, and an attribute byte is (background << 4) | fg.
%define COLOR_BLACK         0
%define COLOR_BLUE          1
%define COLOR_GREEN         2
%define COLOR_CYAN          3
%define COLOR_RED           4
%define COLOR_MAGENTA       5
%define COLOR_BROWN         6
%define COLOR_LIGHT_GREY    7
%define COLOR_DARK_GREY     8
%define COLOR_LIGHT_BLUE    9
%define COLOR_LIGHT_GREEN   10
%define COLOR_LIGHT_CYAN    11
%define COLOR_LIGHT_RED     12
%define COLOR_LIGHT_MAGENTA 13
%define COLOR_YELLOW        14
%define COLOR_WHITE         15

; ------------------------------------------------------------------- limits
; 48 sectors (24 KiB) of kernel at 0x8000 ends at 0xE000, staying inside segment
; 0 and below the video buffer, so the loader never has to step a segment
; register.  build.py enforces the same ceiling.
%define MAX_KERNEL_SECTORS  48
; 896 sectors (448 KiB) fills the staging window exactly: 0x10000 + 0x70000 =
; 0x80000, the start of the extended BIOS data area.
%define MAX_KERNEL32_SECTORS (KERNEL32_STAGE_END - KERNEL32_STAGE_LIN) / SECTOR_SIZE
%define DISK_RETRY_COUNT    3

; Logical block address where the kernel image starts on the medium.  It is far
; out on purpose: the loader reads to physical 0x8000, which is LBA 64, so a
; kernel near LBA 1 would be read from sectors that the read buffer itself has
; already overwritten.  build.py and boot/stage2.asm must agree on this number.
%define KERNEL_START_LBA    128

; Fallback geometry for the unlikely case that INT 13h AH=08h fails.  These are
; the standard 1.44 MiB floppy numbers, which is also what the emulator's
; default image uses.  The boot sector asks the BIOS first, so a hard disk image
; with a different geometry is handled without rebuilding the loader.
%define DEFAULT_SECTORS_PER_TRACK 18
%define DEFAULT_HEADS             2
%define DEFAULT_CYLINDERS         80

; ------------------------------------------------------- kernel image header
; The loader validates this before jumping, so a truncated or mis-built image
; produces a clear message instead of executing garbage.
;
;     0..3   magic "MYOS"
;     4      architecture (1 = 16-bit real mode, 2 = 32-bit protected mode)
;     5      version
;     6      flags (must be zero for now)
;     7      checksum, chosen so that all 16 header bytes sum to 0 mod 256
;     8..11  entry offset from the image start (dword)
;     12..15 image size in bytes (dword)
;
; The entry offset and the size are dwords on purpose.  They were words until the
; 32-bit stage arrived, and a word cannot describe an image larger than 64 KiB --
; which a C++ kernel passes on the first build.  The checksum moved from byte 12
; to byte 7 to make room, and the "16 bytes sum to zero" rule is unchanged, so
; the loader's check is still a single byte-wide loop.
%define IMG_MAGIC_0         0x4D        ; 'M'
%define IMG_MAGIC_1         0x59        ; 'Y'
%define IMG_MAGIC_2         0x4F        ; 'O'
%define IMG_MAGIC_3         0x53        ; 'S'
%define IMG_ARCH_16         1
%define IMG_ARCH_32         2
%define IMG_HEADER_SIZE     16
%define IMG_OFF_MAGIC       0
%define IMG_OFF_ARCH        4
%define IMG_OFF_VERSION     5
%define IMG_OFF_FLAGS       6
%define IMG_OFF_CHECKSUM    7
%define IMG_OFF_ENTRY       8
%define IMG_OFF_SIZE        12

%endif ; MYOS_BOOT_INC
