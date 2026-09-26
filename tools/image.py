"""
Disk image construction for the myos boot images.

Everything here is raw bytes and 512-byte sectors: a NASM boot sector is placed
at LBA 0 of a 1.44 MiB floppy image or into the MBR + first partition sector of
a partitioned hard disk image, and an optional kernel binary follows it.  The
module also implements just enough FAT12 to format a floppy and drop files into
it, so a kernel can be handed to the boot sector through a real filesystem
instead of a raw sector offset.

Layout notes that the rest of the project depends on:

  * A floppy image is 1.44 MiB, geometry 80/2/18, and its boot sector is also a
    FAT12 boot record (BPB at offset 11, two FATs, 224-entry root directory).
  * A hard disk image is an MBR at LBA 0 whose partition table lives at offset
    446.  The active partition holds a *copy* of the boot sector with the BPB
    and partition table zeroed, and the kernel starts one sector after it.
  * CHS fields are the classic 3-byte packing: head in byte 0, sector in bits
    0-5 of byte 1 with cylinder bits 8-9 in bits 6-7, cylinder low byte in
    byte 2.  LBA fields in the partition entry are authoritative; the CHS
    fields are best-effort and saturate at the 1023/254/63 CHS limit.

No external dependencies, standard library only.
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from typing import Optional

SECTOR = 512

#: Standard 3.5" 1.44 MiB floppy geometry.
FLOPPY_1440 = dict(cylinders=80, heads=2, sectors_per_track=18)

#: Hard disk CHS limits imposed by the 3-byte encoding.
MAX_CHS_CYLINDERS = 1024
MAX_CHS_HEADS = 256
MAX_CHS_SECTORS = 64

#: Partition type written into the MBR entry (FAT32 with CHS addressing).
PARTITION_TYPE_FAT32_CHS = 0x0B

_MBR_PARTITION_TABLE_OFFSET = 446
_PARTITION_ENTRY_SIZE = 16
_PARTITION_ENTRIES = 4
_FAT_NAME_SIZE = 11
_DIRECTORY_ENTRY_SIZE = 32

_FAT_NAME_FORBIDDEN = set('"*/:<>?\\|+,;=[]')


class ImageError(Exception):
    """Raised for any invalid input or impossible image layout."""


@dataclass(frozen=True)
class Geometry:
    """A CHS disk geometry.

    ``cylinders``/``heads``/``sectors_per_track`` are the raw CHS dimensions;
    sectors are always 512 bytes and the sector numbering used by :meth:`lba`
    is 1-based, matching what BIOS INT 13h hands to a boot sector.
    """

    cylinders: int
    heads: int
    sectors_per_track: int

    def __post_init__(self) -> None:
        for name in ("cylinders", "heads", "sectors_per_track"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ImageError(f"{name} must be an integer, got {value!r}")
            if value <= 0:
                raise ImageError(f"{name} must be positive, got {value}")
        if self.cylinders > MAX_CHS_CYLINDERS:
            raise ImageError(
                f"geometry has {self.cylinders} cylinders, "
                f"but CHS encoding allows at most {MAX_CHS_CYLINDERS}"
            )
        if self.heads > MAX_CHS_HEADS:
            raise ImageError(
                f"geometry has {self.heads} heads, "
                f"but CHS encoding allows at most {MAX_CHS_HEADS}"
            )
        if self.sectors_per_track > MAX_CHS_SECTORS:
            raise ImageError(
                f"geometry has {self.sectors_per_track} sectors per track, "
                f"but CHS encoding allows at most {MAX_CHS_SECTORS}"
            )

    @property
    def total_sectors(self) -> int:
        """Number of 512-byte sectors the geometry addresses."""
        return self.cylinders * self.heads * self.sectors_per_track

    @property
    def size_bytes(self) -> int:
        """Size of a fully populated image in bytes."""
        return self.total_sectors * SECTOR

    def lba(self, cylinder: int, head: int, sector: int) -> int:
        """Convert a 1-based CHS address to a 0-based LBA."""
        self._check_chs(cylinder, head, sector)
        return (cylinder * self.heads + head) * self.sectors_per_track + (sector - 1)

    def chs_from_lba(self, lba: int) -> tuple[int, int, int]:
        """Convert a 0-based LBA to ``(cylinder, head, 1-based sector)``."""
        if not isinstance(lba, int) or isinstance(lba, bool):
            raise ImageError(f"LBA must be an integer, got {lba!r}")
        if lba < 0:
            raise ImageError(f"LBA {lba} is negative")
        if lba >= self.total_sectors:
            raise ImageError(
                f"LBA {lba} is outside a {self.cylinders}/{self.heads}/"
                f"{self.sectors_per_track} geometry of {self.total_sectors} sectors"
            )
        cylinder, remainder = divmod(lba, self.heads * self.sectors_per_track)
        head, sector0 = divmod(remainder, self.sectors_per_track)
        return cylinder, head, sector0 + 1

    def _check_chs(self, cylinder: int, head: int, sector: int) -> None:
        for name, value in (("cylinder", cylinder), ("head", head), ("sector", sector)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ImageError(f"{name} must be an integer, got {value!r}")
        if not 0 <= cylinder < self.cylinders:
            raise ImageError(
                f"cylinder {cylinder} is outside 0..{self.cylinders - 1} "
                f"for a {self.cylinders}/{self.heads}/{self.sectors_per_track} geometry"
            )
        if not 0 <= head < self.heads:
            raise ImageError(
                f"head {head} is outside 0..{self.heads - 1} "
                f"for a {self.cylinders}/{self.heads}/{self.sectors_per_track} geometry"
            )
        if not 1 <= sector <= self.sectors_per_track:
            raise ImageError(
                f"sector {sector} is outside 1..{self.sectors_per_track} "
                f"(CHS sectors are 1-based)"
            )

    def __str__(self) -> str:
        return (f"{self.cylinders}/{self.heads}/{self.sectors_per_track} "
                f"({self.total_sectors} sectors, {self.size_bytes} bytes)")


def floppy_geometry() -> Geometry:
    """Geometry of a 3.5" 1.44 MiB floppy: 80 cylinders, 2 heads, 18 sectors."""
    return Geometry(**FLOPPY_1440)


def hard_disk_geometry(total_sectors: int) -> Geometry:
    """Largest sane CHS geometry covering ``total_sectors``.

    The search follows the ordering a BIOS-era tool would use, within the classic
    limits of 16 heads, 63 sectors per track and 1024 cylinders: prefer more
    sectors per track, then more heads, then less wasted space -- and always
    cover ``total_sectors``.  A fully standard geometry therefore wins, so
    anything up to 1024/16/63 (516096 sectors, about 252 MiB) gets 1024/16/63,
    while a small image gets a near-exact geometry with real track and head
    counts instead of a degenerate one-sector-per-track shape.

    Raises :class:`ImageError` when ``total_sectors`` is not positive or is too
    large for any geometry within those limits.
    """
    if not isinstance(total_sectors, int) or isinstance(total_sectors, bool):
        raise ImageError(f"total_sectors must be an integer, got {total_sectors!r}")
    if total_sectors <= 0:
        raise ImageError(f"total_sectors must be positive, got {total_sectors}")

    best: Optional[Geometry] = None
    best_key: Optional[tuple[int, int, int]] = None
    for heads in range(16, 0, -1):
        for spt in range(63, 0, -1):
            per_cylinder = heads * spt
            cylinders = -(-total_sectors // per_cylinder)
            if cylinders > MAX_CHS_CYLINDERS:
                continue
            waste = cylinders * per_cylinder - total_sectors
            key = (waste, -spt, -heads)
            if best_key is None or key < best_key:
                best = Geometry(cylinders, heads, spt)
                best_key = key
                if waste == 0:
                    return best

    if best is None:
        limit = MAX_CHS_CYLINDERS * 16 * 63
        raise ImageError(
            f"{total_sectors} sectors is too large for any CHS geometry "
            f"(at most {MAX_CHS_CYLINDERS}/16/63 = {limit} sectors, "
            f"about {limit * SECTOR // (1024 * 1024)} MiB)"
        )
    return best


def validate_boot_sector(data: bytes) -> None:
    """Raise :class:`ImageError` unless ``data`` is a 512-byte boot sector.

    A boot sector must be exactly :data:`SECTOR` bytes long and end with the
    signature bytes 0x55 0xAA, because that is the only thing the firmware
    checks before jumping to it.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ImageError(f"boot sector must be bytes, got {type(data).__name__}")
    if len(data) != SECTOR:
        raise ImageError(
            f"boot sector is {len(data)} bytes, expected exactly {SECTOR}"
        )
    if bytes(data[510:512]) != b"\x55\xaa":
        found = bytes(data[510:512]).hex(" ")
        raise ImageError(
            "boot sector is missing the 0x55AA signature at offsets 510..511 "
            f"(found {found or 'nothing'})"
        )


# --------------------------------------------------------------------- images


def build_floppy_image(boot_sector: bytes, kernel: bytes | None = None,
                       kernel_offset_sectors: int = 1,
                       pad: bool = True) -> bytearray:
    """Assemble a 1.44 MiB floppy image.

    The boot sector is copied to LBA 0 and ``kernel`` to LBA
    ``kernel_offset_sectors`` (1 by default, i.e. immediately after the boot
    sector).  With ``pad=True`` the image is zero-filled to exactly 1.44 MiB;
    otherwise it ends at the last byte addressed by the boot sector, the kernel
    or the offset, whichever is furthest, so an empty kernel still keeps its
    place.

    A copy of the boot sector without any FAT12 BPB is written, so a kernel
    passed here is addressed by raw sector offset, not through a filesystem.
    Use :func:`format_fat12_floppy` and :func:`fat12_write_file` for that.

    Raises :class:`ImageError` for a malformed boot sector, a negative offset,
    or a kernel that does not fit in the image.
    """
    validate_boot_sector(boot_sector)
    geometry = floppy_geometry()
    _check_non_negative("kernel_offset_sectors", kernel_offset_sectors)
    sector = bytes(boot_sector[:SECTOR])

    if kernel is None and pad:
        image = bytearray(geometry.size_bytes)
        image[0:SECTOR] = sector
        return image

    body = bytes(kernel) if kernel is not None else b""
    content_end = kernel_offset_sectors * SECTOR + len(body)
    if content_end > geometry.size_bytes:
        if kernel_offset_sectors >= geometry.total_sectors:
            raise ImageError(
                f"kernel_offset_sectors is {kernel_offset_sectors}, but a 1.44 MiB "
                f"floppy only has {geometry.total_sectors} sectors "
                f"(0..{geometry.total_sectors - 1})"
            )
        used = _sectors_for(content_end)
        raise ImageError(
            f"kernel of {_sectors_for(len(body))} sectors does not fit in a "
            f"1.44 MiB floppy: with the boot sector it would need {used} of "
            f"{geometry.total_sectors} sectors"
        )
    if pad:
        size = geometry.size_bytes
    else:
        # An unpadded image still covers everything addressed so far, so an
        # empty kernel written at a high offset keeps its place.
        size = max(content_end, SECTOR)

    image = bytearray(size)
    image[0:SECTOR] = sector
    if body:
        image[kernel_offset_sectors * SECTOR:content_end] = body
    return image


def build_hard_disk_image(boot_sector: bytes, kernel: bytes | None = None,
                          partition_lba: int = 2048, active: int = 0,
                          geometry: Geometry | None = None) -> bytearray:
    """Assemble an MBR-partitioned hard disk image.

    ``boot_sector`` is used twice:

    * as the MBR at LBA 0, with the four partition entries written at offset
      446 and the 0x55AA signature left intact;
    * as the first sector of the active partition at ``partition_lba``, with the
      BPB area (offsets 11..61) and the partition table area (446..509) zeroed,
      leaving only the executable code and the signature.

    ``active`` selects which of the four entries is bootable (0x80 in byte 0);
    the other three are written as empty entries (all zero).  ``kernel`` is
    copied to ``partition_lba + 1``.

    The image is sized to cover the boot sector, the partition boot sector and
    the kernel (and never less than the supplied geometry), so a kernel that
    does not fit raises :class:`ImageError` only when it is too large for the
    geometry in use.
    """
    validate_boot_sector(boot_sector)
    _check_non_negative("partition_lba", partition_lba)
    if partition_lba < 1:
        raise ImageError(
            f"partition_lba must be at least 1 so LBA 0 stays the MBR, got {partition_lba}"
        )
    if not isinstance(active, int) or isinstance(active, bool):
        raise ImageError(f"active must be an integer, got {active!r}")
    if not 0 <= active < _PARTITION_ENTRIES:
        raise ImageError(
            f"active partition index {active} is outside 0..{_PARTITION_ENTRIES - 1}"
        )

    sector = bytes(boot_sector[:SECTOR])
    kernel_data = bytes(kernel) if kernel is not None else b""

    content_sectors = partition_lba + 1 + _sectors_for(len(kernel_data))
    if geometry is None:
        disk = hard_disk_geometry(content_sectors)
    else:
        if not isinstance(geometry, Geometry):
            raise ImageError(
                f"geometry must be a Geometry instance, got {type(geometry).__name__}"
            )
        disk = geometry
        if content_sectors > disk.total_sectors:
            raise ImageError(
                f"boot sector and kernel of {_sectors_for(len(kernel_data))} sectors "
                f"need {content_sectors} sectors, but the {disk} geometry only "
                f"covers {disk.total_sectors} sectors"
            )

    size = disk.size_bytes
    image = bytearray(size)

    mbr = bytearray(sector)
    mbr[_MBR_PARTITION_TABLE_OFFSET:_MBR_PARTITION_TABLE_OFFSET + _PARTITION_ENTRIES * _PARTITION_ENTRY_SIZE] = bytes(64)

    # The geometry is guaranteed to reach the last content sector and never
    # exceeds CHS addressing, so the final sector of the volume is the last
    # LBA a partitioning tool would name; chs_from_lba cannot fall off the end.
    end_lba = disk.total_sectors - 1
    end_cylinder, end_head, end_sector = disk.chs_from_lba(end_lba)
    entry = _pack_partition_entry(
        bootable=True,
        chs_start=_encode_chs(*disk.chs_from_lba(partition_lba)),
        part_type=PARTITION_TYPE_FAT32_CHS,
        chs_end=_encode_chs(end_cylinder, end_head, end_sector),
        lba_start=partition_lba,
        sectors=disk.total_sectors - partition_lba,
    )
    offset = _MBR_PARTITION_TABLE_OFFSET + active * _PARTITION_ENTRY_SIZE
    mbr[offset:offset + _PARTITION_ENTRY_SIZE] = entry
    image[0:SECTOR] = mbr

    volume = bytearray(sector)
    volume[11:62] = bytes(51)               # no BPB in the partition copy
    volume[_MBR_PARTITION_TABLE_OFFSET:510] = bytes(64)
    image[partition_lba * SECTOR:(partition_lba + 1) * SECTOR] = volume

    if kernel_data:
        start = (partition_lba + 1) * SECTOR
        image[start:start + len(kernel_data)] = kernel_data

    return image


def read_sector(image: bytes | bytearray, lba: int) -> bytes:
    """Return the 512 bytes of sector ``lba``, or raise :class:`ImageError`."""
    if not isinstance(image, (bytes, bytearray, memoryview)):
        raise ImageError(f"image must be bytes, got {type(image).__name__}")
    if not isinstance(lba, int) or isinstance(lba, bool):
        raise ImageError(f"LBA must be an integer, got {lba!r}")
    if lba < 0:
        raise ImageError(f"LBA {lba} is negative")
    offset = lba * SECTOR
    if offset + SECTOR > len(image):
        raise ImageError(
            f"LBA {lba} is outside the image: it needs bytes "
            f"{offset}..{offset + SECTOR}, but the image is {len(image)} bytes"
        )
    return bytes(image[offset:offset + SECTOR])


def write_sector(image: bytearray, lba: int, data: bytes) -> None:
    """Write exactly 512 bytes at sector ``lba`` of an existing image."""
    if not isinstance(image, bytearray):
        raise ImageError(
            f"image must be a bytearray to be written, got {type(image).__name__}"
        )
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ImageError(f"data must be bytes, got {type(data).__name__}")
    if len(data) != SECTOR:
        raise ImageError(f"sector data is {len(data)} bytes, expected exactly {SECTOR}")
    if not isinstance(lba, int) or isinstance(lba, bool):
        raise ImageError(f"LBA must be an integer, got {lba!r}")
    if lba < 0:
        raise ImageError(f"LBA {lba} is negative")
    offset = lba * SECTOR
    if offset + SECTOR > len(image):
        raise ImageError(
            f"LBA {lba} is outside the image: it needs bytes "
            f"{offset}..{offset + SECTOR}, but the image is {len(image)} bytes"
        )
    image[offset:offset + SECTOR] = bytes(data)


def write_partition_entry(image: bytearray, index: int, *, lba_start: int, sectors: int,
                          part_type: int, geometry: Geometry,
                          bootable: bool = False) -> None:
    """Rewrite one of the four MBR partition entries of an existing image.

    :func:`build_hard_disk_image` gives the whole disk to a single entry, which is
    all a boot loader needs.  A second partition -- the filesystem volume -- has to
    be carved out of that: the boot container keeps LBA 1 up to the volume, and the
    volume gets an entry of its own, so the two never overlap and the kernel finds
    the volume by type byte instead of by a hard-coded LBA.

    The CHS fields are encoded from ``geometry``, the same geometry the image was
    built with, so a partition this function writes is one a BIOS can start.
    """
    if not isinstance(image, bytearray):
        raise ImageError(
            f"image must be a bytearray to be written, got {type(image).__name__}"
        )
    if not isinstance(index, int) or isinstance(index, bool):
        raise ImageError(f"partition index must be an integer, got {index!r}")
    if not 0 <= index < _PARTITION_ENTRIES:
        raise ImageError(
            f"partition index {index} is outside 0..{_PARTITION_ENTRIES - 1}"
        )
    if not isinstance(geometry, Geometry):
        raise ImageError(
            f"geometry must be a Geometry instance, got {type(geometry).__name__}"
        )
    _check_non_negative("lba_start", lba_start)
    _check_non_negative("sectors", sectors)
    if lba_start < 1:
        raise ImageError(
            f"a partition cannot start at LBA {lba_start}: LBA 0 is the MBR"
        )
    if sectors < 1:
        raise ImageError(f"partition {index} would be empty ({sectors} sectors)")
    if lba_start + sectors > geometry.total_sectors:
        raise ImageError(
            f"partition {index} covers LBA {lba_start}..{lba_start + sectors - 1}, "
            f"past the last sector ({geometry.total_sectors - 1}) of the {geometry} "
            "geometry"
        )
    if lba_start + sectors > len(image) // SECTOR:
        raise ImageError(
            f"partition {index} covers LBA {lba_start}..{lba_start + sectors - 1}, "
            f"past the {len(image) // SECTOR}-sector image"
        )
    mbr = bytearray(read_sector(image, 0))
    entry = _pack_partition_entry(
        bootable=bootable,
        chs_start=_encode_chs(*geometry.chs_from_lba(lba_start)),
        part_type=part_type,
        chs_end=_encode_chs(*geometry.chs_from_lba(lba_start + sectors - 1)),
        lba_start=lba_start,
        sectors=sectors,
    )
    offset = _MBR_PARTITION_TABLE_OFFSET + index * _PARTITION_ENTRY_SIZE
    mbr[offset:offset + _PARTITION_ENTRY_SIZE] = entry
    image[0:SECTOR] = mbr


def mbr_partition_entries(image: bytes | bytearray) -> list[dict]:
    """Parse the four 16-byte MBR partition entries.

    Each returned dict has the keys ``index``, ``active`` (bool), ``type``,
    ``lba_start``, ``sectors``, ``chs_start`` and ``chs_end``; the CHS values
    are ``(cylinder, head, 1-based sector)`` triples.  An entry whose type byte
    is zero is reported as unused with ``lba_start``/``sectors`` of 0.
    """
    mbr = read_sector(image, 0)
    if mbr[510:512] != b"\x55\xaa":
        raise ImageError(
            "LBA 0 does not look like an MBR: no 0x55AA signature at offsets 510..511"
        )
    entries: list[dict] = []
    for index in range(_PARTITION_ENTRIES):
        raw = mbr[_MBR_PARTITION_TABLE_OFFSET + index * _PARTITION_ENTRY_SIZE:
                  _MBR_PARTITION_TABLE_OFFSET + (index + 1) * _PARTITION_ENTRY_SIZE]
        flag = raw[0]
        if flag not in (0x00, 0x80):
            raise ImageError(
                f"partition entry {index} has an invalid status byte "
                f"{flag:#04x}; expected 0x00 or 0x80"
            )
        part_type = raw[4]
        unused = part_type == 0 and raw[1:4] == b"\x00" * 3 and raw[5:] == b"\x00" * 11
        entries.append({
            "index": index,
            "active": flag == 0x80,
            "type": part_type,
            "lba_start": 0 if unused else struct.unpack_from("<I", raw, 8)[0],
            "sectors": 0 if unused else struct.unpack_from("<I", raw, 12)[0],
            "chs_start": _decode_chs(raw[1:4]),
            "chs_end": _decode_chs(raw[5:8]),
        })
    return entries


def kernel_sectors(kernel: bytes) -> int:
    """Number of 512-byte sectors needed to hold ``kernel`` (0 when empty)."""
    if not isinstance(kernel, (bytes, bytearray, memoryview)):
        raise ImageError(f"kernel must be bytes, got {type(kernel).__name__}")
    return _sectors_for(len(kernel))


# ------------------------------------------------------------------ FAT12


def format_fat12_floppy(volume_label: str = "MYOS") -> bytearray:
    """Build an empty, structurally valid FAT12 1.44 MiB floppy filesystem.

    The result has a boot record with a complete BPB, two identical FATs whose
    first two entries are the reserved media descriptor ``F0 FF FF``, a
    zero-filled 224-entry root directory and a zero-filled data area.
    """
    geometry = floppy_geometry()
    layout = _FloppyLayout()
    image = bytearray(geometry.size_bytes)

    boot = bytearray(SECTOR)
    boot[0:3] = b"\xEB\x3C\x90"             # jmp short +0x3C ; nop
    boot[3:11] = b"MYOS1440"                # OEM name
    _write_bpb_fields(
        boot,
        geometry,
        layout,
        total_sectors16=geometry.total_sectors,
        total_sectors32=0,
        media=layout.media,
        volume_label=volume_label,
    )
    boot[510:512] = b"\x55\xaa"
    image[0:SECTOR] = boot

    fat = bytearray(layout.fat_size * SECTOR)
    fat[0:3] = bytes((layout.media, 0xFF, 0xFF))
    for copy in range(layout.fat_copies):
        start = layout.fat_start_sector + copy * layout.fat_size
        image[start * SECTOR:(start + layout.fat_size) * SECTOR] = fat

    return image


def write_bpb(boot_sector: bytearray, geometry: Geometry | None = None,
              volume_label: str = "MYOS") -> bytearray:
    """Fill the standard BPB into an existing 512-byte boot sector.

    Offsets 11..61 receive the BPB and extended BPB fields for a FAT12 volume
    (the layout :func:`format_fat12_floppy` produces), the volume label is
    written at 43..53 and the filesystem type string at 54..62.  The boot
    signature at 510..511 is left untouched, so the caller's code and signature
    survive.  The same :class:`bytearray` is returned for convenience.

    ``geometry`` defaults to a 1.44 MiB floppy; any geometry with at most 65535
    sectors uses the 16-bit total-sector field, larger ones use the 32-bit one.
    """
    if not isinstance(boot_sector, bytearray):
        raise ImageError(
            "boot_sector must be a bytearray to be modified, got "
            f"{type(boot_sector).__name__}"
        )
    if len(boot_sector) != SECTOR:
        raise ImageError(
            f"boot sector is {len(boot_sector)} bytes, expected exactly {SECTOR}"
        )
    geo = floppy_geometry() if geometry is None else geometry
    if not isinstance(geo, Geometry):
        raise ImageError(
            f"geometry must be a Geometry instance, got {type(geo).__name__}"
        )
    layout = _FloppyLayout(sectors_per_cluster=_default_cluster_size(geo))

    total16 = geo.total_sectors if geo.total_sectors <= 0xFFFF else 0
    total32 = 0 if total16 else geo.total_sectors
    _write_bpb_fields(
        boot_sector,
        geo,
        layout,
        total_sectors16=total16,
        total_sectors32=total32,
        media=layout.media,
        volume_label=volume_label,
    )
    return boot_sector


def fat12_write_file(image: bytearray, name: str, data: bytes) -> None:
    """Create ``name`` in the FAT12 filesystem whose boot record is at LBA 0.

    The BPB is read back from offset 11 of the boot sector, so this works for
    :func:`format_fat12_floppy` output and for any hand-written boot sector
    carrying a matching BPB.  Free clusters are allocated from the end of the
    data area backwards, a root directory entry is created at the first free
    slot, and every FAT copy is updated.

    Raises :class:`ImageError` for an unusable BPB, a malformed 8.3 name, a
    name that already exists, a full root directory, or a file that does not
    fit in the free space.
    """
    if not isinstance(image, bytearray):
        raise ImageError(
            f"image must be a bytearray to be modified, got {type(image).__name__}"
        )
    layout = _parse_fat12_layout(image)
    short_name = _encode_short_name(name)
    wanted = _decode_short_name(short_name)

    if any(entry["name"] == wanted for entry in _root_directory(image, layout)):
        raise ImageError(f"{wanted} already exists in the root directory")

    slot = _find_free_directory_slot(image, layout)
    if slot is None:
        raise ImageError(
            f"root directory is full ({layout.root_entries} entries); "
            f"cannot create {short_name}"
        )

    cluster_size = layout.sectors_per_cluster * SECTOR
    needed = _sectors_for(len(data))
    clusters_needed = 0 if not data else -(-len(data) // cluster_size)
    if clusters_needed > layout.count_of_clusters:
        raise ImageError(
            f"{short_name} is {len(data)} bytes ({needed} sectors), which needs "
            f"{clusters_needed} clusters but the volume only has "
            f"{layout.count_of_clusters}"
        )

    chain = _allocate_clusters(image, layout, clusters_needed, short_name)

    offset = layout.root_start_sector * SECTOR + slot * _DIRECTORY_ENTRY_SIZE
    entry = bytearray(_DIRECTORY_ENTRY_SIZE)
    entry[0:_FAT_NAME_SIZE] = short_name
    entry[11] = 0x20                                     # archive
    struct.pack_into("<H", entry, 26, _encode_time())
    struct.pack_into("<H", entry, 24, _encode_date())
    struct.pack_into("<H", entry, 20, (chain[0] if chain else 0))
    struct.pack_into("<I", entry, 28, len(data))
    image[offset:offset + _DIRECTORY_ENTRY_SIZE] = entry

    for index, cluster in enumerate(chain):
        start = layout.data_start_sector * SECTOR + (cluster - 2) * cluster_size
        end = start + cluster_size
        image[start:end] = bytes(cluster_size)
        chunk = data[index * cluster_size:(index + 1) * cluster_size]
        image[start:start + len(chunk)] = chunk


# -------------------------------------------------------------- FAT12 helpers


@dataclass(frozen=True)
class _FloppyLayout:
    """Fixed FAT12 layout used by the formatter and by :func:`write_bpb`."""

    bytes_per_sector: int = SECTOR
    sectors_per_cluster: int = 1
    reserved_sectors: int = 1
    fat_copies: int = 2
    root_entries: int = 224
    media: int = 0xF0
    fat_size: int = 9

    @property
    def fat_start_sector(self) -> int:
        return self.reserved_sectors

    @property
    def root_start_sector(self) -> int:
        return self.reserved_sectors + self.fat_copies * self.fat_size

    @property
    def root_sectors(self) -> int:
        return -(-self.root_entries * _DIRECTORY_ENTRY_SIZE // self.bytes_per_sector)

    @property
    def data_start_sector(self) -> int:
        return self.root_start_sector + self.root_sectors

    @property
    def count_of_clusters(self) -> int:
        return (80 * 2 * 18 - self.data_start_sector) // self.sectors_per_cluster


@dataclass(frozen=True)
class _BpbLayout:
    """The layout as actually read back from a BPB on disk."""

    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    fat_copies: int
    root_entries: int
    media: int
    fat_size: int
    total_sectors: int

    @property
    def fat_start_sector(self) -> int:
        return self.reserved_sectors

    @property
    def root_start_sector(self) -> int:
        return self.reserved_sectors + self.fat_copies * self.fat_size

    @property
    def root_sectors(self) -> int:
        return -(-self.root_entries * _DIRECTORY_ENTRY_SIZE // self.bytes_per_sector)

    @property
    def data_start_sector(self) -> int:
        return self.root_start_sector + self.root_sectors

    @property
    def count_of_clusters(self) -> int:
        data_sectors = self.total_sectors - self.data_start_sector
        return data_sectors // self.sectors_per_cluster


def _parse_fat12_layout(image: bytearray) -> _BpbLayout:
    """Read and sanity-check the BPB at offset 11 of the boot sector."""
    if len(image) < SECTOR:
        raise ImageError(
            f"image is {len(image)} bytes, too small to hold a boot sector"
        )
    boot = bytes(image[0:SECTOR])
    bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
    sectors_per_cluster = boot[13]
    reserved_sectors = struct.unpack_from("<H", boot, 14)[0]
    fat_copies = boot[16]
    root_entries = struct.unpack_from("<H", boot, 17)[0]
    total16 = struct.unpack_from("<H", boot, 19)[0]
    media = boot[21]
    fat_size = struct.unpack_from("<H", boot, 22)[0]
    total32 = struct.unpack_from("<I", boot, 32)[0]
    total_sectors = total16 or total32

    if bytes_per_sector != SECTOR:
        raise ImageError(
            f"boot sector declares {bytes_per_sector} bytes per sector, "
            f"expected {SECTOR}; the BPB at offset 11 does not describe a "
            f"512-byte-sector FAT12 volume"
        )
    if sectors_per_cluster < 1:
        raise ImageError("BPB declares 0 sectors per cluster")
    if fat_copies < 1:
        raise ImageError("BPB declares 0 FAT copies")
    if fat_size < 1:
        raise ImageError("BPB declares a FAT of 0 sectors")
    if total_sectors < 1:
        raise ImageError("BPB declares 0 total sectors")

    layout = _BpbLayout(
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        reserved_sectors=reserved_sectors,
        fat_copies=fat_copies,
        root_entries=root_entries,
        media=media,
        fat_size=fat_size,
        total_sectors=total_sectors,
    )
    if layout.data_start_sector >= total_sectors:
        raise ImageError(
            f"BPB describes {layout.data_start_sector} metadata sectors but only "
            f"{total_sectors} total sectors"
        )
    if layout.count_of_clusters > 4084:
        raise ImageError(
            f"volume has {layout.count_of_clusters} clusters, which is not a FAT12 "
            f"volume (FAT12 allows at most 4084)"
        )
    last_byte = total_sectors * SECTOR
    if last_byte > len(image):
        raise ImageError(
            f"BPB describes a volume of {total_sectors} sectors "
            f"({last_byte} bytes) but the image is only {len(image)} bytes"
        )
    return layout


def _fat_entry(image: bytearray, layout: _BpbLayout, cluster: int) -> int:
    """Read one 12-bit FAT entry from the first FAT copy."""
    offset = cluster + cluster // 2
    start = layout.fat_start_sector * SECTOR + offset
    pair = image[start:start + 2]
    if len(pair) != 2:
        raise ImageError(f"FAT entry {cluster} lies past the end of the image")
    raw = struct.unpack("<H", bytes(pair))[0]
    return (raw >> 4) if (cluster & 1) else (raw & 0x0FFF)


def _set_fat_entry(image: bytearray, layout: _BpbLayout, cluster: int, value: int) -> None:
    """Write one 12-bit FAT entry into every FAT copy."""
    if not 0 <= value <= 0x0FFF:
        raise ImageError(f"FAT12 entry value {value:#x} does not fit in 12 bits")
    offset = cluster + cluster // 2
    for copy in range(layout.fat_copies):
        start = (layout.fat_start_sector + copy * layout.fat_size) * SECTOR + offset
        pair = bytearray(image[start:start + 2])
        if len(pair) != 2:
            raise ImageError(f"FAT entry {cluster} lies past the end of the image")
        raw = struct.unpack("<H", bytes(pair))[0]
        raw = ((raw & 0x000F) | (value << 4)) if (cluster & 1) else ((raw & 0xF000) | value)
        image[start:start + 2] = struct.pack("<H", raw)


def _allocate_clusters(image: bytearray, layout: _BpbLayout, count: int,
                       label: str) -> list[int]:
    """Claim ``count`` free clusters from the end of the data area, chaining them."""
    if count == 0:
        return []
    free: list[int] = []
    for cluster in range(layout.count_of_clusters, 1, -1):
        if _fat_entry(image, layout, cluster) == 0:
            free.append(cluster)
            if len(free) == count:
                break
    if len(free) < count:
        raise ImageError(
            f"no room for {label}: {count} clusters needed but only {len(free)} free"
        )

    for index, cluster in enumerate(free):
        successor = free[index + 1] if index + 1 < len(free) else 0x0FFF
        _set_fat_entry(image, layout, cluster, successor)
    return free


def _find_free_directory_slot(image: bytearray, layout: _BpbLayout) -> Optional[int]:
    """Index of the first unused root directory entry, if any."""
    start = layout.root_start_sector * SECTOR
    for slot in range(layout.root_entries):
        first = image[start + slot * _DIRECTORY_ENTRY_SIZE]
        if first in (0x00, 0xE5):
            return slot
    return None


def _root_directory(image: bytearray, layout: _BpbLayout) -> list[dict]:
    """Every in-use root directory entry, decoded into plain Python values."""
    start = layout.root_start_sector * SECTOR
    entries: list[dict] = []
    for slot in range(layout.root_entries):
        offset = start + slot * _DIRECTORY_ENTRY_SIZE
        raw = bytes(image[offset:offset + _DIRECTORY_ENTRY_SIZE])
        if len(raw) < _DIRECTORY_ENTRY_SIZE or raw[0] == 0x00:
            break
        if raw[0] == 0xE5:
            continue
        attributes = raw[11]
        if attributes & 0x08:               # volume label, not a file
            continue
        name = _decode_short_name(raw[0:_FAT_NAME_SIZE])
        entries.append({
            "slot": slot,
            "name": name,
            "attributes": attributes,
            "cluster": struct.unpack_from("<H", raw, 20)[0],
            "size": struct.unpack_from("<I", raw, 28)[0],
            "directory": bool(attributes & 0x10),
        })
    return entries


def _encode_short_name(name: str) -> bytes:
    """Turn ``"KERNEL.BIN"`` into the raw 11-byte 8.3 directory name."""
    if not isinstance(name, str):
        raise ImageError(f"file name must be a string, got {type(name).__name__}")
    text = name.strip().upper()
    if not text:
        raise ImageError("file name is empty")

    base, dot, extension = text.partition(".")
    if "." in extension:
        raise ImageError(f"{name!r} is not an 8.3 name: it contains more than one dot")
    if len(base) > 8:
        raise ImageError(
            f"{name!r} is not an 8.3 name: the base name is {len(base)} characters, "
            f"at most 8 are allowed"
        )
    if len(extension) > 3:
        raise ImageError(
            f"{name!r} is not an 8.3 name: the extension is {len(extension)} "
            f"characters, at most 3 are allowed"
        )
    for character in base + extension:
        if ord(character) > 0x7E or ord(character) < 0x21 or character in _FAT_NAME_FORBIDDEN:
            raise ImageError(
                f"{name!r} is not an 8.3 name: {character!r} is not allowed in a "
                f"short FAT name"
            )
    return base.ljust(8).encode("ascii") + extension.ljust(3).encode("ascii")


def _decode_short_name(raw: bytes) -> str:
    """Render the raw 11-byte 8.3 name as ``"NAME.EXT"``."""
    base = raw[0:8].decode("latin-1").rstrip(" ")
    extension = raw[8:11].decode("latin-1").rstrip(" ")
    return f"{base}.{extension}" if extension else base


def _bpb_serial(volume_label: str) -> int:
    """A stable volume serial derived from the label."""
    return (sum(volume_label.encode("latin-1", "replace")) * 0x01010101 + 0x12345678) & 0xFFFFFFFF


def _encode_date() -> int:
    """A fixed DOS date (2024-01-01) so builds are reproducible."""
    return ((2024 - 1980) << 9) | (1 << 5) | 1


def _encode_time() -> int:
    """A fixed DOS time (00:00:00) so builds are reproducible."""
    return 0


def _write_bpb_fields(boot_sector: bytearray, geometry: Geometry, layout: _FloppyLayout,
                      total_sectors16: int, total_sectors32: int, media: int,
                      volume_label: str) -> None:
    """Pack the BPB (offsets 11..61) and identification strings into a sector."""
    if not isinstance(volume_label, str):
        raise ImageError(f"volume_label must be a string, got {type(volume_label).__name__}")
    label = volume_label.strip().upper()
    if len(label) > _FAT_NAME_SIZE:
        raise ImageError(
            f"volume label {volume_label!r} is {len(label)} characters; "
            f"at most {_FAT_NAME_SIZE} are allowed"
        )
    for character in label:
        if ord(character) > 0x7E or ord(character) < 0x21 or character in _FAT_NAME_FORBIDDEN:
            raise ImageError(
                f"volume label {volume_label!r} contains {character!r}, which is not "
                f"allowed in a FAT volume label"
            )

    root_entries = layout.root_entries
    struct.pack_into("<H", boot_sector, 11, layout.bytes_per_sector)
    boot_sector[13] = layout.sectors_per_cluster
    struct.pack_into("<H", boot_sector, 14, layout.reserved_sectors)
    boot_sector[16] = layout.fat_copies
    struct.pack_into("<H", boot_sector, 17, root_entries)
    struct.pack_into("<H", boot_sector, 19, total_sectors16)
    boot_sector[21] = media
    struct.pack_into("<H", boot_sector, 22, layout.fat_size)
    struct.pack_into("<H", boot_sector, 24, geometry.sectors_per_track)
    struct.pack_into("<H", boot_sector, 26, geometry.heads)
    struct.pack_into("<I", boot_sector, 28, 0)                  # hidden sectors
    struct.pack_into("<I", boot_sector, 32, total_sectors32)
    boot_sector[36] = 0x00                                      # drive number
    boot_sector[37] = 0x00                                      # reserved
    boot_sector[38] = 0x29                                      # extended boot signature
    struct.pack_into("<I", boot_sector, 39, _bpb_serial(label or "MYOS"))
    boot_sector[43:54] = (label or "NO NAME").ljust(_FAT_NAME_SIZE).encode("ascii")
    boot_sector[54:62] = b"FAT12   "


def _default_cluster_size(geometry: Geometry) -> int:
    """A conservative sectors-per-cluster for a geometry of this size."""
    sectors = geometry.total_sectors
    if sectors <= 2880:
        return 1
    if sectors <= 16384:
        return 2
    if sectors <= 32768:
        return 4
    return 8


# ------------------------------------------------------------------ primitives


def _sectors_for(length: int) -> int:
    """Number of 512-byte sectors needed for ``length`` bytes."""
    return -(-length // SECTOR)


def _check_non_negative(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ImageError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise ImageError(f"{name} must not be negative, got {value}")


def _encode_chs(cylinder: int, head: int, sector: int) -> bytes:
    """Pack a CHS address into the classic 3-byte BIOS form.

    Saturated values (1023/254/63) are substituted when the address does not
    fit, which is what every real partitioning tool does; the LBA fields remain
    authoritative.
    """
    cylinder = min(max(cylinder, 0), 1023)
    head = min(max(head, 0), 254)
    sector = min(max(sector, 1), 63)
    packed = bytes((
        head & 0xFF,
        ((sector & 0x3F) | ((cylinder >> 8) & 0x03)),
        cylinder & 0xFF,
    ))
    if len(packed) != 3:                                 # pragma: no cover - defensive
        raise ImageError("internal error: CHS encoding did not produce three bytes")
    return packed


def _decode_chs(raw: bytes) -> tuple[int, int, int]:
    """Unpack a classic 3-byte CHS address into ``(cylinder, head, sector)``."""
    if len(raw) != 3:
        raise ImageError(f"CHS field is {len(raw)} bytes, expected exactly 3")
    head = raw[0]
    sector = raw[1] & 0x3F
    cylinder = ((raw[1] >> 6) & 0x03) << 8 | raw[2]
    return cylinder, head, sector


def _pack_partition_entry(bootable: bool, chs_start: bytes, part_type: int,
                          chs_end: bytes, lba_start: int, sectors: int) -> bytes:
    """Pack one 16-byte MBR partition entry."""
    for label, value in (("lba_start", lba_start), ("sectors", sectors)):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ImageError(f"partition {label} {value} does not fit in 32 bits")
    if not 0 <= part_type <= 0xFF:
        raise ImageError(f"partition type {part_type} does not fit in one byte")
    entry = struct.pack(
        "<B3sB3sII",
        0x80 if bootable else 0x00,
        chs_start,
        part_type,
        chs_end,
        lba_start,
        sectors,
    )
    if len(entry) != _PARTITION_ENTRY_SIZE:              # pragma: no cover - defensive
        raise ImageError("internal error: partition entry is not 16 bytes")
    return entry


# ----------------------------------------------------------------------- demo


def _demo_boot_sector() -> bytes:
    """A placeholder boot sector: minimal stub, zero BPB, valid signature.

    The stub halts and loops, so the image is bootable-but-idle in the emulator
    while the demo focuses on the image and filesystem structure.
    """
    boot = bytearray(SECTOR)
    # cli; hlt; jmp short -2
    boot[0:5] = b"\xFA\xF4\xEB\xFE\x90"
    boot[510:512] = b"\x55\xaa"
    return bytes(boot)


def _demo_kernel(sectors: int) -> bytes:
    """A recognisable filler kernel: a signature in the first sector."""
    body = bytearray(sectors * SECTOR)
    body[0:8] = b"MYOSKRNL"
    struct.pack_into("<H", body, 8, sectors)
    return bytes(body)


def _demo_duplicate_name() -> None:
    """Write the same name twice; the second call must be rejected."""
    volume = format_fat12_floppy()
    fat12_write_file(volume, "KERNEL.BIN", b"first")
    fat12_write_file(volume, "KERNEL.BIN", b"second")


def _demo() -> int:
    print("myos disk image demo")
    print(f"  sector size      : {SECTOR} bytes")
    print(f"  floppy geometry  : {floppy_geometry()}")

    boot = _demo_boot_sector()
    validate_boot_sector(boot)
    print(f"  boot sector      : {len(boot)} bytes, signature "
          f"{boot[510:512].hex(' ')}")

    # (a) raw floppy with the kernel at a fixed sector offset
    kernel = _demo_kernel(4)
    print(f"  kernel           : {len(kernel)} bytes = {kernel_sectors(kernel)} sectors")
    floppy = build_floppy_image(boot, kernel)
    print(f"  floppy image     : {len(floppy)} bytes ({len(floppy) // SECTOR} sectors)")
    print(f"  kernel at LBA 1  : {read_sector(floppy, 1)[0:8]!r}")

    # (b) FAT12 floppy with two files, then read the root directory back
    fat = format_fat12_floppy("MYOSBOOT")
    fat12_write_file(fat, "KERNEL.BIN", kernel)
    fat12_write_file(fat, "README.TXT", b"myos fat12 volume\n")
    layout = _parse_fat12_layout(fat)
    duplicates = sum(
        1 for copy in range(1, layout.fat_copies)
        if fat[layout.fat_start_sector * SECTOR:
               (layout.fat_start_sector + layout.fat_size) * SECTOR]
        != fat[(layout.fat_start_sector + copy * layout.fat_size) * SECTOR:
               (layout.fat_start_sector + (copy + 1) * layout.fat_size) * SECTOR]
    )
    print(f"  FAT12 floppy     : {len(fat)} bytes, volume label "
          f"{fat[43:54].decode('ascii')!r}, clusters={layout.count_of_clusters}")
    print(f"  FAT copies differ: {duplicates}")
    print("  root directory:")
    for entry in _root_directory(fat, layout):
        print(f"    {entry['slot']:>2}  {entry['name']:<12} "
              f"cluster={entry['cluster']:<4} size={entry['size']}")

    # (c) MBR-partitioned hard disk
    disk = build_hard_disk_image(boot, kernel, partition_lba=2048, active=0)
    print(f"  hard disk image  : {len(disk)} bytes ({len(disk) // SECTOR} sectors), "
          f"auto geometry {hard_disk_geometry(len(disk) // SECTOR)}")
    mbr = read_sector(disk, 0)
    print(f"  MBR signature    : {mbr[510:512].hex(' ')}")
    print("  partition table:")
    for entry in mbr_partition_entries(disk):
        print(f"    {entry['index']}  active={int(entry['active'])}  "
              f"type={entry['type']:#04x}  chs_start={entry['chs_start']}  "
              f"chs_end={entry['chs_end']}  lba_start={entry['lba_start']}  "
              f"sectors={entry['sectors']}")
    partition_sector = read_sector(disk, 2048)
    table = partition_sector[_MBR_PARTITION_TABLE_OFFSET:510]
    print(f"  partition table in partition sector: "
          f"{table.hex() if any(table) else '(all zero)'}")
    print(f"  partition signature: {partition_sector[510:512].hex(' ')}")
    print(f"  kernel at LBA 2049: {read_sector(disk, 2049)[0:8]!r}")

    # (d) a disk with a caller-chosen geometry that reaches the CHS limit
    small_disk = Geometry(512, 16, 63)
    sized = build_hard_disk_image(boot, kernel, partition_lba=2048, active=0,
                                  geometry=small_disk)
    sized_entry = mbr_partition_entries(sized)[0]
    print(f"  chosen geometry  : {len(sized)} bytes ({len(sized) // SECTOR} sectors), "
          f"geometry {small_disk}")
    print(f"    entry 0        : type={sized_entry['type']:#04x}  "
          f"chs_start={sized_entry['chs_start']}  chs_end={sized_entry['chs_end']}  "
          f"lba_start={sized_entry['lba_start']}  sectors={sized_entry['sectors']}")

    # (e) error paths
    for description, action in (
        ("480-byte boot sector",
         lambda: validate_boot_sector(boot[:480])),
        ("boot sector without 0x55AA",
         lambda: validate_boot_sector(bytes(512))),
        ("4000-sector kernel in a floppy",
         lambda: build_floppy_image(boot, bytes(4000 * SECTOR))),
        ("kernel offset past the floppy",
         lambda: build_floppy_image(boot, kernel, kernel_offset_sectors=2900)),
        ("partition index 4",
         lambda: build_hard_disk_image(boot, geometry=floppy_geometry(), active=4)),
        ("partition outside the geometry",
         lambda: build_hard_disk_image(boot, geometry=Geometry(1, 1, 18))),
        ("kernel larger than the geometry",
         lambda: build_hard_disk_image(boot, bytes(200 * SECTOR),
                                       geometry=Geometry(1, 16, 63))),
        ("file too large for the volume",
         lambda: fat12_write_file(format_fat12_floppy(), "BIG.BIN",
                                  bytes(2900 * SECTOR))),
        ("malformed 8.3 name",
         lambda: fat12_write_file(format_fat12_floppy(), "TOOLONGNAME.BIN", b"x")),
        ("duplicate file name",
         _demo_duplicate_name),
        ("missing MBR signature",
         lambda: mbr_partition_entries(bytes(1024))),
    ):
        try:
            action()
        except ImageError as error:
            print(f"  error path       : {description} -> {error}")
        else:
            print(f"  FAIL             : {description} did not raise ImageError",
                  file=sys.stderr)
            return 1

    print("demo completed")
    return 0


if __name__ == "__main__":
    sys.exit(_demo())
