"""myfs: the small filesystem myos keeps in a disk partition.

The format is deliberately plain, because two independent implementations have to
agree on it byte for byte: this tool (which builds the volume at build time) and
kernel32/fs.cpp (which mounts and reads it at run time).  A test compares the
constants in both places, the same way the boot info block is cross-checked.

Layout, little endian throughout, 512-byte blocks:

    block 0        superblock
    blocks 1..4    inode table: 64 inodes of 32 bytes
    block 5        bitmaps: inode bitmap (8 bytes), then block bitmap (256 bytes)
    blocks 6..     data blocks

An inode is 32 bytes: type (1), padding (3), size (4), then 12 block numbers
(2 bytes each) -- 11 direct blocks and one single-indirect block that holds 256
more numbers.  A file is therefore at most (11 + 256) * 512 = 136704 bytes.  A
directory is an inode of type 2 whose data blocks hold 32-byte dirents (a
29-character NUL-terminated name and a 2-byte inode number), 16 per block.

Everything that can go wrong is caught here at build time rather than turning into
a volume the kernel has to guess about: a name that does not fit, a file bigger
than the block budget, an inode or block that runs out, an empty directory tree.
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import image  # noqa: E402

# --------------------------------------------------------------------- format
# These constants are the format.  kernel32/fs.h states them again in C++ and
# tests/test_myfs.py compares the two definitions, because a silent disagreement
# here would look like a plausible volume full of garbage.

BLOCK_SIZE = 512
MAGIC = 0x5346594D                     # 'M','Y','F','S' little endian
VERSION = 1
BLOCK_COUNT = 2048                     # 1 MiB of volume
INODE_COUNT = 64
FIRST_DATA_BLOCK = 6
ROOT_INODE = 1                         # inode 0 is reserved and never allocated

INODE_SIZE = 32
DIRENT_SIZE = 32
NAME_MAX = 29                          # plus the NUL terminator, inside 30 bytes
DIRECT_BLOCKS = 11                     # then one single-indirect block
POINTERS_PER_BLOCK = BLOCK_SIZE // 2
MAX_FILE_BLOCKS = DIRECT_BLOCKS + POINTERS_PER_BLOCK
MAX_FILE_SIZE = MAX_FILE_BLOCKS * BLOCK_SIZE
ENTRIES_PER_BLOCK = BLOCK_SIZE // DIRENT_SIZE

INODE_TABLE_BLOCK = 1
BITMAP_BLOCK = 5
INODE_BITMAP_BYTES = INODE_COUNT // 8
BLOCK_BITMAP_BYTES = BLOCK_COUNT // 8
VOLUME_BYTES = BLOCK_COUNT * BLOCK_SIZE

TYPE_FREE = 0
TYPE_FILE = 1
TYPE_DIR = 2

STATE_DIRTY = 0
STATE_CLEAN = 1

LABEL_SIZE = 24
DEFAULT_LABEL = "myos"

# Where the volume sits inside a disk image.  The boot image and the user's data
# disk share this layout, so one parser -- the kernel's MBR walk -- finds the
# volume either way, and no code has to know a partition LBA except this file and
# build.py.  The scratch tail exists so that a device test can write and read back
# a sector without touching anything the filesystem owns.
PARTITION_INDEX = 1
PARTITION_LBA = 2048
PARTITION_SECTORS = BLOCK_COUNT
SCRATCH_LBA = PARTITION_LBA + PARTITION_SECTORS
SCRATCH_SECTORS = 8
DISK_SECTORS = SCRATCH_LBA + SCRATCH_SECTORS
PARTITION_TYPE = 0x7F                   # "myfs"; no standard type describes this

MANIFEST_NAME = "manifest"
MANIFEST_PATH = "/manifest"


class MyfsError(Exception):
    """A volume that cannot be built, read, or repaired as asked."""


# ---------------------------------------------------------------- little views


def checksum(data: bytes) -> int:
    """The checksum the kernel computes too: h = h * 31 + byte, 32 bits.

    Written down once, here, and again in kernel32/kernel.cpp.  It is not a
    cryptographic digest and does not try to be; it is a cheap way for two
    implementations to prove they read the same bytes -- the manifest, and the
    staged copy of the kernel image the loader read off the disk.
    """
    value = 0
    for byte in data:
        value = (value * 31 + byte) & 0xFFFFFFFF
    return value


def _split_path(path: str) -> list[str]:
    """Components of a slash-separated path, ignoring empty ones."""
    return [part for part in path.split("/") if part]


def _bit(bitmap: bytearray, index: int) -> bool:
    return bool(bitmap[index // 8] & (1 << (index % 8)))


def _set_bit(bitmap: bytearray, index: int, value: bool) -> None:
    mask = 1 << (index % 8)
    if value:
        bitmap[index // 8] |= mask
    else:
        bitmap[index // 8] &= ~mask & 0xFF


@dataclass
class Inode:
    """One 32-byte inode, in the shape the kernel uses."""

    index: int
    type: int = TYPE_FREE
    size: int = 0
    blocks: list[int] = field(default_factory=lambda: [0] * (DIRECT_BLOCKS + 1))

    @property
    def direct(self) -> list[int]:
        return self.blocks[:DIRECT_BLOCKS]

    @property
    def indirect(self) -> int:
        return self.blocks[DIRECT_BLOCKS]

    @indirect.setter
    def indirect(self, block: int) -> None:
        self.blocks[DIRECT_BLOCKS] = block

    @property
    def is_dir(self) -> bool:
        return self.type == TYPE_DIR

    @property
    def is_file(self) -> bool:
        return self.type == TYPE_FILE


class Volume:
    """A myfs volume, in memory, as bytes plus the views over them.

    Nothing here caches state: every accessor reads the bytes it needs, so the
    object is exactly the volume and `bytes(volume.data)` is what goes on disk.
    """

    def __init__(self, data: Optional[bytes | bytearray] = None, *,
                 block_count: int = BLOCK_COUNT, label: str = DEFAULT_LABEL,
                 state: int = STATE_CLEAN) -> None:
        if data is None:
            self._format(block_count, label, state)
        else:
            raw = bytes(data)
            if len(raw) % BLOCK_SIZE != 0:
                raise MyfsError(
                    f"volume is {len(raw)} bytes, which is not a whole number of "
                    f"{BLOCK_SIZE}-byte blocks"
                )
            self.data = bytearray(raw)
            self._validate_header()

    # ------------------------------------------------------------- formatting

    def _format(self, block_count: int, label: str, state: int) -> None:
        if block_count < FIRST_DATA_BLOCK + 1:
            raise MyfsError(
                f"a volume needs more than {FIRST_DATA_BLOCK} blocks to hold its "
                f"own metadata, got {block_count}"
            )
        if block_count % 8 != 0:
            raise MyfsError(f"block count {block_count} must be a multiple of 8")
        if block_count > (BLOCK_SIZE - INODE_BITMAP_BYTES) * 8:
            raise MyfsError(
                f"block count {block_count} does not fit in one {BLOCK_SIZE}-byte "
                "bitmap block"
            )
        if len(label.encode("ascii", "replace")) >= LABEL_SIZE:
            raise MyfsError(f"label {label!r} does not fit in {LABEL_SIZE} bytes")

        self.data = bytearray(block_count * BLOCK_SIZE)
        self._write_super({
            "magic": MAGIC,
            "version": VERSION,
            "block_size": BLOCK_SIZE,
            "block_count": block_count,
            "inode_count": INODE_COUNT,
            "first_data_block": FIRST_DATA_BLOCK,
            "free_blocks": block_count - FIRST_DATA_BLOCK,
            "free_inodes": INODE_COUNT - (ROOT_INODE + 1),
            "root_inode": ROOT_INODE,
            "state": state,
        })
        self._write_label(label)

        # Metadata blocks are used from the outset, and inodes 0 and 1 are taken:
        # 0 is reserved so that a zeroed dirent is never a valid inode, and 1 is the
        # root directory.  Forgetting the root here means allocate_inode hands it
        # out again, and the root ends up pointing at itself.
        blocks = self.block_bitmap()
        for block in range(FIRST_DATA_BLOCK):
            _set_bit(blocks, block, True)
        self.write_block_bitmap(blocks)
        inodes = self.inode_bitmap()
        for index in range(ROOT_INODE + 1):
            _set_bit(inodes, index, True)
        self.write_inode_bitmap(inodes)

        root = Inode(index=ROOT_INODE, type=TYPE_DIR, size=0)
        self.write_inode(root)

    def _validate_header(self) -> None:
        head = self._read_super()
        if head["magic"] != MAGIC:
            raise MyfsError(
                f"volume magic is {head['magic']:#010x}, expected {MAGIC:#010x} "
                "('MYFS')"
            )
        if head["version"] != VERSION:
            raise MyfsError(
                f"volume version is {head['version']}, this tool writes {VERSION}"
            )
        if head["block_size"] != BLOCK_SIZE:
            raise MyfsError(
                f"volume block size is {head['block_size']}, expected {BLOCK_SIZE}"
            )
        if head["block_count"] * BLOCK_SIZE != len(self.data):
            raise MyfsError(
                f"the superblock says {head['block_count']} blocks but the volume "
                f"holds {len(self.data) // BLOCK_SIZE}"
            )

    # ------------------------------------------------------------ superblock

    _SUPER_LAYOUT = "<10I24s448s"

    def _read_super(self) -> dict:
        magic, version, block_size, block_count, inode_count, first_data_block, \
            free_blocks, free_inodes, root_inode, state, label, _ = \
            struct.unpack_from(self._SUPER_LAYOUT, self.data, 0)
        return {
            "magic": magic, "version": version, "block_size": block_size,
            "block_count": block_count, "inode_count": inode_count,
            "first_data_block": first_data_block, "free_blocks": free_blocks,
            "free_inodes": free_inodes, "root_inode": root_inode, "state": state,
            "label": label.split(b"\x00")[0].decode("ascii", "replace"),
        }

    def _write_super(self, fields: dict) -> None:
        label = bytes(fields.get("label", DEFAULT_LABEL), "ascii")
        struct.pack_into(
            self._SUPER_LAYOUT, self.data, 0,
            fields["magic"], fields["version"], fields["block_size"],
            fields["block_count"], fields["inode_count"], fields["first_data_block"],
            fields["free_blocks"], fields["free_inodes"], fields["root_inode"],
            fields["state"], label, bytes(448),
        )

    def _write_label(self, label: str) -> None:
        start = 40
        self.data[start:start + LABEL_SIZE] = bytes(LABEL_SIZE)
        self.data[start:start + len(label)] = bytes(label, "ascii")

    @property
    def block_count(self) -> int:
        return len(self.data) // BLOCK_SIZE

    @property
    def inode_count(self) -> int:
        return self._read_super()["inode_count"]

    @property
    def label(self) -> str:
        return self._read_super()["label"]

    @property
    def state(self) -> int:
        return self._read_super()["state"]

    @property
    def free_blocks(self) -> int:
        return self._read_super()["free_blocks"]

    @property
    def free_inodes(self) -> int:
        return self._read_super()["free_inodes"]

    def set_free_counts(self, free_blocks: int, free_inodes: int) -> None:
        struct.pack_into("<I", self.data, 24, free_blocks)
        struct.pack_into("<I", self.data, 28, free_inodes)

    def set_state(self, state: int) -> None:
        struct.pack_into("<I", self.data, 36, state)

    # ---------------------------------------------------------------- blocks

    def block(self, index: int) -> bytes:
        if not 0 <= index < self.block_count:
            raise MyfsError(f"block {index} is outside the volume")
        return bytes(self.data[index * BLOCK_SIZE:(index + 1) * BLOCK_SIZE])

    def write_block(self, index: int, data: bytes) -> None:
        if not 0 <= index < self.block_count:
            raise MyfsError(f"block {index} is outside the volume")
        if len(data) > BLOCK_SIZE:
            raise MyfsError(
                f"{len(data)} bytes do not fit in one {BLOCK_SIZE}-byte block"
            )
        start = index * BLOCK_SIZE
        self.data[start:start + BLOCK_SIZE] = bytes(data).ljust(BLOCK_SIZE, b"\x00")

    def block_bitmap(self) -> bytearray:
        start = BITMAP_BLOCK * BLOCK_SIZE + INODE_BITMAP_BYTES
        return bytearray(self.data[start:start + BLOCK_BITMAP_BYTES])

    def write_block_bitmap(self, bitmap: bytearray) -> None:
        start = BITMAP_BLOCK * BLOCK_SIZE + INODE_BITMAP_BYTES
        self.data[start:start + BLOCK_BITMAP_BYTES] = bytes(bitmap)

    def inode_bitmap(self) -> bytearray:
        start = BITMAP_BLOCK * BLOCK_SIZE
        return bytearray(self.data[start:start + INODE_BITMAP_BYTES])

    def write_inode_bitmap(self, bitmap: bytearray) -> None:
        start = BITMAP_BLOCK * BLOCK_SIZE
        self.data[start:start + INODE_BITMAP_BYTES] = bytes(bitmap)

    def block_is_used(self, index: int) -> bool:
        return _bit(self.block_bitmap(), index)

    def inode_is_used(self, index: int) -> bool:
        return _bit(self.inode_bitmap(), index)

    def allocate_block(self) -> int:
        bitmap = self.block_bitmap()
        first = self._read_super()["first_data_block"]
        for index in range(first, self.block_count):
            if not _bit(bitmap, index):
                _set_bit(bitmap, index, True)
                self.write_block_bitmap(bitmap)
                self.set_free_counts(self.free_blocks - 1, self.free_inodes)
                return index
        raise MyfsError("the volume has no free blocks left")

    def release_block(self, index: int) -> None:
        if not self._read_super()["first_data_block"] <= index < self.block_count:
            raise MyfsError(f"block {index} is not a data block")
        bitmap = self.block_bitmap()
        if not _bit(bitmap, index):
            raise MyfsError(f"block {index} was already free")
        _set_bit(bitmap, index, False)
        self.write_block_bitmap(bitmap)
        self.set_free_counts(self.free_blocks + 1, self.free_inodes)

    # ---------------------------------------------------------------- inodes

    _INODE_LAYOUT = "<B3sI12H"

    def inode_offset(self, index: int) -> int:
        if not 0 <= index < self.inode_count:
            raise MyfsError(
                f"inode {index} is outside the {self.inode_count}-inode table"
            )
        return INODE_TABLE_BLOCK * BLOCK_SIZE + index * INODE_SIZE

    def read_inode(self, index: int) -> Inode:
        offset = self.inode_offset(index)
        type_, _pad, size, *blocks = struct.unpack_from(self._INODE_LAYOUT, self.data, offset)
        return Inode(index=index, type=type_, size=size, blocks=list(blocks))

    def write_inode(self, inode: Inode) -> None:
        struct.pack_into(
            self._INODE_LAYOUT, self.data, self.inode_offset(inode.index),
            inode.type, bytes(3), inode.size, *inode.blocks,
        )

    def allocate_inode(self, type_: int) -> Inode:
        bitmap = self.inode_bitmap()
        # Inode 0 is reserved: bit 0 is set at format time and never cleared.
        for index in range(1, self.inode_count):
            if not _bit(bitmap, index):
                _set_bit(bitmap, index, True)
                self.write_inode_bitmap(bitmap)
                self.set_free_counts(self.free_blocks, self.free_inodes - 1)
                inode = Inode(index=index, type=type_)
                self.write_inode(inode)
                return inode
        raise MyfsError(f"the volume has no free inodes left ({self.inode_count} exist)")

    def release_inode(self, index: int) -> None:
        bitmap = self.inode_bitmap()
        if not _bit(bitmap, index):
            raise MyfsError(f"inode {index} was already free")
        _set_bit(bitmap, index, False)
        self.write_inode_bitmap(bitmap)
        self.set_free_counts(self.free_blocks, self.free_inodes + 1)
        self.write_inode(Inode(index=index))

    def block_pointers(self, inode: Inode) -> list[int]:
        """Every block the inode references, direct list first."""
        pointers = [block for block in inode.direct if block != 0]
        if inode.indirect != 0:
            raw = self.block(inode.indirect)
            for offset in range(0, BLOCK_SIZE, 2):
                (block,) = struct.unpack_from("<H", raw, offset)
                if block != 0:
                    pointers.append(block)
        return pointers

    # ------------------------------------------------------------ file data

    def _map_block(self, inode: Inode, index: int, *, create: bool) -> int:
        """Physical block holding logical block ``index`` of ``inode``.

        With ``create`` the blocks are allocated on the way, which is what the
        write path needs; otherwise an unmapped block comes back as 0.
        """
        if index < DIRECT_BLOCKS:
            if inode.blocks[index] == 0 and create:
                inode.blocks[index] = self.allocate_block()
                self.write_inode(inode)
            return inode.blocks[index]
        if inode.indirect == 0:
            if not create:
                return 0
            inode.indirect = self.allocate_block()
            self.write_block(inode.indirect, bytes(BLOCK_SIZE))
            self.write_inode(inode)
        entry = index - DIRECT_BLOCKS
        if entry >= POINTERS_PER_BLOCK:
            raise MyfsError(
                f"logical block {index} is past the {MAX_FILE_BLOCKS}-block limit"
            )
        raw = bytearray(self.block(inode.indirect))
        (block,) = struct.unpack_from("<H", raw, entry * 2)
        if block == 0 and create:
            block = self.allocate_block()
            struct.pack_into("<H", raw, entry * 2, block)
            self.write_block(inode.indirect, bytes(raw))
        return block

    def read_bytes(self, inode: Inode, offset: int = 0,
                   count: Optional[int] = None) -> bytes:
        """The bytes an inode's data blocks hold, files and directories alike."""
        if count is None:
            count = inode.size - offset
        if offset > inode.size:
            return b""
        count = min(count, inode.size - offset)
        out = bytearray()
        while count > 0:
            logical = offset // BLOCK_SIZE
            within = offset % BLOCK_SIZE
            chunk = min(count, BLOCK_SIZE - within)
            block = self._map_block(inode, logical, create=False)
            data = self.block(block) if block != 0 else bytes(BLOCK_SIZE)
            out += data[within:within + chunk]
            offset += chunk
            count -= chunk
        return bytes(out)

    def read_file(self, inode: Inode, offset: int = 0,
                  count: Optional[int] = None) -> bytes:
        """A file's bytes, or as many as exist when ``count`` runs past the end."""
        if inode.type != TYPE_FILE:
            raise MyfsError(f"inode {inode.index} is not a file")
        return self.read_bytes(inode, offset, count)

    def write_file(self, inode: Inode, data: bytes, offset: int = 0,
                   truncate: bool = False) -> None:
        """Write ``data`` at ``offset``, growing the file and its blocks.

        With ``truncate`` the file ends where the write does, which is what an
        overwrite means: the bytes after the new end are released rather than left
        behind under a longer size.
        """
        if inode.type != TYPE_FILE:
            raise MyfsError(f"inode {inode.index} is not a file")
        if offset + len(data) > MAX_FILE_SIZE:
            raise MyfsError(
                f"writing {len(data)} bytes at {offset} needs more than the "
                f"{MAX_FILE_SIZE}-byte limit of one file"
            )
        remaining = memoryview(bytes(data))
        position = offset
        while len(remaining) > 0:
            logical = position // BLOCK_SIZE
            within = position % BLOCK_SIZE
            chunk = min(len(remaining), BLOCK_SIZE - within)
            block = self._map_block(inode, logical, create=True)
            raw = bytearray(self.block(block))
            raw[within:within + chunk] = bytes(remaining[:chunk])
            self.write_block(block, bytes(raw))
            remaining = remaining[chunk:]
            position += chunk
        if truncate:
            self._release_from(inode, position)
        if truncate or position > inode.size:
            inode.size = position
        self.write_inode(inode)

    def truncate_file(self, inode: Inode, size: int = 0) -> None:
        """Release every block at or past ``size`` and set the size to it.

        ``truncate_file(inode)`` is therefore "empty this file", and a shorter
        overwrite uses the same path: keeping the old blocks would leave the file
        reporting its old length with stale bytes behind it.
        """
        if inode.type != TYPE_FILE:
            raise MyfsError(f"inode {inode.index} is not a file")
        if size > inode.size:
            raise MyfsError(
                f"truncating inode {inode.index} from {inode.size} to {size} would "
                "grow it; use write_file for that"
            )
        self._release_from(inode, size)
        inode.size = size
        self.write_inode(inode)

    def _release_from(self, inode: Inode, size: int) -> None:
        """Release every block that holds byte ``size`` of ``inode`` or later."""
        first_free = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
        for logical in range(first_free, DIRECT_BLOCKS):
            if inode.blocks[logical] != 0:
                self.release_block(inode.blocks[logical])
                inode.blocks[logical] = 0
        if inode.indirect != 0:
            raw = bytearray(self.block(inode.indirect))
            for entry in range(POINTERS_PER_BLOCK):
                if DIRECT_BLOCKS + entry < first_free:
                    continue
                (block,) = struct.unpack_from("<H", raw, entry * 2)
                if block != 0:
                    self.release_block(block)
                    struct.pack_into("<H", raw, entry * 2, 0)
            self.write_block(inode.indirect, bytes(raw))
            if first_free <= DIRECT_BLOCKS:
                self.release_block(inode.indirect)
                inode.indirect = 0

    # --------------------------------------------------------- directories

    def dir_entries(self, inode: Inode) -> list[tuple[str, int]]:
        if not inode.is_dir:
            raise MyfsError(f"inode {inode.index} is not a directory")
        if inode.size % DIRENT_SIZE != 0:
            raise MyfsError(
                f"directory inode {inode.index} has size {inode.size}, which is not a "
                f"multiple of {DIRENT_SIZE}"
            )
        entries: list[tuple[str, int]] = []
        raw = self.read_bytes(inode) if inode.size else b""
        for offset in range(0, len(raw), DIRENT_SIZE):
            name, child = struct.unpack_from("<30sH", raw, offset)
            if name[0] == 0:
                continue                       # a hole left by a removed entry
            entries.append((name.split(b"\x00")[0].decode("ascii", "replace"), child))
        return entries

    def dir_add(self, inode: Inode, name: str, child: int) -> None:
        if inode.type != TYPE_DIR:
            raise MyfsError(f"inode {inode.index} is not a directory")
        encoded = name.encode("ascii", "replace")
        if len(encoded) > NAME_MAX:
            raise MyfsError(
                f"name {name!r} is {len(encoded)} bytes, the limit is {NAME_MAX}"
            )
        for existing, _ in self.dir_entries(inode):
            if existing == name:
                raise MyfsError(f"{name!r} already exists in inode {inode.index}")
        entry = struct.pack("<30sH", encoded, child)
        raw = bytearray(self.read_bytes(inode)) if inode.size else bytearray()
        # A slot left by dir_remove is reused before the directory grows.
        for offset in range(0, len(raw), DIRENT_SIZE):
            if raw[offset] == 0:
                raw[offset:offset + DIRENT_SIZE] = entry
                self._write_dir_bytes(inode, raw)
                return
        raw += entry
        self._write_dir_bytes(inode, raw)

    def _write_dir_bytes(self, inode: Inode, raw: bytes) -> None:
        """Replace a directory's contents, keeping its block count honest.

        Blocks are only ever added: a directory that shrinks keeps the block that
        held the removed entry, which is why dir_remove only zeroes the slot.  A
        shorter ``size`` then hides it, and the next dir_add reuses it.
        """
        needed = (len(raw) + BLOCK_SIZE - 1) // BLOCK_SIZE
        offset = 0
        for logical in range(needed):
            block = self._map_block(inode, logical, create=True)
            self.write_block(block, bytes(raw[offset:offset + BLOCK_SIZE]))
            offset += BLOCK_SIZE
        inode.size = len(raw)
        self.write_inode(inode)

    def dir_remove(self, inode: Inode, name: str) -> int:
        raw = bytearray(self.read_bytes(inode)) if inode.size else bytearray()
        for offset in range(0, len(raw), DIRENT_SIZE):
            entry_name, child = struct.unpack_from("<30sH", raw, offset)
            if entry_name.split(b"\x00")[0].decode("ascii", "replace") == name:
                raw[offset:offset + DIRENT_SIZE] = bytes(DIRENT_SIZE)
                self._write_dir_bytes(inode, raw)
                return child
        raise MyfsError(f"{name!r} is not in inode {inode.index}")

    def lookup(self, inode: Inode, name: str) -> int:
        for existing, child in self.dir_entries(inode):
            if existing == name:
                return child
        raise MyfsError(f"{name!r} not found in inode {inode.index}")

    def resolve(self, path: str) -> Inode:
        """Walk a path from the root, one component at a time."""
        current = self.read_inode(self._read_super()["root_inode"])
        for part in _split_path(path):
            if part == ".":
                continue
            if part == "..":
                raise MyfsError("'..' is not supported: myfs keeps no parent links")
            if not current.is_dir:
                raise MyfsError(f"{part!r} is inside a non-directory")
            current = self.read_inode(self.lookup(current, part))
        return current

    # ------------------------------------------------------------- creation

    def create_file(self, path: str, data: bytes = b"") -> Inode:
        parts = _split_path(path)
        if not parts:
            raise MyfsError("a file needs a name")
        parent = self.resolve("/".join(parts[:-1]))
        if not parent.is_dir:
            raise MyfsError(f"{parts[-1]!r} is inside a non-directory")
        self._reject_existing(parent, parts[-1])
        inode = self.allocate_inode(TYPE_FILE)
        try:
            if data:
                self.write_file(inode, data)
            self.dir_add(parent, parts[-1], inode.index)
        except MyfsError:
            # Leave nothing behind: half-created files are how a crash test turns
            # into a permanently full volume.
            self.truncate_file(inode)
            self.release_inode(inode.index)
            raise
        return inode

    def make_directory(self, path: str) -> Inode:
        parts = _split_path(path)
        if not parts:
            raise MyfsError("a directory needs a name")
        parent = self.resolve("/".join(parts[:-1]))
        if not parent.is_dir:
            raise MyfsError(f"{parts[-1]!r} is inside a non-directory")
        self._reject_existing(parent, parts[-1])
        inode = self.allocate_inode(TYPE_DIR)
        try:
            self.dir_add(parent, parts[-1], inode.index)
        except MyfsError:
            self.release_inode(inode.index)
            raise
        return inode

    def _reject_existing(self, directory: Inode, name: str) -> None:
        for existing, _ in self.dir_entries(directory):
            if existing == name:
                raise MyfsError(f"{name!r} already exists in inode {directory.index}")

    def remove(self, path: str) -> None:
        parts = _split_path(path)
        if not parts:
            raise MyfsError("a name is needed")
        parent = self.resolve("/".join(parts[:-1]))
        child = self.read_inode(self.lookup(parent, parts[-1]))
        if child.is_dir and self.dir_entries(child):
            raise MyfsError(f"{parts[-1]!r} is not empty")
        if child.is_file:
            self.truncate_file(child)
        else:
            for block in self.block_pointers(child):
                self.release_block(block)
            if child.indirect != 0:
                self.release_block(child.indirect)
        self.dir_remove(parent, parts[-1])
        self.release_inode(child.index)

    # -------------------------------------------------------------- traversal

    def iter_files(self) -> Iterator[tuple[str, Inode]]:
        """Every file and directory, depth first, path relative to the root.

        Directories are visited once: a volume with a cycle in it (which is what a
        root that lists itself produces) would otherwise make every listing loop
        forever, and hanging is a worse failure than a bad listing.
        """
        stack: list[tuple[str, Inode]] = [("", self.read_inode(self._read_super()["root_inode"]))]
        seen: set[int] = set()
        while stack:
            prefix, inode = stack.pop()
            if not inode.is_dir or inode.index in seen:
                continue
            seen.add(inode.index)
            for name, child_index in sorted(self.dir_entries(inode)):
                child = self.read_inode(child_index)
                path = f"{prefix}{name}"
                if child.is_dir:
                    stack.append((f"{path}/", child))
                yield path, child

    def manifest_text(self) -> str:
        """The manifest the kernel checks itself against, one line per file."""
        lines = []
        for path, inode in sorted(self.iter_files()):
            if inode.is_file:
                data = self.read_file(inode)
                lines.append(f"{path} {inode.size} {checksum(data):08X}")
        return "".join(line + "\n" for line in lines)

    # ------------------------------------------------------------- diagnosis

    def check(self) -> list[str]:
        """Every structural invariant this tool can state, as a list of problems.

        The kernel mounts volumes this tool built, so the obvious failure mode is
        the two disagreeing about what a valid volume looks like.  These checks are
        therefore written from the on-disk bytes alone, not from the in-memory
        bookkeeping that produced them.
        """
        problems: list[str] = []
        head = self._read_super()
        if head["first_data_block"] != FIRST_DATA_BLOCK:
            problems.append(
                f"superblock says the first data block is {head['first_data_block']}, "
                f"this tool writes {FIRST_DATA_BLOCK}"
            )
        if head["inode_count"] != INODE_COUNT:
            problems.append(
                f"superblock says {head['inode_count']} inodes, this tool writes "
                f"{INODE_COUNT}"
            )
        if head["root_inode"] >= INODE_COUNT:
            problems.append(
                f"the root inode {head['root_inode']} is outside the inode table"
            )
        root = self.read_inode(head["root_inode"])
        if not root.is_dir:
            problems.append(f"inode {root.index} is the root but is not a directory")

        referenced: set[int] = set()
        # Inode 0 starts at 1: it is reserved (never allocated, so that a zeroed
        # dirent is never a valid inode) and therefore has no type and no blocks.
        for index in range(1, self.inode_count):
            if not self.inode_is_used(index):
                continue
            inode = self.read_inode(index)
            if inode.type == TYPE_FREE:
                problems.append(
                    f"inode {index} is marked used in the bitmap but free in the table"
                )
                continue
            if inode.type not in (TYPE_FILE, TYPE_DIR):
                problems.append(f"inode {index} has unknown type {inode.type}")
                continue
            if inode.is_file and inode.size > MAX_FILE_SIZE:
                problems.append(
                    f"inode {index} says {inode.size} bytes, over the {MAX_FILE_SIZE} "
                    "byte limit"
                )
            if inode.is_dir and inode.size % DIRENT_SIZE != 0:
                problems.append(
                    f"directory inode {index} has size {inode.size}, not a multiple "
                    f"of {DIRENT_SIZE}"
                )
            for block in self.block_pointers(inode):
                referenced.add(block)
            if inode.indirect:
                referenced.add(inode.indirect)
        free_inodes = sum(
            1 for index in range(self.inode_count) if not self.inode_is_used(index)
        )
        if free_inodes != head["free_inodes"]:
            problems.append(
                f"the superblock says {head['free_inodes']} free inodes, the bitmap "
                f"says {free_inodes}"
            )

        bitmap = self.block_bitmap()
        marked = sum(1 for index in range(self.block_count) if _bit(bitmap, index))
        for block in sorted(referenced):
            if not self.block_is_used(block):
                problems.append(
                    f"block {block} is referenced by an inode but marked free"
                )
        leaked = sorted(
            index for index in range(FIRST_DATA_BLOCK, self.block_count)
            if self.block_is_used(index) and index not in referenced
        )
        if leaked:
            problems.append(
                f"{len(leaked)} block(s) marked used but referenced by nothing: "
                + ", ".join(str(block) for block in leaked[:8])
                + (" ..." if len(leaked) > 8 else "")
            )
        free_blocks = self.block_count - marked
        if free_blocks != head["free_blocks"]:
            problems.append(
                f"the superblock says {head['free_blocks']} free blocks, the bitmap "
                f"says {free_blocks}"
            )

        # Every reachable entry must point at an inode that exists and is used, and
        # every allocated inode must be reachable: an orphan is either a leak or a
        # mistake in the directory tree, and both are worth naming.
        reachable = {head["root_inode"]}
        for path, inode in self.iter_files():
            reachable.add(inode.index)
            if inode.type == TYPE_FREE:
                problems.append(f"{path} points at free inode {inode.index}")
            elif inode.index >= INODE_COUNT or not self.inode_is_used(inode.index):
                problems.append(f"{path} points at unallocated inode {inode.index}")
        for index in range(1, self.inode_count):
            if self.inode_is_used(index) and index not in reachable:
                problems.append(
                    f"inode {index} is allocated but not reachable from the root"
                )
        return problems

    # --------------------------------------------------------------- output

    def to_bytes(self) -> bytes:
        return bytes(self.data)


def build_volume(files_dir: Path, *, block_count: int = BLOCK_COUNT,
                 label: str = DEFAULT_LABEL,
                 leak_blocks: int = 0) -> Volume:
    """Pack a directory tree into a fresh volume, then add the manifest.

    Directories are created before the files that live in them, so a directory
    inode is always reachable by the time something is put inside it.  The
    manifest is written last and is not part of itself.
    """
    files_dir = Path(files_dir)
    if not files_dir.is_dir():
        raise MyfsError(f"{files_dir} is not a directory")

    volume = Volume(block_count=block_count, label=label)

    def walk(directory: Path, prefix: str) -> None:
        entries = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            if len(name.encode("ascii", "replace")) > NAME_MAX:
                raise MyfsError(
                    f"{entry} has a {len(name)}-byte name, the limit is {NAME_MAX}"
                )
            path = f"{prefix}{name}"
            if entry.is_dir():
                volume.make_directory(path)
                walk(entry, f"{path}/")
            elif entry.is_file():
                volume.create_file(path, entry.read_bytes())
            else:
                raise MyfsError(f"{entry} is neither a file nor a directory")

    walk(files_dir, "")

    manifest = volume.manifest_text()
    if not manifest:
        raise MyfsError(f"{files_dir} holds no files to pack")
    volume.create_file(MANIFEST_PATH, manifest.encode("ascii"))

    if leak_blocks:
        # A deliberately inconsistent volume, for the fsck tests: blocks that are
        # marked used but that no inode references, which is exactly what a power
        # cut in the middle of a write leaves behind.
        for _ in range(leak_blocks):
            volume.allocate_block()
    return volume


def volume_from_image(path: Path, partition_index: int) -> Volume:
    """Read the myfs volume out of one partition of a disk image."""
    data = Path(path).read_bytes()
    entries = image.mbr_partition_entries(data)
    if not 0 <= partition_index < len(entries):
        raise MyfsError(
            f"{path} has no partition {partition_index} (it declares "
            f"{len(entries)} entries)"
        )
    entry = entries[partition_index]
    if entry["sectors"] == 0:
        raise MyfsError(f"partition {partition_index} of {path} is unused")
    start = entry["lba_start"] * image.SECTOR
    end = start + entry["sectors"] * image.SECTOR
    if end > len(data):
        raise MyfsError(
            f"partition {partition_index} of {path} runs to byte {end}, past the "
            f"{len(data)}-byte image"
        )
    return Volume(data[start:end])


def build_disk_image(volume: Volume) -> bytes:
    """A whole disk carrying one myfs partition and no boot loader.

    This is the shape of the user's data disk: the kernel does not care whether
    the disk it was booted from is the one with the volume, only that an MBR
    declares a partition of type :data:`PARTITION_TYPE`.
    """
    if volume.block_count != PARTITION_SECTORS:
        raise MyfsError(
            f"the volume has {volume.block_count} blocks but partition "
            f"{PARTITION_INDEX} reserves {PARTITION_SECTORS}"
        )
    geometry = image.hard_disk_geometry(DISK_SECTORS)
    disk = bytearray(geometry.size_bytes)
    disk[image.SECTOR - 2:image.SECTOR] = b"\x55\xaa"    # a valid, non-bootable MBR
    image.write_partition_entry(
        disk, PARTITION_INDEX, lba_start=PARTITION_LBA, sectors=PARTITION_SECTORS,
        part_type=PARTITION_TYPE, geometry=geometry,
    )
    start = PARTITION_LBA * image.SECTOR
    disk[start:start + volume.block_count * BLOCK_SIZE] = volume.to_bytes()
    return bytes(disk)


def write_data_disk(path: Path, *, files_dir: Optional[Path] = None,
                    label: str = DEFAULT_LABEL, leak_blocks: int = 0) -> Volume:
    """Create the user's data disk, if it is not there already.

    Called by `run.py` and by the CLI.  It is never called by build.py: the images
    a build produces are reproducible and disposable, and this file is where the
    user's own files live, so a build must not overwrite it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if files_dir is not None:
        volume = build_volume(Path(files_dir), label=label, leak_blocks=leak_blocks)
    else:
        volume = Volume(label=label)
    path.write_bytes(build_disk_image(volume))
    return volume


# ------------------------------------------------------------------------ CLI


def _open_source(args: argparse.Namespace) -> Volume:
    if args.image is not None:
        return volume_from_image(args.image, args.partition)
    try:
        return Volume(Path(args.volume).read_bytes())
    except MyfsError as error:
        if "magic" not in str(error):
            raise
        # A whole disk image passed as --volume is the likely mistake, and saying
        # so beats "the magic is wrong".
        raise MyfsError(
            f"{args.volume} does not start with a myfs superblock; if it is a whole "
            "disk image, use --image with --partition instead"
        ) from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="build, list, extract and check myfs volumes",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--volume", metavar="FILE",
                        help="a volume image (just the myfs blocks)")
    source.add_argument("--image", metavar="FILE",
                        help="a whole disk image; use --partition to pick the volume")
    source.add_argument("--build", metavar="DIR",
                        help="build a fresh volume from a directory tree")
    source.add_argument("--create-data-disk", metavar="FILE",
                        help="write a whole disk holding one fresh myfs partition")

    parser.add_argument("--partition", type=int, default=PARTITION_INDEX,
                        help=f"partition index inside --image "
                             f"(default {PARTITION_INDEX})")
    parser.add_argument("--out", metavar="FILE",
                        help="where --build writes the volume, and --extract the file")
    parser.add_argument("--list", action="store_true", help="list the volume")
    parser.add_argument("--extract", metavar="NAME", help="write one file out")
    parser.add_argument("--check", action="store_true",
                        help="verify every structural invariant")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="volume label")
    parser.add_argument("--files", metavar="DIR",
                        help="directory tree to pack into --create-data-disk")
    parser.add_argument("--blocks", type=int, default=BLOCK_COUNT,
                        help=f"block count for --build (default {BLOCK_COUNT})")
    parser.add_argument("--leak-blocks", type=int, default=0, metavar="N",
                        help="build with N blocks marked used but referenced by "
                             "nothing (a deliberate power-cut fixture)")
    parser.add_argument("--dirty", action="store_true",
                        help="build the volume with its state flag saying it was "
                             "never unmounted cleanly")
    args = parser.parse_args(argv)

    try:
        if args.build is not None:
            volume = build_volume(Path(args.build), block_count=args.blocks,
                                  label=args.label, leak_blocks=args.leak_blocks)
            if args.dirty:
                volume.set_state(STATE_DIRTY)
            if args.out is None:
                parser.error("--build needs --out")
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_bytes(volume.to_bytes())
            print(f"wrote {args.out}: {volume.block_count} blocks, "
                  f"{volume.free_blocks} free, {volume.free_inodes} free inodes")
            return 0

        if args.create_data_disk is not None:
            volume = write_data_disk(Path(args.create_data_disk),
                                     files_dir=Path(args.files) if args.files else None,
                                     label=args.label, leak_blocks=args.leak_blocks)
            print(f"wrote {args.create_data_disk}: myfs in partition "
                  f"{PARTITION_INDEX} at LBA {PARTITION_LBA}, {volume.block_count} "
                  f"blocks, {volume.free_inodes} free inodes")
            return 0

        volume = _open_source(args)
        if args.list or (args.extract is None and not args.check):
            for path, inode in sorted(volume.iter_files()):
                kind = "dir " if inode.is_dir else "file"
                print(f"  {kind} {path}{'/' if inode.is_dir else ''} "
                      f"({inode.size} bytes, inode {inode.index})")
            print(f"{volume.label!r}: {volume.block_count} blocks, "
                  f"{volume.free_blocks} free, {volume.free_inodes} free inodes, "
                  f"state {'clean' if volume.state == STATE_CLEAN else 'dirty'}")
        if args.extract is not None:
            if args.out is None:
                parser.error("--extract needs --out")
            inode = volume.resolve(args.extract)
            data = volume.read_file(inode) if inode.is_file else b""
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_bytes(data)
            print(f"wrote {args.out}: {len(data)} bytes")
        if args.check:
            problems = volume.check()
            if problems:
                print(f"{len(problems)} problem(s):")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print("clean: every invariant holds")
        return 0
    except (MyfsError, image.ImageError, OSError) as error:
        print(f"myfs: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
