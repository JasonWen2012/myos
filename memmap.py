#!/usr/bin/env python3
"""
Print the myos memory map with real numbers.

Everything asserted in the docs is measured here rather than restated: the boot
constants come from boot/boot.inc, the kernel layout comes from the linker, the
image contents are checked at their physical offsets, and the simulator is run to
show what is actually occupied once the kernel is up.

    python memmap.py              the whole map
    python memmap.py --constants  just the boot/boot.inc values
    python memmap.py --kernel     just the linked kernel layout
    python memmap.py --image      just the disk/image placement
    python memmap.py --live       just what the booted machine occupies
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build as myos_build  # noqa: E402
from emulator.machine import Machine  # noqa: E402
from tools import image as image_tools  # noqa: E402
from tools import cofllink, myfs, toolchain  # noqa: E402

K = 1024


def read_boot_constants() -> dict[str, int]:
    """Parse the %define values out of boot/boot.inc.

    Parsed rather than duplicated so the report cannot drift from the assembly.
    """
    text = (ROOT / "boot" / "boot.inc").read_text(encoding="ascii")
    constants: dict[str, int] = {}
    for match in re.finditer(r"^%define\s+([A-Z0-9_]+)\s+([^;\n]+)", text, re.M):
        name, expr = match.group(1), match.group(2).strip()
        try:
            value = eval(expr, {"__builtins__": {}}, {})       # noqa: S307
        except Exception:
            continue
        if isinstance(value, int):
            constants[name] = value
    return constants


def show_constants() -> None:
    c = read_boot_constants()
    print("boot/boot.inc constants")
    print("-" * 62)
    groups = [
        ("loader", ["BOOT_LOAD_OFF", "STAGE2_LOAD_OFF",
                    "SCRATCH_SEG", "SCRATCH_OFF", "STACK_SEG", "STACK_OFF",
                    "BOOT_STACK_TOP"]),
        ("kernel", ["KERNEL_LOAD_SEG", "KERNEL_LOAD_OFF", "KERNEL_STACK_TOP",
                    "REALMODE_TOP", "MAX_KERNEL_SECTORS"]),
        ("protected mode", ["KERNEL32_LOAD_SEG", "KERNEL32_LOAD_OFF",
                            "KERNEL32_LOAD_LIN"]),
        ("video", ["VIDEO_SEG", "VIDEO_BASE", "VIDEO_COLS", "VIDEO_ROWS"]),
        ("disk", ["KERNEL_START_LBA", "SECTOR_SIZE", "DEFAULT_SECTORS_PER_TRACK",
                  "DEFAULT_HEADS", "DEFAULT_CYLINDERS"]),
        ("image header", ["IMG_HEADER_SIZE", "IMG_OFF_ENTRY", "IMG_OFF_SIZE",
                          "IMG_OFF_CHECKSUM"]),
    ]
    for title, names in groups:
        print(f"  {title}")
        for name in names:
            if name in c:
                print(f"    {name:<22} {c[name]:#010x}  ({c[name]})")
    print()
    print("  derived addresses")
    scratch = (c["SCRATCH_SEG"] << 4) + c["SCRATCH_OFF"]
    stack = (c["STACK_SEG"] << 4) + c["STACK_OFF"]
    kernel_end = c["KERNEL_LOAD_OFF"] + c["MAX_KERNEL_SECTORS"] * c["SECTOR_SIZE"]
    print(f"    {'scratch buffer':<22} {scratch:#010x}")
    print(f"    {'handover stack':<22} {stack:#010x}")
    print(f"    {'kernel max end':<22} {kernel_end:#010x}"
          f"  (limit {c['REALMODE_TOP']:#x})")
    if kernel_end > c["REALMODE_TOP"]:
        print("    !! kernel ceiling exceeds REALMODE_TOP")


def show_kernel(arch: int = 16) -> None:
    toolchain_ = toolchain.discover()
    myos_build.BUILD_DIR.mkdir(exist_ok=True)
    if arch == 16:
        obj = myos_build.BUILD_DIR / "kernel16.o"
        toolchain.assemble(ROOT / "kernel16" / "main.asm", obj, fmt="win32",
                           include_dirs=[ROOT / "boot", ROOT / "kernel16"],
                           tc=toolchain_)
        sources = [obj]
        base = myos_build.KERNEL16_BASE
        entry_name = "kernel_main"
        layout = cofllink.loader_layout()
        key_symbols = ("image_header", "console_data", "console_attribute",
                       "msg_banner", "image_end", "kernel_main", "console_init",
                       "console_puts")
    else:
        sources = []
        for name in myos_build.KERNEL32_ASM_SOURCES:
            obj = myos_build.BUILD_DIR / (Path(name).stem + "32.o")
            toolchain.assemble(ROOT / "kernel32" / name, obj, fmt="win32",
                               include_dirs=[ROOT / "boot"], tc=toolchain_)
            sources.append(obj)
        for name in myos_build.KERNEL32_CPP_SOURCES:
            obj = myos_build.BUILD_DIR / (Path(name).stem + "32.o")
            toolchain.compile_cpp(ROOT / "kernel32" / name, obj,
                                  include_dirs=[ROOT / "kernel32"], tc=toolchain_)
            sources.append(obj)
        base = myos_build.KERNEL32_BASE
        entry_name = "kernel_entry"
        layout = cofllink.kernel_layout()
        key_symbols = ("image_header", "kernel_entry", "kmain", "kernel_stack_top",
                       "kernel_stack_bottom", "isr_stub_table", "console_init",
                       "shell_run")

    result = cofllink.link(sources, base=base, entry=entry_name, layout=layout)
    entry_address = cofllink.resolve_symbol(result.symbols, entry_name)
    # Patch the header the same way the build does; printing the raw linked image
    # would show an unfilled entry offset, size and checksum and look like a bug.
    header = cofllink.patch_image_header(result.image, entry_address - base,
                                         myos_build.HEADER_SIZE)
    print(f"linked {arch}-bit kernel")
    print("-" * 62)
    print(f"  base {result.base:#x}, total {result.size} bytes "
          f"({image_tools.kernel_sectors(result.image)} sectors)")
    print()
    for name in sorted(result.section_bounds,
                       key=lambda n: result.section_bounds[n][0]):
        start, size = result.section_bounds[name]
        if size:
            print(f"  {name:<8} {start:#08x} .. {start + size:#08x}  {size:>5} bytes")
    print()
    print("  key symbols")
    for name in key_symbols:
        address = cofllink.resolve_symbol(result.symbols, name)
        if address is not None:
            print(f"    {name:<22} {address:#08x}")
    print()
    print("  image header (16 bytes at image offset 0)")
    print(f"    magic      {header[0:4]!r}")
    print(f"    arch       {header[4]}  "
          f"({'16-bit real mode' if arch == 16 else '32-bit protected mode'})")
    print(f"    version    {header[5]:#04x}   flags {header[6]}")
    entry = int.from_bytes(header[8:12], "little")
    print(f"    entry off  {entry:#06x}  -> {result.base + entry:#08x}"
          f"  ({entry_name} is at {entry_address:#08x})")
    print(f"    size       {int.from_bytes(header[12:16], 'little')}"
          f"  (linked image is {len(result.image)} bytes)")
    print(f"    checksum   {header[7]:#04x}  (all 16 bytes sum to "
          f"{sum(header[:16]) & 0xFF:#x}, must be 0)")


def show_image(arch: int = 16) -> None:
    result = myos_build.build(arch=arch)
    print("disk image placement")
    print("-" * 62)
    stage1 = result.boot.read_bytes()
    stage2 = result.stage2.read_bytes()
    kernel = result.kernel.read_bytes()
    floppy = result.floppy.read_bytes()

    def where(data: bytes, label: str) -> None:
        offset = floppy.find(data)
        lba = offset // 512 if offset >= 0 else None
        print(f"  {label:<14} {len(data):>6} bytes  first at image offset "
              f"{offset:#08x}" + (f"  (LBA {lba})" if lba is not None else "  NOT FOUND"))

    where(stage1, "boot sector")
    where(stage2, "stage2")
    where(kernel, "kernel")

    print()
    print(f"  floppy image   {len(floppy)} bytes "
          f"({len(floppy) // 512} sectors of 512)")
    geometry = image_tools.floppy_geometry()
    print(f"  geometry       {geometry.cylinders} cyl x {geometry.heads} heads "
          f"x {geometry.sectors_per_track} spt")
    print()
    if arch == 32:
        print("  32-bit placement")
        print(f"    staged at       {myos_build.KERNEL32_STAGE_LIN:#x} "
              f"(window ends at {myos_build.KERNEL32_STAGE_END:#x})")
        print(f"    runs at         {myos_build.KERNEL32_BASE:#x}, "
              "copied there by the protected-mode entry")
        print(f"    bootstrap stack {myos_build.KERNEL32_STACK_LIN:#x}, above the image")
        print()
        print("  hard disk layout (32-bit only: the myfs volume lives here)")
        print("    LBA 0           MBR, four partition entries at byte 446")
        print(f"    LBA 1..{myos_build.STAGE2_LBA + myos_build.STAGE2_MAX_SECTORS - 1:<9} stage2 "
              f"({myos_build.STAGE2_MAX_SECTORS} sectors reserved)")
        print(f"    LBA {myos_build.KERNEL_LBA}..{myos_build.KERNEL_LBA + myos_build.MAX_KERNEL32_SECTORS - 1:<8} "
              f"kernel image budget ({myos_build.MAX_KERNEL32_SECTORS} sectors)")
        print(f"    LBA {myfs.PARTITION_LBA}..{myfs.PARTITION_LBA + myfs.PARTITION_SECTORS - 1:<8} "
              f"myfs volume, partition {myfs.PARTITION_INDEX}, type {myfs.PARTITION_TYPE:#04x}")
        print(f"    LBA {myfs.SCRATCH_LBA}..{myfs.DISK_SECTORS - 1:<7} "
              f"device scratch ({myfs.SCRATCH_SECTORS} sectors, `blk test` writes here)")
        print(f"    image size      {myfs.DISK_SECTORS} sectors "
              f"({myfs.DISK_SECTORS * 512} bytes = {myfs.DISK_SECTORS * 512 // (1024 * 1024)} MiB)")
        print(f"    volume          {myfs.BLOCK_COUNT} blocks of {myfs.BLOCK_SIZE} bytes, "
              f"{myfs.INODE_COUNT} inodes, first data block {myfs.FIRST_DATA_BLOCK}")
        return
    print("  why the kernel is at LBA", myos_build.KERNEL_LBA, "and not next to stage2:")
    print(f"    the loader reads the kernel to physical {myos_build.KERNEL_LOAD_LIN:#x}, "
          f"which is LBA {myos_build.KERNEL_LOAD_LIN // 512}")
    print(f"    so a kernel at LBA {myos_build.STAGE2_LBA + myos_build.STAGE2_MAX_SECTORS} "
          "would be read from sectors the read buffer itself overwrites")


def show_live() -> None:
    myos_build.build(arch=16)
    machine = Machine()
    machine.load_image(myos_build.IMAGES_DIR / "myos16.img")
    machine.boot()
    # run_until_idle: the kernel ends up in its shell, polling INT 16h, which is
    # as settled as this machine ever gets.
    reason = machine.run_until_idle(max_steps=400_000, idle_polls=60_000)
    c = read_boot_constants()
    mem = machine.cpu.mem

    print("live machine after boot")
    print("-" * 62)
    print(f"  {reason}, {machine.cpu.instructions} instructions")
    registers = machine.register_dump()
    print(f"  CS={registers['CS']:#06x} DS={registers['DS']:#06x} "
          f"ES={registers['ES']:#06x} SS={registers['SS']:#06x} "
          f"SP={registers['SP']:#06x}")
    print()
    print("  occupied regions (measured)")
    stage2_size = len((myos_build.BUILD_DIR / "stage2.bin").read_bytes())
    kernel_size = len((myos_build.BUILD_DIR / "kernel16.bin").read_bytes())
    regions = [
        ("IVT", 0x0000, 0x0400),
        ("BDA", 0x0400, 0x0500),
        ("boot sector", c["BOOT_LOAD_OFF"], c["BOOT_LOAD_OFF"] + c["SECTOR_SIZE"]),
        ("stage2", c["STAGE2_LOAD_OFF"], c["STAGE2_LOAD_OFF"] + stage2_size),
        ("scratch", (c["SCRATCH_SEG"] << 4) + c["SCRATCH_OFF"],
         (c["SCRATCH_SEG"] << 4) + c["SCRATCH_OFF"] + 512),
        ("kernel image", c["KERNEL_LOAD_OFF"], c["KERNEL_LOAD_OFF"] + kernel_size),
        ("VGA text buffer", c["VIDEO_BASE"], c["VIDEO_BASE"] + 80 * 25 * 2),
    ]
    for name, start, end in regions:
        print(f"    {name:<16} {start:#08x} .. {end:#08x}  {end - start:>6} bytes")

    used = sum(end - start for _, start, end in regions)
    print()
    print(f"  occupied total {used} bytes of the 1 MiB real-mode space "
          f"({used / K:.1f} KiB)")
    print(f"  free below the kernel {c['STAGE2_LOAD_OFF'] + stage2_size:#x} .. "
          f"{c['KERNEL_LOAD_OFF']:#x} "
          f"({c['KERNEL_LOAD_OFF'] - c['STAGE2_LOAD_OFF'] - stage2_size} bytes)")
    print(f"  kernel stack used  {c['KERNEL_STACK_TOP'] - registers['SP']} bytes "
          f"(top {c['KERNEL_STACK_TOP']:#x}, SP {registers['SP']:#06x})")
    print()
    print("  BIOS-reported memory (what a kernel can ask the BIOS for)")
    print(f"    INT 12h   {mem[0x413] | (mem[0x414] << 8)} KiB conventional")
    print("    INT 15h   15360 KiB extended (emulated constant)")

    print()
    print("  screen contents")
    for index, line in enumerate(machine.screen_chars()):
        if line.strip():
            print(f"    {index:2d}| {line.rstrip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="print the myos memory map")
    parser.add_argument("--constants", action="store_true")
    parser.add_argument("--kernel", action="store_true")
    parser.add_argument("--image", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="boot the 16-bit image in the in-tree emulator and "
                             "print the runtime snapshot (the emulator is 16-bit "
                             "only, so this has no 32-bit equivalent)")
    parser.add_argument("--arch", type=int, default=16, choices=(16, 32))
    args = parser.parse_args(argv)

    if args.live and args.arch != 16:
        print("--live runs the 16-bit image in the in-tree emulator; for the 32-bit "
              "kernel use --arch 32 --kernel/--image, or QEMU via run.py")
        return 2

    selected = args.constants or args.kernel or args.image or args.live
    if not selected or args.constants:
        show_constants()
    if not selected or args.kernel:
        show_kernel(args.arch)
    if not selected or args.image:
        show_image(args.arch)
    if not selected or args.live:
        show_live()
    return 0


if __name__ == "__main__":
    sys.exit(main())
