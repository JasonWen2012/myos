"""
A small COFF/PE32 linker for bare-metal i386 images.

Why this exists
---------------
The toolchain available here cannot produce a bare-metal image: the UCRT64 and
MSYS2 ``ld`` both support only ``i386pe``/``i386pep``, and every attempt to get a
flat binary out of them fails with

    cannot perform PE operations on non PE output file

``objcopy`` will not rescue it either, because there is no intermediate format it
can consume that ``ld`` is willing to emit.  Since ``g++ -m32`` happily produces
valid PE32 relocatable objects (verifiable with ``objdump -dr``), the shortest
honest path is to link them here.

Scope
-----
This is deliberately a *small* linker, not a GNU ld replacement:

  * inputs are PE32 i386 relocatable objects (``IMAGE_FILE_MACHINE_I386``),
  * output is a flat binary at a caller-chosen base address,
  * the only relocations a freestanding i386 kernel needs are supported, and an
    unknown relocation type is a hard error that names the section, offset and
    type number -- never a silently wrong image,
  * unresolved symbols are reported with the objects that reference them.

Layout
------
``LayoutSpec`` holds one ``SectionPlacement`` per output region.  Each placement
names the input section names it accepts (prefix or exact match) and its output
order.  This is what lets kernel16 place its 16-byte image header at offset 0:
the header is assembled as its own input section, and that section is placed
first.

PE detour
---------
The target defines ``IMAGE_SCN_LNK_COMDAT`` on its sections, which would
normally require COMDAT selection handling.  A freestanding kernel is a single
translation unit per object with no inline-function duplication, so this linker
treats every section as ordinary content and rejects duplicate definitions of a
non-weak symbol instead.

External symbols in this toolchain are emitted with a leading underscore
(``_kput`` for ``kput``); both spellings are therefore indexed as aliases so that
an ``extern "C" void kput(...)`` and a NASM ``global kput`` link together.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

SECTOR = 512

# COFF constants
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_SCN_CNT_CODE = 0x00000020
IMAGE_SCN_CNT_INITIALIZED_DATA = 0x00000040
IMAGE_SCN_CNT_UNINITIALIZED_DATA = 0x00000080
IMAGE_SCN_LNK_COMDAT = 0x00001000

SYM_CLASS_EXTERNAL = 2
SYM_CLASS_STATIC = 3
SYM_CLASS_WEAK_EXTERNAL = 105

# PE32 relocation types (the full documented set; only a couple occur in
# practice, but an unexpected one must be named precisely rather than guessed).
RELOCATION_NAMES = {
    0x0000: "ABSOLUTE",
    0x0001: "ADDR32",
    0x0002: "ADDR32NB",
    0x0003: "SEG12",
    0x0004: "SECTION",
    0x0005: "SECREL",
    0x0006: "DIR32",
    0x0007: "DIR32NB",
    0x0009: "SEG12",
    0x000A: "SECTION",
    0x000B: "SECREL",
    0x000C: "TOKEN",
    0x000D: "SECREL7",
    0x000E: "REL32",
    0x0014: "REL32",
}

# Relocations this linker can apply.  Each entry is (name, needs_addend, relative).
SUPPORTED_RELOCATIONS = {
    0x0001: ("ADDR32", True, False),
    0x0002: ("ADDR32NB", True, False),
    0x0006: ("DIR32", True, False),
    0x0007: ("DIR32NB", True, False),
    0x0014: ("REL32", True, True),
}

SYMBOL_SIZE = 18
SECTION_SIZE = 40
SECTION_HEADER_OFFSET = 20

# COFF symbol record field offsets.  They are listed explicitly because the
# natural-looking struct format "<IihHBB" is wrong: it puts the two bytes after
# the value where the storage class lives, so `naux` is read from the wrong
# place.  For a 0x01 there the walk skips a multiple of 18 records and every
# later symbol is misparsed -- section numbers come out as 97, 100, ... and every
# definition looks undefined.
SYM_NAME = 0
SYM_VALUE = 8
SYM_SECTION = 12
SYM_TYPE = 14
SYM_CLASS = 16
SYM_AUX = 17


def _read_symbol(data: bytes, off: int) -> tuple[str, int, int, int, int, int]:
    """Decode one 18-byte COFF symbol record.

    Layout, with an 8-byte name field first:
        0..7   name: 4 raw bytes plus 4 bytes of padding when the first four are
               a short inline name, otherwise four zero bytes followed by a
               4-byte offset into the string table
        8..11  value
        12..13 section number (0 = undefined, -1 = absolute)
        14..15 type
        16     storage class
        17     auxiliary record count
    """
    name_raw = data[off:off + 8]
    if name_raw[0:4] != b"\x00\x00\x00\x00":
        end = name_raw.find(b"\0")
        name = name_raw[:end if end >= 0 else 8].decode("ascii", "replace")
        name_offset = 0
    else:
        name = ""
        name_offset = struct.unpack_from("<I", name_raw, 4)[0]
    value = struct.unpack_from("<I", data, off + SYM_VALUE)[0]
    section = struct.unpack_from("<h", data, off + SYM_SECTION)[0]
    sym_type = struct.unpack_from("<H", data, off + SYM_TYPE)[0]
    storage_class = data[off + SYM_CLASS]
    naux = data[off + SYM_AUX]
    del sym_type
    return name, name_offset, value, section, storage_class, naux


class LinkError(RuntimeError):
    """Raised for any condition that would produce an unusable image."""


@dataclass
class InputSection:
    name: str
    raw: bytes
    vaddr: int = 0
    relocations: list = field(default_factory=list)   # (offset, symbol_index, type)
    characteristics: int = 0
    source: str = ""
    discard: bool = False         # matched a discard region: contents are dropped
    size: int = 0                 # bytes it occupies, raw or reserved

    def __post_init__(self) -> None:
        if not self.size:
            self.size = len(self.raw)

    @property
    def is_bss(self) -> bool:
        return bool(self.characteristics & IMAGE_SCN_CNT_UNINITIALIZED_DATA)


@dataclass
class Symbol:
    name: str
    value: int
    section_number: int           # 1-based, 0 = undefined, -1 = absolute
    storage_class: int
    type: int
    source: str = ""

    @property
    def is_defined(self) -> bool:
        return self.section_number != 0

    @property
    def is_absolute(self) -> bool:
        return self.section_number == -1


@dataclass
class ObjectFile:
    path: Path
    sections: list[InputSection]
    symbols: list[Symbol]
    string_table: bytes = b""
    # Relocations index the symbol table by *physical COFF entry number*, which
    # includes auxiliary records.  Aux records are not symbols, so keeping the
    # logical list and looking a relocation up in it silently resolves every
    # reference to whichever symbol happens to sit at that logical position --
    # which is how a `mov esi, console_data` ended up loading kernel_main's
    # address.  This map is the one relocations must use.
    symbols_by_index: dict[int, Symbol] = field(default_factory=dict)

    def symbol_at(self, entry_index: int) -> Optional[Symbol]:
        return self.symbols_by_index.get(entry_index)


@dataclass
class SectionPlacement:
    """One output region: which input sections belong to it, and in what order."""

    output_name: str
    match: tuple[str, ...]
    order: int
    align: int = 16
    writable: bool = False
    zero_fill: bool = False       # .bss style: occupies space, emits no bytes
    discard: bool = False         # accept the section and then ignore its contents

    def accepts(self, section_name: str) -> bool:
        for pattern in self.match:
            if pattern.endswith("*"):
                if section_name.startswith(pattern[:-1]):
                    return True
            elif section_name == pattern:
                return True
        return False


# ------------------------------------------------------------- default layouts

def kernel_layout() -> list[SectionPlacement]:
    """Standard freestanding kernel layout: header, code, rodata, data, bss.

    The header region comes first because the loader reads the image header from
    offset 0 of the image; it is its own section (`.myos_header`, written in
    kernel32/boot.asm) so that the order is stated here rather than depending on
    which section the compiler happens to emit first.

    The patterns are prefixes because this target's compiler puts things in
    sub-sections: `.rdata$zzz` (its linker version marker, which every object
    carries) and `.text$<name>` (COMDAT, one per out-of-line inline function) both
    have to land somewhere.

    Sections that would be *wrong* to place are listed as discarded rather than
    given a region.  Debug information has no meaning in a flat image, and unwind
    tables are dead weight for a kernel compiled without exceptions and without
    asynchronous unwinding -- placing them would silently grow the image and the
    loader's copy.  Discarding is explicit and printed with --verbose; a section
    that matches nothing at all is still a hard error.
    """
    return [
        SectionPlacement("header", (".myos_header",), order=-1, align=16),
        SectionPlacement("code", (".text*",), order=0, align=16, writable=False),
        SectionPlacement("rodata", (".rdata*", ".rodata*"), order=1, align=4),
        SectionPlacement("data", (".data*",), order=2, align=4, writable=True),
        SectionPlacement("bss", (".bss", ".bss$*"), order=3, align=4, writable=True,
                         zero_fill=True),
        # The stack goes last, after every other .bss object, because it grows down
        # from its top: anything the kernel keeps above the stack top is safe, and
        # anything inside the stack's range gets overwritten by the next function
        # call.  With the reverse order the kernel's own static variables sat in the
        # stack's shadow and were corrupted by ordinary calls -- which looked like a
        # miscompiled kprintf rather than a layout mistake.
        SectionPlacement("stack", (".bss.stack",), order=4, align=16, writable=True,
                         zero_fill=True),
        SectionPlacement("discard", (".debug*", ".eh_frame*", ".xdata*", ".pdata*",
                                     ".drectve*", ".CRT*", ".gfids*", ".giats*"),
                         order=99, discard=True),
    ]


def loader_layout() -> list[SectionPlacement]:
    """Layout for the 16-bit kernel.

    The image header lives at offset 0 of the .data section (see
    kernel16/main.asm), so .data has to be placed first.  Putting the header at
    offset 0 through section order rather than as its own section means nasm
    never has to compute a difference across sections, which would need a 16-bit
    relocation COFF cannot express.
    """
    return [
        SectionPlacement("data", (".data",), order=0, align=4, writable=True),
        SectionPlacement("code", (".text",), order=1, align=16),
        SectionPlacement("rodata", (".rdata",), order=2, align=2),
        SectionPlacement("bss", (".bss",), order=3, align=4, writable=True, zero_fill=True),
    ]


# ------------------------------------------------------------------- parsing

def read_object(path: Path | str) -> ObjectFile:
    """Parse a PE32 i386 relocatable object into sections and symbols."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LinkError(f"cannot read object {path}: {exc}") from exc
    if len(data) < SECTION_HEADER_OFFSET:
        raise LinkError(f"{path.name} is too small to be a COFF object")
    if data[:2] == b"MZ":
        raise LinkError(
            f"{path.name} looks like an executable/import library, not a "
            "relocatable object; compile with -c"
        )

    machine, nsec, _ts, symptr, nsym, opthdr, _chars = struct.unpack_from("<HHIIIHH", data, 0)
    if machine != IMAGE_FILE_MACHINE_I386:
        raise LinkError(
            f"{path.name} has machine type {machine:#06x}, expected "
            f"{IMAGE_FILE_MACHINE_I386:#06x} (i386); compile with -m32"
        )
    if opthdr != 0:
        raise LinkError(f"{path.name} has an optional header; expected a relocatable object")
    if nsec == 0:
        raise LinkError(f"{path.name} declares no sections")

    # The string table starts right after the symbol table, but COFF's nsym
    # excludes auxiliary entries, so the real end has to be found by walking the
    # symbols and counting their aux records.
    sym_end = _symbol_table_end(data, symptr, nsym, path)
    string_table = data[sym_end:]
    strtab_size = struct.unpack_from("<I", string_table, 0)[0] if len(string_table) >= 4 else 4

    symbols: list[Symbol] = []
    symbols_by_index: dict[int, Symbol] = {}
    entry_index = 0
    while entry_index < nsym:
        off = symptr + entry_index * SYMBOL_SIZE
        if off + SYMBOL_SIZE > len(data):
            raise LinkError(
                f"{path.name}: symbol {entry_index} at {off:#x} runs past the end "
                f"of the {len(data)}-byte file"
            )
        name, name_offset, value, section, storage_class, naux = _read_symbol(data, off)
        if not name:
            name = _string_table_lookup(data, sym_end, name_offset)
        symbol = Symbol(name=name, value=value, section_number=section,
                        storage_class=storage_class, type=0, source=path.name)
        symbols.append(symbol)
        symbols_by_index[entry_index] = symbol
        entry_index += 1 + naux             # step over auxiliary records
    if entry_index != nsym:
        raise LinkError(
            f"{path.name}: symbol table ends at {entry_index} but the header declares "
            f"{nsym} entries"
        )

    sections: list[InputSection] = []
    raw = _section_raw_names(data, nsec, opthdr, string_table, sym_end)
    for i in range(nsec):
        off = SECTION_HEADER_OFFSET + opthdr + i * SECTION_SIZE
        (name_bytes, _vsize, _vaddr, raw_size, raw_ptr, rel_ptr, _line_ptr,
         nrel, _nline, chars) = struct.unpack_from("<8sIIIIIIHHI", data, off)
        name = raw[i]
        # An uninitialised section carries no file contents: its SizeOfRawData is
        # the number of bytes to *reserve* and its PointerToRawData is 0.  Slicing
        # the file from offset 0 for it copies the COFF header itself into what is
        # supposed to be zero-filled BSS -- which is how the kernel's static
        # counters started life holding 0x0005014C, i.e. the i386 machine type
        # 0x014C followed by the section count 5.
        is_bss = bool(chars & IMAGE_SCN_CNT_UNINITIALIZED_DATA)
        if is_bss or not raw_size or raw_ptr == 0:
            body = b""
        else:
            body = bytes(data[raw_ptr:raw_ptr + raw_size])
        relocations = []
        for j in range(nrel):
            va, symidx, reltype = struct.unpack_from("<IIH", data, rel_ptr + j * 10)
            relocations.append((va, symidx, reltype))
        sections.append(InputSection(name=name, raw=body, relocations=relocations,
                                     characteristics=chars, source=path.name,
                                     size=raw_size))
    return ObjectFile(path=path, sections=sections, symbols=symbols,
                      string_table=string_table[:strtab_size],
                      symbols_by_index=symbols_by_index)


def _string_table_lookup(data: bytes, table_offset: int, offset: int) -> str:
    """Read a NUL-terminated name from the COFF string table."""
    if offset < 4:
        return ""
    start = table_offset + offset
    if start >= len(data):
        return ""
    end = data.find(b"\0", start)
    if end < 0:
        end = min(len(data), start + 128)
    return data[start:end].decode("ascii", "replace")


def _symbol_name(data: bytes, entry_offset: int, name_field: int,
                 string_table: bytes, table_offset: int) -> str:
    """Decode a COFF symbol name.

    The 8-byte name field is either
      * an inline name when its first four bytes are non-zero -- nasm emits this
        for short names such as `.file` and `.text`, and the name ends at the
        first NUL, or
      * zeroes followed by a 4-byte offset into the string table.

    Treating the inline form as an offset is the mistake that makes a perfectly
    good object look corrupt: `.fil` reads as 0x6c69662e, a wildly out-of-range
    string offset.
    """
    first_four = data[entry_offset:entry_offset + 4]
    if first_four != b"\x00\x00\x00\x00":
        raw = data[entry_offset:entry_offset + 8]
        end = raw.find(b"\0")
        if end < 0:
            end = 8
        return raw[:end].decode("ascii", "replace")
    offset = struct.unpack_from("<I", data, entry_offset + 4)[0]
    return _string_table_lookup(data, table_offset, offset)


def _symbol_table_end(data: bytes, symptr: int, nsym: int, path: Path) -> int:
    """Offset just past the last symbol, counting auxiliary records."""
    raw_index = 0
    while raw_index < nsym:
        off = symptr + raw_index * SYMBOL_SIZE
        if off + SYMBOL_SIZE > len(data):
            raise LinkError(f"{path.name}: symbol table runs past the end of the file")
        naux = data[off + 17]
        raw_index += 1 + naux
    if raw_index != nsym:
        raise LinkError(
            f"{path.name}: symbol table has {raw_index} entries (with aux records) "
            f"but the header declares {nsym}"
        )
    return symptr + nsym * SYMBOL_SIZE


def _section_raw_names(data: bytes, nsec: int, opthdr: int, string_table: bytes,
                       string_table_off: int) -> list[str]:
    names = []
    for i in range(nsec):
        off = SECTION_HEADER_OFFSET + opthdr + i * SECTION_SIZE
        name_bytes = data[off:off + 8]
        name = name_bytes.rstrip(b"\0").decode("ascii", "replace")
        if name.startswith("/") and name[1:].isdigit() and string_table_off:
            str_off = int(name[1:])
            start = string_table_off + str_off
            end = data.find(b"\0", start)
            if end < 0:
                end = min(len(data), start + 64)
            name = data[start:end].decode("ascii", "replace")
        names.append(name)
    return names


# ------------------------------------------------------------------- linking

@dataclass
class LinkResult:
    image: bytes
    symbols: dict[str, int]
    section_bounds: dict[str, tuple[int, int]]
    base: int
    entry: int
    size: int


def resolve_symbol(symbols: dict[str, int], name: str) -> Optional[int]:
    """Look a symbol up under both the bare and underscore-prefixed spellings.

    Two toolchains feed this linker and they disagree about names: nasm's `global
    kernel_main` produces `kernel_main`, while this target's g++ prefixes extern
    "C" symbols with an underscore (`_kmain`).  Anything that *names* a symbol --
    the entry point, a relocation against an external, build.py checking that the
    entry it just linked exists -- has to accept either spelling.
    """
    bare = name.lstrip("_")
    for candidate in (bare, "_" + bare, name):
        if candidate in symbols:
            return symbols[candidate]
    return None


def _is_section_symbol(sym: Symbol, section: InputSection) -> bool:
    """True for the section symbol every object carries.

    It is named after its own section, sits at offset 0, and is STATIC with type
    0.  Two things follow from recognising it:

      * it must not be entered in the symbol table.  Several objects contributing
        to one output region each carry a `.text` section symbol, and with the
        sections at different addresses that looks exactly like one symbol defined
        twice -- which the linker rightly refuses.
      * a relocation against it is legitimate and common: a jump table emitted into
        .rdata is addressed relative to the section, not to a named label.  Those
        must resolve to the section's base address, with the addend already in the
        image at the relocation site.

    The name comparison allows for COFF's 8-byte inline names: a section called
    `.bss.stack` has its symbol spelled `.bss.sta` in the field, so an exact match
    would miss it and let the section symbol into the symbol table.
    """
    if not (sym.value == 0 and sym.storage_class == SYM_CLASS_STATIC and sym.type == 0):
        return False
    if sym.name == section.name:
        return True
    return (len(sym.name) == 8 and section.name.startswith(sym.name))


def link(objects: Sequence[Path | str], base: int = 0x100000, entry: Optional[str] = None,
         layout: Optional[Sequence[SectionPlacement]] = None,
         verbose: bool = False) -> LinkResult:
    """Link PE32 objects into a flat binary at `base`."""
    if not objects:
        raise LinkError("no input objects")
    parsed = [read_object(o) for o in objects]
    placements = list(layout) if layout is not None else kernel_layout()

    # ---- 1. assign every input section to exactly one placement
    assigned: dict[int, SectionPlacement] = {}
    for obj_index, obj in enumerate(parsed):
        for sec in obj.sections:
            if not sec.raw and not sec.is_bss and not sec.relocations:
                continue                       # empty padding section
            for placement in placements:
                if placement.accepts(sec.name):
                    assigned[id(sec)] = placement
                    sec.discard = placement.discard
                    break
            else:
                raise LinkError(
                    f"{obj.path.name}: section {sec.name!r} matches no output "
                    f"region; known regions: "
                    f"{', '.join(p.output_name + '=' + '/'.join(p.match) for p in placements)}"
                )

    discarded = [(obj.path.name, sec.name) for obj in parsed for sec in obj.sections
                 if sec.discard]
    if verbose and discarded:
        for source, name in discarded:
            print(f"[link] discarding {name} from {source}")

    # ---- 2. lay out regions
    #
    # A section's size, not len(raw): an uninitialised section (.bss) carries a
    # SizeOfRawData and no file contents at all, so measuring it by its bytes gave
    # every .bss section a size of zero.  Two objects with .bss then landed on the
    # same address -- the kernel's stack buffer and its counters overlapped, and
    # the counters came back as stack garbage.
    ordered = sorted(placements, key=lambda p: p.order)
    cursor = base
    region_offsets: dict[str, int] = {}
    for placement in ordered:
        if placement.discard:
            continue
        cursor = _align_up(cursor, placement.align)
        region_offsets[placement.output_name] = cursor
        region_size = 0
        contributed = False
        for obj in parsed:
            for sec in obj.sections:
                if assigned.get(id(sec)) is placement:
                    contributed = True
                    region_size = _align_up(region_size, 4)
                    sec.vaddr = cursor + region_size
                    region_size += sec.size
        if placement.zero_fill:
            region_size = _align_up(region_size, 4)
        cursor += region_size
    total_size = cursor - base

    # ---- 3. build the symbol table
    symbols: dict[str, int] = {}
    for obj in parsed:
        for sym in obj.symbols:
            name = sym.name
            if not name:
                continue
            if sym.is_absolute:
                value = sym.value
            elif 0 < sym.section_number <= len(obj.sections):
                section = obj.sections[sym.section_number - 1]
                if section.discard or _is_section_symbol(sym, section):
                    continue                   # a dropped section, or the section itself
                value = section.vaddr + sym.value
            else:
                # section_number 0 means undefined; anything larger than the
                # section count is not a definition either (nasm emits a few
                # symbols whose section field does not refer to a section), so
                # skip rather than indexing out of range.
                continue
            _define(symbols, name, value, obj.path.name)

    image = bytearray(total_size)

    # ---- 4. copy section contents
    for placement in ordered:
        if placement.discard:
            continue
        for obj in parsed:
            for sec in obj.sections:
                if assigned.get(id(sec)) is placement and sec.raw:
                    offset = sec.vaddr - base
                    image[offset:offset + len(sec.raw)] = sec.raw

    # ---- 5. apply relocations
    for obj in parsed:
        for sec in obj.sections:
            if assigned.get(id(sec)) is None or sec.discard:
                continue
            for (rel_offset, sym_index, rel_type) in sec.relocations:
                _apply_relocation(obj, sec, rel_offset, sym_index, rel_type,
                                  symbols, base, image)

    # ---- 6. entry point
    if entry is None:
        entry_addr = base
    else:
        entry_addr = resolve_symbol(symbols, entry)
        if entry_addr is None:
            raise LinkError(
                f"entry symbol {entry!r} not found; defined symbols include "
                f"{', '.join(sorted(symbols)[:12])}"
            )

    bounds = {}
    for placement in ordered:
        if placement.discard:
            continue                           # occupies nothing, so it has no bounds
        start = region_offsets[placement.output_name]
        size = 0
        for obj in parsed:
            for sec in obj.sections:
                if assigned.get(id(sec)) is placement:
                    size = max(size, (sec.vaddr - start) + sec.size)
        bounds[placement.output_name] = (start, size)

    if verbose:
        _print_layout(parsed, ordered, region_offsets, bounds, base)

    return LinkResult(image=bytes(image), symbols=symbols, section_bounds=bounds,
                      base=base, entry=entry_addr, size=total_size)


def _print_layout(parsed, ordered, region_offsets, bounds, base) -> None:
    """Print what the linker placed, in the order it placed it.

    A discarded region is named but has no bounds -- occupying nothing is the
    point of discarding it -- so it is printed as discarded rather than looked up.
    """
    end = max((start + size for start, size in bounds.values()), default=base)
    print(f"link base {base:#x}, image ends at {end:#x} ({end - base} bytes)")
    for placement in ordered:
        if placement.discard:
            print(f"  {placement.output_name:<8} (discarded)")
            continue
        start, size = bounds[placement.output_name]
        print(f"  {placement.output_name:<8} {start:#010x} .. {start + size:#010x} "
              f"({size} bytes)")


def _define(symbols: dict[str, int], name: str, value: int, source: str) -> None:
    """Record a symbol definition, refusing silent redefinition.

    nasm emits aliases (an `equ` and the label it points at share an address),
    which is fine, but two *different* addresses for one name would make the
    image wrong, so that is an error rather than a last-one-wins.
    """
    existing = symbols.get(name)
    if existing is not None and existing != value:
        raise LinkError(
            f"symbol {name!r} is defined more than once with different addresses "
            f"({existing:#x} and {value:#x}); last definition came from {source}"
        )
    symbols[name] = value


def _apply_relocation(obj: ObjectFile, sec: InputSection, offset: int, sym_index: int,
                      rel_type: int, symbols: dict[str, int], base: int,
                      image: bytearray) -> None:
    if rel_type == 0x0000:                     # ABSOLUTE: nothing to do
        return
    if rel_type not in SUPPORTED_RELOCATIONS:
        name = RELOCATION_NAMES.get(rel_type, f"unknown {rel_type:#06x}")
        raise LinkError(
            f"unsupported relocation {name} ({rel_type:#06x}) in {obj.path.name} "
            f"section {sec.name} at offset {offset:#x}"
        )
    if sym_index not in obj.symbols_by_index:
        raise LinkError(
            f"{obj.path.name}: relocation at {offset:#x} references symbol table "
            f"entry {sym_index}, which is not a symbol (the object has "
            f"{len(obj.symbols_by_index)} symbols across {len(obj.symbols)} entries)"
        )
    sym = obj.symbols_by_index[sym_index]
    name = sym.name
    if not name:
        raise LinkError(
            f"{obj.path.name}: relocation at {offset:#x} in {sec.name} references an "
            "unnamed symbol; COFF section symbols are named after their section, so "
            "this object is malformed"
        )

    if sym.is_absolute:
        target = sym.value
    elif 0 < sym.section_number <= len(obj.sections):
        section = obj.sections[sym.section_number - 1]
        if _is_section_symbol(sym, section):
            target = section.vaddr
        else:
            target = section.vaddr + sym.value
    else:
        # Undefined here, so it has to be defined by another object, under either
        # spelling (see resolve_symbol).
        target = resolve_symbol(symbols, name)
        if target is None:
            raise LinkError(_unresolved_message(obj, sec, offset, name, symbols))

    place = sec.vaddr - base + offset
    if place < 0 or place + 4 > len(image):
        raise LinkError(
            f"{obj.path.name}: relocation at {offset:#x} in {sec.name} points outside "
            "the image"
        )
    import os as _os
    if _os.environ.get("MYOS_LINK_DEBUG"):
        print(f"[link] {sec.name}+{offset:#x} ({sec.vaddr + offset:#x}) "
              f"type={rel_type:#06x} symbol={name!r} section={sym.section_number} "
              f"value={sym.value:#x} -> target={target:#x}")
    addend = struct.unpack_from("<i", image, place)[0]
    if rel_type in (0x0014,):                  # REL32: relative, PC is next insn
        value = target + addend - (sec.vaddr + offset + 4)
    else:                                      # ADDR32 / DIR32 / *NB
        value = target + addend
    struct.pack_into("<I", image, place, value & 0xFFFFFFFF)


def _unresolved_message(obj: ObjectFile, sec: InputSection, offset: int,
                        name: str, symbols: dict[str, int]) -> str:
    key = name.lstrip("_")
    candidates = [s for s in symbols if s.lstrip("_") == key]
    hint = ""
    if candidates:
        hint = f" (defined as {', '.join(candidates)} elsewhere; check underscore/name mangling)"
    if name.startswith("__") or "guard" in name or name.endswith("@@"):
        hint += " (compiler runtime helper: build with -fno-threadsafe-statics and -nostdlib)"
    return (
        f"unresolved symbol {name!r} referenced by {obj.path.name} section "
        f"{sec.name} at offset {offset:#x}{hint}"
    )


def _align_up(value: int, align: int) -> int:
    if align <= 1:
        return value
    return (value + align - 1) // align * align


# -------------------------------------------------------------- image helpers

def patch_image_header(image: bytes, entry_offset: int,
                       header_size: int = 16) -> bytes:
    """Fill in the entry offset, size and checksum of a kernel image header.

    The assembly side cannot write the entry offset, because `kernel_main -
    image_header` is a difference between .text and .data and would need another
    16-bit relocation.  Both values are therefore derived here instead, from the
    linker's own symbol table, and the checksum is recomputed over the result.

    Layout (see boot/boot.inc and kernel16/main.asm):
        0..3 magic, 4 arch, 5 version, 6 flags,
        7 checksum, 8..11 entry offset (dword), 12..15 size (dword)

    Entry and size are dwords: the 32-bit kernel is larger than 64 KiB, and the
    word-sized fields this format started with could not describe it.
    """
    if len(image) < header_size:
        raise LinkError(f"image is {len(image)} bytes, shorter than the {header_size}-byte header")
    if entry_offset < header_size or entry_offset >= len(image):
        raise LinkError(
            f"entry offset {entry_offset:#x} is outside the image "
            f"({header_size:#x}..{len(image):#x})"
        )
    body = bytearray(image)
    struct.pack_into("<I", body, 8, entry_offset)
    struct.pack_into("<I", body, 12, len(body))
    body[6] = 0
    body[7] = 0
    body[7] = (-sum(body[:header_size])) & 0xFF
    if sum(body[:header_size]) & 0xFF != 0:
        raise LinkError("internal error: header checksum did not verify after patching")
    return bytes(body)


def verify_header(image: bytes, header_size: int = 16, magic: bytes = b"MYOS",
                  arch: Optional[int] = None) -> None:
    """Raise LinkError unless the image header is present and self-consistent."""
    if len(image) < header_size:
        raise LinkError(f"image is {len(image)} bytes, shorter than the {header_size}-byte header")
    if image[:4] != magic:
        raise LinkError(f"image magic is {image[:4]!r}, expected {magic!r}")
    if arch is not None and image[4] != arch:
        raise LinkError(f"image architecture byte is {image[4]}, expected {arch}")
    if sum(image[:header_size]) & 0xFF != 0:
        raise LinkError("image header checksum does not verify")
    if image[6] != 0:
        raise LinkError(f"image header flags byte is {image[6]}, expected 0")
    recorded_size = struct.unpack_from("<I", image, 12)[0]
    if recorded_size != len(image):
        raise LinkError(
            f"image header records size {recorded_size} but the image is "
            f"{len(image)} bytes"
        )
    entry = struct.unpack_from("<I", image, 8)[0]
    if not header_size <= entry < len(image):
        raise LinkError(f"image header entry offset {entry:#x} is outside the image")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="link PE32 objects into a flat binary")
    parser.add_argument("objects", nargs="+", help="PE32 relocatable object files")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--base", default="0x100000")
    parser.add_argument("--entry")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    result = link(args.objects, base=int(args.base, 0), entry=args.entry,
                  verbose=args.verbose)
    Path(args.output).write_bytes(result.image)
    print(f"wrote {args.output}: {result.size} bytes, entry {result.entry:#x}")
