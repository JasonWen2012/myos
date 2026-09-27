"""Build myos images: assemble, link, patch the image header, pack disks.

Two kernels are supported:

  arch 16  kernel16/*.asm            flat binary loaded at 0x8000 by boot/boot16.asm
  arch 32  kernel32/*.cpp + *.asm    protected-mode kernel at 0x100000 (stage 4)

Every step asserts the invariants it depends on -- the boot sector being exactly
512 bytes with a 0x55AA signature, the image header checksum verifying, the
kernel fitting on the disk -- so a broken build fails here with a precise
message instead of producing an image that misbehaves under an emulator.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cofllink, image, myfs, toolchain  # noqa: E402

BOOT_DIR = ROOT / "boot"
KERNEL16_DIR = ROOT / "kernel16"
KERNEL32_DIR = ROOT / "kernel32"
BUILD_DIR = ROOT / "build"
IMAGES_DIR = ROOT / "images"
# The files the build packs into the myfs volume.  They are part of the source
# tree, and the kernel checks every one of them against the manifest the packer
# writes, which is how the format's two implementations stay honest.
FILES_DIR = ROOT / "files"
# Where the user's own files live.  Deliberately not under images/: `clean` wipes
# images/, and nobody wants a build command to delete their data.
DATA_DIR = ROOT / "data"
DATA_DISK = DATA_DIR / "myfs-data.img"

KERNEL16_SOURCES = ("main.asm", "console.asm")
# The 32-bit kernel grows one milestone at a time, so this list is what exists
# now rather than what is planned: build_kernel32 refuses to build if a source is
# missing, which is a clearer failure than a link error about a stub nobody wrote.
KERNEL32_ASM_SOURCES = ("boot.asm", "isr.asm", "ring3.asm", "switch.asm")
KERNEL32_CPP_SOURCES = ("kernel.cpp", "console.cpp", "libc.cpp", "gdt.cpp",
                        "idt.cpp", "pic.cpp", "pit.cpp", "keyboard.cpp",
                        "shell.cpp", "mem.cpp", "ata.cpp", "mbr.cpp", "fs.cpp",
                        "file.cpp", "pmm.cpp", "paging.cpp", "heap.cpp",
                        "panic.cpp", "tss.cpp", "usercopy.cpp", "syscall.cpp",
                        "user.cpp", "klog.cpp", "task.cpp", "sched.cpp")

# Ring-3 programs.  Each entry is a name (which becomes /bin/<name> in the myfs
# volume) and the sources that make it: user/header.asm is what gives the image its
# loadable header, so every program links it.  The extension decides the compiler, so
# a program can be assembly, C++, or both.
USER_DIR = ROOT / "user"
USER_BASE = 0x40000000              # matches USER_BASE in kernel32/usercopy.h
USER_IMAGE_HEADER = 16
USER_IMAGE_ARCH = 3                 # matches USER_IMAGE_ARCH in kernel32/user.h
USER_PROGRAMS = (
    ("hello", ("header.asm", "hello.asm")),
    ("badwrite", ("header.asm", "badwrite.asm")),
    ("hellocpp", ("header.asm", "hello.cpp")),
)

BOOT_STAGE1 = "boot16.asm"          # the 512-byte first stage
BOOT_STAGE2 = "stage2.asm"          # the second stage it loads
STAGE2_LBA = 1                      # sectors 1..8 hold the second stage
STAGE2_MAX_SECTORS = 8
# The kernel image lives at LBA 128 on the disk, which is physical 0x10000.  That
# is deliberately far from the loader's own memory footprint (0x0600..0x7C00 for
# the loader and its stack, 0x8000..0xE000 for the kernel) so that the read
# buffer can never overlap the loader.  A real disk image would have a filesystem
# here instead.
KERNEL_LBA = 128
STAGE2_LOAD_OFFSET = 0x0700         # matches STAGE2_LOAD_OFF in boot/boot.inc
KERNEL_LOAD_LIN = 0x8000            # matches KERNEL_LOAD_OFF in boot/boot.inc

KERNEL16_BASE = KERNEL_LOAD_LIN
KERNEL32_BASE = 0x100000
HEADER_SIZE = 16
BOOT_SIGNATURE = b"\x55\xaa"
# Matches MAX_KERNEL_SECTORS in boot/boot.inc: 48 sectors (24 KiB) at 0x8000 ends
# at 0xE000, inside segment 0 and below the video buffer.
MAX_KERNEL_SECTORS = 48
# Matches KERNEL32_STAGE_* in boot/boot.inc: the 32-bit image is staged at
# 0x10000 and the staging window stops at 0x80000, where the extended BIOS data
# area begins.  The kernel stack the loader sets up sits above that window.
KERNEL32_STAGE_LIN = 0x10000
KERNEL32_STAGE_END = 0x80000
KERNEL32_STACK_LIN = 0x200000
MAX_KERNEL32_SECTORS = (KERNEL32_STAGE_END - KERNEL32_STAGE_LIN) // 512
GDT_SEL_CODE = 0x0008


class BuildError(RuntimeError):
    pass


@dataclass
class BuildResult:
    boot: Path
    stage2: Path
    kernel: Path
    floppy: Path
    hard_disk: Path
    kernel_bytes: int
    sectors_read: int
    volume: Path | None = None          # the packed myfs volume, 32-bit images only
    users: dict[str, Path] = field(default_factory=dict)   # /bin/<name> in that volume


def _ensure_dirs() -> None:
    BUILD_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)


def build_stage1(tc: toolchain.Toolchain, out: Path) -> bytes:
    """Assemble boot/boot16.asm and assert it is exactly one valid boot sector."""
    toolchain.assemble(BOOT_DIR / BOOT_STAGE1, out, fmt="bin",
                       include_dirs=[BOOT_DIR], tc=tc)
    data = out.read_bytes()
    if len(data) != 512:
        raise BuildError(
            f"{BOOT_STAGE1} assembled to {len(data)} bytes but a boot sector must be "
            "exactly 512; the `times 510 - ($ - $$) db 0` padding is the constraint "
            "that keeps this file from growing, so move logic into stage2.asm"
        )
    if data[510:512] != BOOT_SIGNATURE:
        raise BuildError(
            f"boot sector signature is {data[510:512].hex()}, expected 55aa; "
            "the `dw 0xAA55` line must be the last thing in the file"
        )
    _verify_stage1_handoff(tc, out)
    return data


def _verify_stage1_handoff(tc: toolchain.Toolchain, image: Path) -> None:
    """Prove the handoff to stage2 is a far jump to 0000:0700.

    `jmp seg:label` in nasm resolves the offset from the start of the output file
    rather than from the org, so it silently assembles to a jump into the
    interrupt vector table.  Checking the emitted bytes here means that trap
    cannot come back without the build failing.
    """
    data = image.read_bytes()
    if data.count(0xEA) == 0:
        raise BuildError(
            "no far jump opcode in the first stage; the handoff to stage2 is missing"
        )
    expected = (bytes([0xEA])
                + STAGE2_LOAD_OFFSET.to_bytes(2, "little")
                + (0x0000).to_bytes(2, "little"))
    if expected not in data:
        found = [f"{i:#x}: {data[i:i + 5].hex()}"
                 for i in range(len(data)) if data[i] == 0xEA]
        raise BuildError(
            f"the handoff to stage2 must be the bytes {expected.hex()} "
            f"(EA {STAGE2_LOAD_OFFSET:04X} 0000) but the sector contains "
            f"{found or 'no EA opcode'}; see the jump note in boot/boot16.asm"
        )
    listing = toolchain.disassemble(image, bits=16, origin=0x7C00, tc=tc)
    for line in listing.splitlines():
        lowered = line.lower()
        if ":word 0x" not in lowered or "jmp" not in lowered:
            continue
        # ndisasm prints `jmp word 0x0:word 0x700`; parse the numbers rather than
        # string-matching, since it does not zero-pad.
        try:
            segment_text, offset_text = lowered.split("jmp word ", 1)[1].split(":word ")
            segment = int(segment_text.strip(), 16)
            offset = int(offset_text.strip().split()[0], 16)
        except (IndexError, ValueError):
            continue
        if segment == 0 and offset == STAGE2_LOAD_OFFSET:
            return
    if listing:
        raise BuildError(
            f"ndisasm does not see a far jump to 0x0000:{STAGE2_LOAD_OFFSET:04X} in "
            "the first stage; the handoff encoding is wrong"
        )


def build_stage2(tc: toolchain.Toolchain, out: Path) -> bytes:
    """Assemble boot/stage2.asm and assert it fits the reserved sectors."""
    toolchain.assemble(BOOT_DIR / BOOT_STAGE2, out, fmt="bin",
                       include_dirs=[BOOT_DIR], tc=tc)
    data = out.read_bytes()
    if not data:
        raise BuildError("stage2 assembled to zero bytes")
    limit = STAGE2_MAX_SECTORS * 512
    if len(data) > limit:
        raise BuildError(
            f"stage2 is {len(data)} bytes but only {limit} bytes "
            f"({STAGE2_MAX_SECTORS} sectors) are reserved for it; either shrink it "
            "or raise STAGE2_MAX_SECTORS here and STAGE2_SECTORS in boot16.asm"
        )
    _verify_pm_handoff(data)
    return data


def _verify_pm_handoff(stage2: bytes) -> None:
    """Prove the loader's jump into protected mode is encoded as intended.

    The protected-mode entry is reached with a hand-written far jump: the 0x66
    prefix (32-bit offset), 0xEA, the offset, and the CS selector.  nasm's
    `jmp seg:label` form resolves the offset from the start of the output file
    rather than from the org -- the trap that put the boot sector's handoff into
    the interrupt vector table -- so the bytes are spelled out and checked here.

    The offset must point *inside* this stage: an offset computed from the file
    start is 0x700 too small and lands outside it, which is exactly the mistake
    this catches.
    """
    expected_selector = GDT_SEL_CODE.to_bytes(2, "little")
    found = []
    for i in range(len(stage2) - 9):
        if stage2[i:i + 2] != b"\x66\xea":
            continue
        offset = int.from_bytes(stage2[i + 2:i + 6], "little")
        selector = stage2[i + 6:i + 8]
        found.append((i, offset, selector))
    if not found:
        raise BuildError(
            "stage2 contains no `66 EA` far jump; the protected-mode entry is missing"
        )
    lo, hi = STAGE2_LOAD_OFFSET, STAGE2_LOAD_OFFSET + len(stage2)
    for _, offset, selector in found:
        if selector != expected_selector:
            raise BuildError(
                f"the protected-mode far jump selects CS {int.from_bytes(selector, 'little'):#06x} "
                f"but the code descriptor is {GDT_SEL_CODE:#06x}"
            )
        if not lo <= offset < hi:
            raise BuildError(
                f"the protected-mode far jump targets {offset:#x}, outside this "
                f"stage's own {lo:#x}..{hi:#x}; the offset was probably resolved "
                "from the start of the file instead of from the org"
            )


def build_kernel16(tc: toolchain.Toolchain, out: Path,
                   verbose: bool = False) -> bytes:
    """Assemble and link the 16-bit kernel, then patch its header checksum.

    The kernel is one assembly unit on purpose: COFF cannot express the 16-bit
    relocations a multi-object real-mode kernel would need for external calls,
    so main.asm %includes the other modules instead of them being linked
    together.  See the header comment in kernel16/main.asm.
    """
    obj = BUILD_DIR / "kernel16.o"
    toolchain.assemble(KERNEL16_DIR / "main.asm", obj, fmt="win32",
                       include_dirs=[BOOT_DIR, KERNEL16_DIR], tc=tc)

    result = cofllink.link([obj], base=KERNEL16_BASE, entry="kernel_main",
                           layout=cofllink.loader_layout(), verbose=verbose)
    if "kernel_main" not in result.symbols:
        raise BuildError("kernel_main was not linked; check the global in main.asm")

    # The entry offset and size are written here rather than in the assembly,
    # because both would need a relocation the assembler cannot express.
    entry_offset = result.symbols["kernel_main"] - KERNEL16_BASE
    kernel = cofllink.patch_image_header(result.image, entry_offset, HEADER_SIZE)
    cofllink.verify_header(kernel, HEADER_SIZE, b"MYOS", arch=1)

    # The loader reads the header from the very first byte of the image, so prove
    # it landed at offset 0 rather than trusting the section layout to do it.
    if "image_header" not in result.symbols:
        raise BuildError("image_header was not linked; check kernel16/main.asm")
    if result.symbols["image_header"] != KERNEL16_BASE:
        raise BuildError(
            f"the image header landed at {result.symbols['image_header']:#x} but the "
            f"boot loader requires it at {KERNEL16_BASE:#x}; the .data section must "
            "be placed first in cofllink.loader_layout()"
        )

    recorded_entry = int.from_bytes(kernel[8:12], "little")
    if KERNEL16_BASE + recorded_entry != result.symbols["kernel_main"]:
        raise BuildError(
            f"header entry offset {recorded_entry:#x} does not resolve to "
            f"kernel_main at {result.symbols['kernel_main']:#x}"
        )
    out.write_bytes(kernel)
    return kernel


def build_user(tc: toolchain.Toolchain, name: str, sources: tuple[str, ...],
               verbose: bool = False) -> bytes:
    """Assemble, link and patch one ring-3 program.

    The image is built the same way the kernel's is -- assemble, link with the in-tree
    linker, patch the 16-byte header -- and differs in exactly three things: the base
    address, the layout (no kernel stack section), and the architecture byte that tells
    the kernel this is something to run in ring 3 rather than a kernel to boot.
    """
    objects = []
    for source in sources:
        path = USER_DIR / source
        if not path.is_file():
            raise BuildError(f"user/{source} is missing (needed by the {name} program)")
        obj = BUILD_DIR / f"user-{name}-{Path(source).stem}.o"
        if path.suffix == ".asm":
            toolchain.assemble(path, obj, fmt="win32", include_dirs=[USER_DIR], tc=tc)
        else:
            toolchain.compile_cpp(path, obj, include_dirs=[USER_DIR], tc=tc)
        objects.append(obj)
    result = cofllink.link(objects, base=USER_BASE, entry="user_entry",
                           layout=cofllink.user_layout(), verbose=verbose)
    entry_address = cofllink.resolve_symbol(result.symbols, "user_entry")
    if entry_address is None:
        raise BuildError(f"{name} does not define user_entry")
    entry_offset = entry_address - USER_BASE
    image = cofllink.patch_image_header(result.image, entry_offset, USER_IMAGE_HEADER)
    cofllink.verify_header(image, USER_IMAGE_HEADER, b"MYOS", arch=USER_IMAGE_ARCH)
    # The kernel refuses an image bigger than USER_IMAGE_MAX, so catch it here where
    # the message can say which program and by how much.
    if len(image) > 256 * 1024:
        raise BuildError(
            f"{name} is {len(image)} bytes, past the 256 KiB a user image may be"
        )
    return image


def build_users(tc: toolchain.Toolchain, verbose: bool = False) -> dict[str, Path]:
    """Builds every ring-3 program and returns where each one landed."""
    built: dict[str, Path] = {}
    for name, sources in USER_PROGRAMS:
        image = build_user(tc, name, sources, verbose=verbose)
        path = BUILD_DIR / "user" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        built[name] = path
    return built


def stage_volume_tree(users: dict[str, Path]) -> Path:
    """Builds the directory tree that goes into the myfs volume.

    `files/` as it is in the source tree, plus `/bin/<name>` for every ring-3 program.
    Assembling it here rather than teaching the packer about several sources keeps
    `tools/myfs.py` a description of the on-disk format and nothing else.
    """
    target = BUILD_DIR / "volume"
    if target.exists():
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
    (target / "bin").mkdir(parents=True, exist_ok=True)
    for source in sorted(FILES_DIR.rglob("*")):
        if source.is_file():
            destination = target / source.relative_to(FILES_DIR)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    for name, image in sorted(users.items()):
        (target / "bin" / name).write_bytes(image.read_bytes())
    return target


def build_kernel32(tc: toolchain.Toolchain, out: Path, verbose: bool = False) -> bytes:
    """Compile and link the 32-bit C++ kernel, then patch its image header.

    The header is the same 16 bytes the 16-bit kernel carries, with arch = 2: the
    loader reads the size to know how much to load, validates the checksum, and
    uses the entry offset to find `kmain` inside the image it just copied to
    0x100000.  None of the three can be written by the assembler -- the entry is a
    linker-symbol difference and the size is only known after linking -- so they
    are filled in here, exactly as for the 16-bit image.
    """
    missing = [name for name in (*KERNEL32_ASM_SOURCES, *KERNEL32_CPP_SOURCES)
               if not (KERNEL32_DIR / name).is_file()]
    if missing:
        raise BuildError(
            "the 32-bit kernel is not written yet; missing sources: "
            + ", ".join(missing)
        )
    objects = []
    for source in KERNEL32_ASM_SOURCES:
        obj = BUILD_DIR / (Path(source).stem + "32.o")
        toolchain.assemble(KERNEL32_DIR / source, obj, fmt="win32",
                           include_dirs=[BOOT_DIR], tc=tc)
        objects.append(obj)
    for source in KERNEL32_CPP_SOURCES:
        obj = BUILD_DIR / (Path(source).stem + "32.o")
        toolchain.compile_cpp(KERNEL32_DIR / source, obj,
                              include_dirs=[KERNEL32_DIR], tc=tc)
        objects.append(obj)
    # The object name comes from the file's stem, so two sources called `user.asm`
    # and `user.cpp` would share one .o -- and the same object linked twice does not
    # fail as a duplicate *file*: it links, into two different places, and the error
    # that finally appears is "this static function is defined more than once at
    # different addresses", which points at the function and not at the file names.
    if len(set(objects)) != len(objects):
        duplicates = sorted({path.name for path in objects
                             if [p.name for p in objects].count(path.name) > 1})
        raise BuildError(
            "two kernel sources share one object name: " + ", ".join(duplicates)
            + "; rename one of them so the stems differ"
        )
    result = cofllink.link(objects, base=KERNEL32_BASE, entry="kernel_entry",
                           layout=cofllink.kernel_layout(), verbose=verbose)
    entry_address = cofllink.resolve_symbol(result.symbols, "kernel_entry")
    if entry_address is None:
        raise BuildError("kernel_entry was not linked; check kernel32/boot.asm")
    if cofllink.resolve_symbol(result.symbols, "kmain") is None:
        raise BuildError("kmain was not linked; check kernel32/kernel.cpp")

    entry_offset = entry_address - KERNEL32_BASE
    kernel = cofllink.patch_image_header(result.image, entry_offset, HEADER_SIZE)
    cofllink.verify_header(kernel, HEADER_SIZE, b"MYOS", arch=2)
    _verify_stub_table(kernel, result.symbols)

    # The loader copies the image with rep movsb and then jumps to
    # KERNEL32_LOAD_LIN + entry, so the entry has to be inside the copy and the
    # whole image has to fit below the bootstrap stack.
    if entry_offset >= len(kernel):
        raise BuildError(
            f"kmain is at image offset {entry_offset:#x}, past the end of the "
            f"{len(kernel)}-byte image"
        )
    loaded_end = KERNEL32_BASE + len(kernel)
    if loaded_end + 0x10000 > KERNEL32_STACK_LIN:
        raise BuildError(
            f"the kernel ends at {loaded_end:#x}, too close to the loader's "
            f"bootstrap stack at {KERNEL32_STACK_LIN:#x}; shrink the kernel or move "
            "KERNEL32_STACK_LIN in boot.inc and build.py"
        )
    out.write_bytes(kernel)
    return kernel


def _verify_stub_table(image: bytes, symbols: dict) -> None:
    """Prove every IDT stub address the kernel will install points into the kernel.

    The table is written in assembly as `dd isr %+ index`, and a preprocessor
    variable named like the label prefix turns those into plain numbers: the
    entries become raw section offsets with no relocation, the image links and
    boots, and the first interrupt jumps into low memory.  Reading the finished
    image is the only place that mistake is visible before it happens.
    """
    address = cofllink.resolve_symbol(symbols, "isr_stub_table")
    if address is None:
        raise BuildError("isr_stub_table was not linked; check kernel32/isr.asm")
    limit = KERNEL32_BASE + len(image)
    count = 32 + 16                              # exceptions + IRQs
    start = address - KERNEL32_BASE
    entries = [int.from_bytes(image[start + i * 4:start + i * 4 + 4], "little")
               for i in range(count)]
    bad = {i: value for i, value in enumerate(entries)
           if not KERNEL32_BASE <= value < limit}
    if bad:
        raise BuildError(
            "these interrupt stubs point outside the kernel image (vector: target): "
            + ", ".join(f"{i}: {value:#x}" for i, value in sorted(bad.items()))
            + "; the stub table in kernel32/isr.asm is probably holding offsets "
              "rather than relocations"
        )


def pack_images(stage1: bytes, stage2: bytes, kernel: bytes,
                arch: int = 16,
                volume_source: Path | None = None) -> tuple[Path, Path, Path | None]:
    """Write the floppy and MBR-partitioned hard disk images.

    Both media share one layout:
        LBA 0        first-stage boot sector (also the MBR of the disk image)
        LBA 1..8     second stage
        LBA 128..    kernel image

    The kernel's LBA is not adjacent to stage2 on purpose.  The loader reads the
    16-bit kernel to physical 0x8000, which is LBA 64, so putting the kernel at
    LBA 9 would have made the disk read overwrite the loader's own source data.
    LBA 128 (physical 0x10000) is clear of both the loader (0x700..0x7C00) and
    the kernel's destination (0x8000..0xE000).

    The 32-bit hard disk goes further: it is bigger, and it declares a second
    partition for the myfs volume.  The boot container is then shortened so the
    two partitions do not overlap, because the kernel finds the volume by walking
    the partition table and would otherwise be told the volume starts inside the
    region holding the loader and the kernel.

    The hard disk image also carries the first-stage sector in the active
    partition as its volume boot record, at partition LBA 1, because that is
    where this loader expects to be executed from.
    """
    if len(stage2) > STAGE2_MAX_SECTORS * image.SECTOR:
        raise BuildError(
            f"stage2 is {len(stage2)} bytes but only "
            f"{STAGE2_MAX_SECTORS * image.SECTOR} bytes are reserved for it"
        )
    kernel_lba_bytes = KERNEL_LBA * image.SECTOR

    floppy = image.build_floppy_image(stage1)
    floppy[STAGE2_LBA * image.SECTOR:
           STAGE2_LBA * image.SECTOR + len(stage2)] = stage2
    end = kernel_lba_bytes + len(kernel)
    if end > len(floppy):
        raise BuildError(
            f"the kernel at LBA {KERNEL_LBA} ends at byte {end}, past the "
            f"{len(floppy)}-byte floppy image"
        )
    floppy[kernel_lba_bytes:end] = kernel
    floppy_path = IMAGES_DIR / f"myos{arch}.img"
    floppy_path.write_bytes(floppy)

    volume_path: Path | None = None
    if arch == 32:
        if myfs.BLOCK_SIZE != image.SECTOR:
            raise BuildError(
                f"myfs blocks are {myfs.BLOCK_SIZE} bytes but disk sectors are "
                f"{image.SECTOR}; the volume is placed by LBA and the loader reads "
                "by sector, so the two have to match"
            )
        if myfs.PARTITION_LBA < KERNEL_LBA + MAX_KERNEL32_SECTORS:
            raise BuildError(
                f"the myfs partition starts at LBA {myfs.PARTITION_LBA}, inside the "
                f"kernel's reserved run (LBA {KERNEL_LBA}.."
                f"{KERNEL_LBA + MAX_KERNEL32_SECTORS - 1}); move the partition or "
                "shrink the kernel budget in boot/boot.inc"
            )
        source = volume_source if volume_source is not None else FILES_DIR
        volume = myfs.build_volume(source)
        volume_path = IMAGES_DIR / "myfs-volume.img"
        volume_path.write_bytes(volume.to_bytes())

        geometry = image.hard_disk_geometry(myfs.DISK_SECTORS)
        hard = image.build_hard_disk_image(stage1, partition_lba=1, geometry=geometry)
        # Carve the disk in two: the boot container keeps everything up to the
        # volume, and the volume gets a partition of its own, of the type the
        # kernel looks for.
        image.write_partition_entry(
            hard, 0, lba_start=1, sectors=myfs.PARTITION_LBA - 1,
            part_type=image.PARTITION_TYPE_FAT32_CHS, geometry=geometry, bootable=True,
        )
        image.write_partition_entry(
            hard, myfs.PARTITION_INDEX, lba_start=myfs.PARTITION_LBA,
            sectors=myfs.PARTITION_SECTORS, part_type=myfs.PARTITION_TYPE,
            geometry=geometry,
        )
        start = myfs.PARTITION_LBA * image.SECTOR
        hard[start:start + myfs.VOLUME_BYTES] = volume.to_bytes()
    else:
        geometry = image.hard_disk_geometry(len(floppy) // image.SECTOR)
        hard = image.build_hard_disk_image(stage1, partition_lba=1, geometry=geometry)

    hard[STAGE2_LBA * image.SECTOR:
         STAGE2_LBA * image.SECTOR + len(stage2)] = stage2
    hard[kernel_lba_bytes:kernel_lba_bytes + len(kernel)] = kernel
    hard_path = IMAGES_DIR / f"myos{arch}-hd.img"
    hard_path.write_bytes(hard)
    return floppy_path, hard_path, volume_path


def bundled_entries(users: dict[str, Path]) -> list[tuple[str, bytes]]:
    """Everything a build ships, as (path inside the volume, bytes).

    `files/` plus the ring-3 programs.  This is the list a data disk is *updated*
    with: naming what the build owns is what makes it possible to add to the user's
    disk without guessing which of its files are theirs.
    """
    entries: list[tuple[str, bytes]] = []
    for source in sorted(FILES_DIR.rglob("*")):
        if source.is_file():
            relative = source.relative_to(FILES_DIR).as_posix()
            entries.append((f"/{relative}", source.read_bytes()))
    for name, image in sorted(users.items()):
        entries.append((f"/bin/{name}", image.read_bytes()))
    return entries


def update_data_disk(tc: toolchain.Toolchain) -> int:
    """Add the files a build ships into the user's own disk, leaving the rest alone.

    The data disk is never rebuilt -- that is the whole reason the user's files
    survive a build -- so when a build starts shipping something new (the ring-3
    programs, for instance) an older disk simply does not have it, and `run /bin/hello`
    answers "no such file or directory".  This merges: every name the build owns is
    created or replaced, and nothing else on the disk is touched.
    """
    users = build_users(tc)
    if not DATA_DISK.exists():
        volume = myfs.write_data_disk(DATA_DISK, files_dir=FILES_DIR)
        print(f"wrote {DATA_DISK} ({DATA_DISK.stat().st_size} bytes, myfs in "
              f"partition {myfs.PARTITION_INDEX} at LBA {myfs.PARTITION_LBA}, "
              f"{volume.free_blocks} free blocks)")
        return 0

    volume = myfs.volume_from_image(DATA_DISK, myfs.PARTITION_INDEX)
    before = {path for path, _ in volume.iter_files()}
    for path, data in bundled_entries(users):
        volume.put_file(path, data)
    DATA_DISK.write_bytes(myfs.build_disk_image(volume))
    after = {path for path, _ in volume.iter_files()}
    print(f"updated {DATA_DISK}: {len(before & after)} entry/entries kept, "
          f"{len(after - before)} added ({', '.join(sorted(after - before)) or 'none'}), "
          f"{volume.free_blocks} free blocks")
    print("(if QEMU has this disk open, close it before booting again)")
    return 0


def build(arch: int = 16, verbose: bool = False) -> BuildResult:
    _ensure_dirs()
    tc = toolchain.discover()
    if tc.missing_required:
        raise BuildError(tc.report())

    stage1_out = BUILD_DIR / "boot16.bin"
    stage2_out = BUILD_DIR / "stage2.bin"
    stage1 = build_stage1(tc, stage1_out)
    stage2 = build_stage2(tc, stage2_out)

    users: dict[str, Path] = {}
    volume_source: Path | None = None
    if arch == 16:
        kernel = build_kernel16(tc, BUILD_DIR / "kernel16.bin", verbose=verbose)
        budget = MAX_KERNEL_SECTORS
    else:
        kernel = build_kernel32(tc, BUILD_DIR / "kernel32.bin", verbose=verbose)
        budget = MAX_KERNEL32_SECTORS
        # The ring-3 programs are built and packed next to the files the source tree
        # ships: same volume, same manifest, same check on every boot.
        users = build_users(tc, verbose=verbose)
        volume_source = stage_volume_tree(users)

    if len(kernel) > budget * image.SECTOR:
        where = ("MAX_KERNEL_SECTORS in both build.py and boot/boot.inc"
                 if arch == 16 else
                 "MAX_KERNEL32_SECTORS/KERNEL32_STAGE_END in both build.py and boot/boot.inc")
        raise BuildError(
            f"kernel is {len(kernel)} bytes ({image.kernel_sectors(kernel)} sectors), "
            f"which exceeds the loader's {budget}-sector budget; raise {where}, "
            "or shrink the kernel"
        )

    floppy, hard, volume = pack_images(stage1, stage2, kernel, arch, volume_source)
    return BuildResult(boot=stage1_out, stage2=stage2_out,
                       kernel=BUILD_DIR / f"kernel{arch}.bin", floppy=floppy,
                       hard_disk=hard, kernel_bytes=len(kernel),
                       sectors_read=image.kernel_sectors(kernel), volume=volume,
                       users=users)


def doctor() -> int:
    tc = toolchain.discover()
    print(tc.report())
    return 1 if tc.missing_required else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build myos disk images")
    parser.add_argument("command", nargs="?", default="all",
                        choices=("all", "boot", "kernel16", "kernel32", "images",
                                 "data-disk", "doctor", "clean"))
    parser.add_argument("--arch", type=int, default=16, choices=(16, 32))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--update", action="store_true",
                        help="with `data-disk`: merge the files a build ships "
                             "(files/ and /bin programs) into the existing disk, "
                             "leaving everything else on it alone")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return doctor()
    if args.command == "data-disk":
        # The user's own disk, created once and then never rebuilt: `run.py` boots
        # the floppy and hangs this off the ATA controller, so what the kernel
        # writes here survives both a reboot and a `build.py all`.  `--update` is the
        # one way build output reaches it, and it names exactly what it replaces.
        if args.update:
            return update_data_disk(toolchain.discover())
        if DATA_DISK.exists():
            volume = myfs.volume_from_image(DATA_DISK, myfs.PARTITION_INDEX)
            print(f"kept {DATA_DISK} (already exists, "
                  f"{volume.free_blocks} free blocks)")
            print("(use `build.py data-disk --update` to merge the files a build "
                  "ships into it, such as /bin programs)")
            return 0
        volume = myfs.write_data_disk(DATA_DISK, files_dir=FILES_DIR)
        print(f"wrote {DATA_DISK} ({DATA_DISK.stat().st_size} bytes, myfs in "
              f"partition {myfs.PARTITION_INDEX} at LBA {myfs.PARTITION_LBA}, "
              f"{volume.free_blocks} free blocks)")
        return 0
    if args.command == "clean":
        for path in list(BUILD_DIR.glob("*")) + list(IMAGES_DIR.glob("*")):
            if path.is_file():
                path.unlink()
        print("cleaned build/ and images/")
        return 0

    try:
        result = build(arch=args.arch, verbose=args.verbose)
    except (BuildError, cofllink.LinkError, myfs.MyfsError,
            toolchain.ToolFailed) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"boot sector : {result.boot} (512 bytes, signature ok)")
    print(f"second stage: {result.stage2} ({result.stage2.stat().st_size} bytes)")
    print(f"kernel      : {result.kernel} ({result.kernel_bytes} bytes, "
          f"{result.sectors_read} sectors)")
    print(f"floppy image: {result.floppy} ({result.floppy.stat().st_size} bytes)")
    print(f"disk image  : {result.hard_disk} ({result.hard_disk.stat().st_size} bytes)")
    if result.volume is not None:
        packed = len(list(myfs.Volume(result.volume.read_bytes()).iter_files()))
        programs = ", ".join(sorted(result.users)) if result.users else "none"
        print(f"volume      : {result.volume} "
              f"({result.volume.stat().st_size} bytes, {packed} entries: {FILES_DIR} "
              f"plus /bin programs [{programs}])")
    return 0


if __name__ == "__main__":
    sys.exit(main())
