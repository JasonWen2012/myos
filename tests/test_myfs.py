"""
Tests for myfs: the format, the host-side packer, and the disk layout it produces.

Two things are being checked here.  The first is that the packer builds a volume
that satisfies every invariant the format states -- free counts, bitmaps, block
ownership, reachability.  The second is that the Python definition of the format
and the C++ one in kernel32/fs.h agree, because a silent disagreement between them
would not look like a bug: it would look like a kernel reading a plausible volume
full of garbage.
"""

from __future__ import annotations

import contextlib
import re
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build as myos_build  # noqa: E402
from tools import image, myfs  # noqa: E402

FS_HEADER = ROOT / "kernel32" / "fs.h"


def declared_expression(name: str) -> str:
    """The right-hand side of a `constexpr ... NAME = ...;` in kernel32/fs.h."""
    match = re.search(rf"\b{name}\s*=\s*([^;]+);", FS_HEADER.read_text())
    assert match is not None, f"{name} is not declared in kernel32/fs.h"
    return match.group(1).strip()


def declared_constant(name: str, seen: tuple[str, ...] = ()) -> int:
    """The value of a constant in kernel32/fs.h, which may be an expression.

    Several of the kernel's constants are written in terms of others -- the
    pointer count is `FS_BLOCK_SIZE / 2`, and the file size limit is a product --
    so looking for a literal is not good enough.  The expression is evaluated
    here instead, resolving names against the header recursively, and nothing is
    taken from the Python side: the comparison is the whole point.
    """
    if name in seen:
        raise AssertionError(
            f"circular constant definition: {' -> '.join(seen + (name,))}")
    expression = declared_expression(name)
    if not re.fullmatch(r"[\sA-Za-z0-9_+\-*/()]+", expression):
        raise AssertionError(f"{name} is not an arithmetic expression: {expression!r}")
    without_literals = re.sub(r"0[xX][0-9A-Fa-f]+|\d+", " ", expression)
    names = re.findall(r"[A-Za-z_]\w*", without_literals)
    values = {other: declared_constant(other, seen + (name,)) for other in names}
    return int(eval(expression, {"__builtins__": {}}, values))  # noqa: S307


class UserImageTests(unittest.TestCase):
    """The ring-3 programs the build produces, and where they end up."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.volume = myfs.volume_from_image(cls.result.hard_disk, myfs.PARTITION_INDEX)

    def test_every_program_in_the_build_is_in_the_volume(self) -> None:
        self.assertEqual(sorted(self.result.users), ["badwrite", "hello", "hellocpp"])
        for name, path in self.result.users.items():
            packed = self.volume.read_file(self.volume.resolve(f"/bin/{name}"))
            self.assertEqual(packed, path.read_bytes(),
                             f"/bin/{name} in the volume is not the image the build made")

    def test_every_image_has_a_header_the_kernel_will_accept(self) -> None:
        # The rules the kernel applies (kernel32/user.cpp): the MYOS magic, architecture
        # 3, a header whose bytes sum to zero, and an entry offset and size inside the
        # image.  A program that fails any of these is refused at run time, so catching
        # it here is the difference between a build failure and a surprise.
        for name, path in self.result.users.items():
            image = path.read_bytes()
            self.assertEqual(image[:4], b"MYOS", name)
            self.assertEqual(image[4], 3, f"{name} must say it is a user program")
            self.assertEqual(image[6], 0, f"{name} must have zero flags")
            self.assertEqual(sum(image[:16]) & 0xFF, 0, f"{name} header checksum")
            entry = int.from_bytes(image[8:12], "little")
            size = int.from_bytes(image[12:16], "little")
            self.assertEqual(size, len(image), name)
            self.assertGreaterEqual(entry, 16)
            self.assertLess(entry, size)

    def test_the_kernel_image_is_not_a_user_image(self) -> None:
        # Same magic, different architecture byte: this is what keeps the two loaders
        # from accepting each other's images.
        kernel = self.result.kernel.read_bytes()
        self.assertEqual(kernel[:4], b"MYOS")
        self.assertEqual(kernel[4], 2)
        self.assertEqual(self.result.users["hello"].read_bytes()[4], 3)

    def test_the_manifest_covers_the_programs(self) -> None:
        manifest = self.volume.read_file(self.volume.resolve("/manifest")).decode()
        listed = {line.split()[0] for line in manifest.splitlines()}
        for name in self.result.users:
            self.assertIn(f"bin/{name}", listed)
        # Three files from files/ plus three programs.
        self.assertEqual(len(listed), 6)

    def test_the_16_bit_build_has_no_user_programs(self) -> None:
        # Ring 3 is a 32-bit kernel feature; the 16-bit image and its bundle are
        # unchanged by it.
        result = myos_build.build(arch=16)
        self.assertEqual(result.users, {})
        self.assertIsNone(result.volume)


class PutFileTests(unittest.TestCase):
    """Writing one more file into a volume that already exists.

    This is the operation that makes a data disk able to receive new build output
    without being rebuilt: `put_file` creates the directory it needs and replaces the
    name it is given, and touches nothing else.
    """

    def test_it_creates_the_directory_it_needs(self) -> None:
        volume = myfs.build_volume(ROOT / "files")
        volume.put_file("/bin/tools/hello", b"program bytes")
        self.assertEqual(volume.read_file(volume.resolve("/bin/tools/hello")),
                         b"program bytes")
        self.assertTrue(volume.resolve("/bin").is_dir)
        self.assertEqual(volume.check(), [])

    def test_it_replaces_a_file_in_place(self) -> None:
        volume = myfs.build_volume(ROOT / "files")
        before = volume.free_blocks
        volume.put_file("/bin/hello", b"first version")
        after_first = volume.free_blocks
        volume.put_file("/bin/hello", b"second version")
        self.assertEqual(volume.read_file(volume.resolve("/bin/hello")),
                         b"second version")
        self.assertLessEqual(after_first, before)
        self.assertEqual(volume.check(), [])

    def test_it_refuses_to_replace_a_directory(self) -> None:
        volume = myfs.build_volume(ROOT / "files")
        with self.assertRaises(myfs.MyfsError):
            volume.put_file("/docs", b"not a directory")

    def test_the_command_line_can_put_a_file_into_a_disk_image(self) -> None:
        with scratch_directory() as directory:
            disk = directory / "put.img"
            myfs.write_data_disk(disk, files_dir=ROOT / "files")
            source = directory / "hello.bin"
            source.write_bytes(b"MYOS" + bytes(12))
            self.assertEqual(myfs.main(["--image", str(disk), "--partition", "1",
                                        "--put", "/bin/hello",
                                        "--from", str(source)]), 0)
            volume = myfs.volume_from_image(disk, myfs.PARTITION_INDEX)
            self.assertEqual(volume.read_file(volume.resolve("/bin/hello")),
                             b"MYOS" + bytes(12))
            # The files that were already there are still there.
            self.assertEqual(volume.read_file(volume.resolve("/hello.txt")),
                             (ROOT / "files" / "hello.txt").read_bytes())
            self.assertEqual(volume.check(), [])


class DataDiskUpdateTests(unittest.TestCase):
    """`build.py data-disk --update`: a merge, not a rebuild."""

    def setUp(self) -> None:
        self.original = myos_build.DATA_DISK

    def tearDown(self) -> None:
        myos_build.DATA_DISK = self.original

    def test_it_adds_what_the_build_ships_and_keeps_the_rest(self) -> None:
        with scratch_directory() as directory:
            disk = directory / "update.img"
            volume = myfs.build_volume(ROOT / "files")
            volume.create_file("/note.txt", b"mine")          # the user's own file
            disk.write_bytes(myfs.build_disk_image(volume))
            myos_build.DATA_DISK = disk

            self.assertEqual(myos_build.main(["data-disk", "--update"]), 0)

            after = myfs.volume_from_image(disk, myfs.PARTITION_INDEX)
            self.assertEqual(after.read_file(after.resolve("/note.txt")), b"mine")
            for name, path in myos_build.build_users(myos_build_toolchain()).items():
                self.assertEqual(after.read_file(after.resolve(f"/bin/{name}")),
                                 path.read_bytes())
            self.assertEqual(after.check(), [])

    def test_updating_twice_is_the_same_as_updating_once(self) -> None:
        with scratch_directory() as directory:
            disk = directory / "twice.img"
            myfs.write_data_disk(disk, files_dir=ROOT / "files")
            myos_build.DATA_DISK = disk
            self.assertEqual(myos_build.main(["data-disk", "--update"]), 0)
            once = disk.read_bytes()
            self.assertEqual(myos_build.main(["data-disk", "--update"]), 0)
            twice = disk.read_bytes()
            first = myfs.volume_from_image(disk, myfs.PARTITION_INDEX)
            self.assertEqual(first.check(), [])
            self.assertEqual(len(once), len(twice))
            # `run` in the guest checks the manifest of whatever is on the volume, so a
            # merge that rewrote files differently each time would be a boot-time
            # failure rather than a cosmetic difference.
            self.assertEqual(first.read_file(first.resolve("/manifest")),
                             myfs.volume_from_image(disk, myfs.PARTITION_INDEX)
                                 .read_file(myfs.volume_from_image(
                                     disk, myfs.PARTITION_INDEX).resolve("/manifest")))

    def test_a_disk_without_programs_is_kept_as_it_is(self) -> None:
        # `data-disk` on its own still never rebuilds: only `--update` writes.
        with scratch_directory() as directory:
            disk = directory / "kept.img"
            volume = myfs.build_volume(ROOT / "files")
            volume.create_file("/note.txt", b"mine")
            disk.write_bytes(myfs.build_disk_image(volume))
            myos_build.DATA_DISK = disk
            before = disk.read_bytes()
            self.assertEqual(myos_build.main(["data-disk"]), 0)
            self.assertEqual(disk.read_bytes(), before)


def myos_build_toolchain():
    """The toolchain the build uses, for tests that call into build.py's internals."""
    from tools import toolchain
    return toolchain.discover()


@contextlib.contextmanager
def scratch_directory():
    """A scratch directory under build/tmp, deliberately left behind.

    Windows refuses some of the permission changes shutil makes while deleting a
    tree in this sandbox, and a test that failed during cleanup would be reporting
    the sandbox rather than the code.  build/ is a disposable directory, so what
    the tests write there is simply left in it.
    """
    directory = ROOT / "build" / "tmp" / "myfs-tests"
    directory.mkdir(parents=True, exist_ok=True)
    yield directory


def checksum(data: bytes) -> int:
    """The manifest checksum, written out again on purpose.

    The kernel has its own copy of this arithmetic; re-deriving it here rather than
    calling the packer's means a change to one implementation is caught by the
    other instead of being agreed with.
    """
    value = 0
    for byte in data:
        value = (value * 31 + byte) & 0xFFFFFFFF
    return value


class MyfsFormatTests(unittest.TestCase):
    """The volume the packer writes, checked from its bytes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.volume = myfs.build_volume(ROOT / "files")

    def test_the_volume_is_the_size_the_format_says(self) -> None:
        data = self.volume.to_bytes()
        self.assertEqual(len(data), myfs.BLOCK_COUNT * myfs.BLOCK_SIZE)
        self.assertEqual(len(data), myfs.VOLUME_BYTES)

    def test_it_passes_every_structural_check(self) -> None:
        self.assertEqual(self.volume.check(), [])

    def test_the_superblock_says_what_the_volume_is(self) -> None:
        reloaded = myfs.Volume(self.volume.to_bytes())
        block = reloaded.block(0)
        magic, version, block_size, block_count, inode_count, first_data_block = \
            struct.unpack_from("<6I", block, 0)
        self.assertEqual(magic, myfs.MAGIC)
        self.assertEqual(version, myfs.VERSION)
        self.assertEqual(block_size, myfs.BLOCK_SIZE)
        self.assertEqual(block_count, myfs.BLOCK_COUNT)
        self.assertEqual(inode_count, myfs.INODE_COUNT)
        self.assertEqual(first_data_block, myfs.FIRST_DATA_BLOCK)

    def test_the_metadata_blocks_are_marked_used(self) -> None:
        for block in range(myfs.FIRST_DATA_BLOCK):
            self.assertTrue(self.volume.block_is_used(block),
                            f"metadata block {block} is marked free")

    def test_inode_zero_is_reserved_and_the_root_is_inode_one(self) -> None:
        self.assertTrue(self.volume.inode_is_used(0))
        self.assertTrue(self.volume.inode_is_used(myfs.ROOT_INODE))
        self.assertTrue(self.volume.read_inode(myfs.ROOT_INODE).is_dir)

    def test_the_packed_tree_is_the_tree_in_files(self) -> None:
        packed = {path: inode for path, inode in self.volume.iter_files()}
        self.assertIn("hello.txt", packed)
        self.assertIn("readme.txt", packed)
        self.assertIn("docs", packed)
        self.assertIn("docs/notes.txt", packed)
        for name in ("hello.txt", "readme.txt"):
            source = (ROOT / "files" / name).read_bytes()
            inode = self.volume.resolve(f"/{name}")
            self.assertEqual(self.volume.read_file(inode), source)

    def test_the_manifest_lists_every_file_the_volume_holds(self) -> None:
        manifest = self.volume.read_file(self.volume.resolve("/manifest")).decode()
        listed = {}
        for line in manifest.splitlines():
            name, size, digest = line.split()
            listed[name] = (int(size), digest)
        self.assertNotIn("manifest", listed, "the manifest must not list itself")
        for path, inode in self.volume.iter_files():
            if inode.is_dir or path == "manifest":
                continue
            self.assertIn(path, listed)
            self.assertEqual(listed[path][0], inode.size)
        self.assertEqual(len(listed), 3)

    def test_the_manifest_checksums_match_the_bytes(self) -> None:
        manifest = self.volume.read_file(self.volume.resolve("/manifest")).decode()
        for line in manifest.splitlines():
            name, _size, stated = line.split()
            data = self.volume.read_file(self.volume.resolve(f"/{name}"))
            self.assertEqual(f"{checksum(data):08X}", stated)

    def test_a_file_can_grow_past_the_direct_blocks_and_come_back(self) -> None:
        volume = myfs.Volume(self.volume.to_bytes())
        before = volume.free_blocks
        data = bytes((index * 7 + 1) & 0xFF for index in range(20000))
        inode = volume.create_file("/big.bin", data)
        self.assertGreater(inode.indirect, 0, "20000 bytes should need the indirect block")
        self.assertEqual(volume.read_file(volume.resolve("/big.bin")), data)
        self.assertEqual(volume.check(), [])
        volume.remove("/big.bin")
        self.assertEqual(volume.free_blocks, before)
        self.assertEqual(volume.check(), [])

    def test_a_shorter_overwrite_releases_the_blocks_it_no_longer_needs(self) -> None:
        volume = myfs.Volume(self.volume.to_bytes())
        before = volume.free_blocks
        inode = volume.create_file("/shrink.bin", b"x" * 4096)
        volume.write_file(volume.resolve("/shrink.bin"), b"y" * 100, truncate=True)
        inode = volume.read_inode(inode.index)
        self.assertEqual(inode.size, 100)
        self.assertEqual(volume.read_file(inode), b"y" * 100)
        self.assertEqual(inode.indirect, 0)
        volume.remove("/shrink.bin")
        self.assertEqual(volume.free_blocks, before)
        self.assertEqual(volume.check(), [])

    def test_creating_and_removing_a_subdirectory_leaves_no_trace(self) -> None:
        volume = myfs.Volume(self.volume.to_bytes())
        before = volume.free_blocks
        volume.make_directory("/d")
        volume.create_file("/d/f", b"inside")
        self.assertEqual(volume.read_file(volume.resolve("/d/f")), b"inside")
        with self.assertRaises(myfs.MyfsError):
            volume.remove("/d")                  # not empty
        volume.remove("/d/f")
        volume.remove("/d")
        self.assertEqual(volume.free_blocks, before)
        self.assertEqual(volume.check(), [])

    def test_a_name_that_does_not_fit_is_refused(self) -> None:
        volume = myfs.Volume(self.volume.to_bytes())
        with self.assertRaises(myfs.MyfsError):
            volume.create_file("/" + "n" * (myfs.NAME_MAX + 1), b"")

    def test_a_file_bigger_than_the_format_allows_is_refused(self) -> None:
        volume = myfs.Volume(self.volume.to_bytes())
        with self.assertRaises(myfs.MyfsError):
            volume.create_file("/huge", b"z" * (myfs.MAX_FILE_SIZE + 1))

    def test_a_volume_that_runs_out_of_space_says_so(self) -> None:
        volume = myfs.Volume(block_count=64)
        with self.assertRaises(myfs.MyfsError):
            for index in range(100):
                volume.create_file(f"/f{index}", b"x" * 400)

    def test_a_volume_that_is_not_myfs_is_refused(self) -> None:
        with self.assertRaises(myfs.MyfsError):
            myfs.Volume(bytes(64 * myfs.BLOCK_SIZE))

    def test_a_bad_magic_is_reported_as_such(self) -> None:
        data = bytearray(self.volume.to_bytes())
        data[0] ^= 0xFF
        with self.assertRaises(myfs.MyfsError):
            myfs.Volume(bytes(data))

    def test_the_leak_fixture_is_detected(self) -> None:
        # What a power cut in the middle of a write leaves behind: blocks marked
        # used that no inode references.  The kernel's fsck reclaims them; the host
        # checker has to see them first.
        volume = myfs.build_volume(ROOT / "files", leak_blocks=3)
        problems = volume.check()
        self.assertTrue(any("referenced by nothing" in problem for problem in problems),
                        problems)

    def test_the_dirty_fixture_is_detected(self) -> None:
        # The other half of a power cut: a state flag that says the volume was never
        # unmounted.  The kernel reports it at boot and fsck clears it.
        volume = myfs.build_volume(ROOT / "files")
        volume.set_state(myfs.STATE_DIRTY)
        reloaded = myfs.Volume(volume.to_bytes())
        self.assertEqual(reloaded.state, myfs.STATE_DIRTY)
        self.assertEqual(reloaded.check(), [],
                         "the state flag is not a structural inconsistency")
        reloaded.set_state(myfs.STATE_CLEAN)
        self.assertEqual(myfs.Volume(reloaded.to_bytes()).state, myfs.STATE_CLEAN)

    def test_the_command_line_can_build_both_fixtures(self) -> None:
        with scratch_directory() as directory:
            dirty = directory / "dirty.img"
            leaky = directory / "leaky.img"
            self.assertEqual(myfs.main(["--build", "files", "--out", str(dirty),
                                        "--dirty"]), 0)
            self.assertEqual(myfs.main(["--build", "files", "--out", str(leaky),
                                        "--leak-blocks", "2"]), 0)
            self.assertEqual(myfs.Volume(dirty.read_bytes()).state, myfs.STATE_DIRTY)
            problems = myfs.Volume(leaky.read_bytes()).check()
            self.assertTrue(any("referenced by nothing" in problem
                                for problem in problems), problems)

    def test_a_removed_directory_entry_is_reused(self) -> None:
        # A directory does not shrink when an entry is removed; the slot is a hole
        # that the next creation fills, which is what keeps a long-lived directory
        # from growing forever.
        volume = myfs.Volume(self.volume.to_bytes())
        volume.create_file("/one", b"1")
        volume.create_file("/two", b"2")
        before = len(volume.read_bytes(volume.read_inode(1)))
        volume.remove("/one")
        volume.create_file("/three", b"3")
        self.assertEqual(volume.read_file(volume.resolve("/three")), b"3")
        self.assertEqual(len(volume.read_bytes(volume.read_inode(1))), before,
                         "the reused slot grew the directory instead of filling it")
        self.assertEqual(volume.check(), [])


class MyfsContractTests(unittest.TestCase):
    """The format as Python states it and as C++ states it."""

    def test_the_format_constants_agree_with_the_kernel(self) -> None:
        self.assertEqual(declared_constant("FS_MAGIC"), myfs.MAGIC)
        self.assertEqual(declared_constant("FS_VERSION"), myfs.VERSION)
        self.assertEqual(declared_constant("FS_BLOCK_SIZE"), myfs.BLOCK_SIZE)
        self.assertEqual(declared_constant("FS_BLOCK_COUNT"), myfs.BLOCK_COUNT)
        self.assertEqual(declared_constant("FS_INODE_COUNT"), myfs.INODE_COUNT)
        self.assertEqual(declared_constant("FS_FIRST_DATA_BLOCK"), myfs.FIRST_DATA_BLOCK)
        self.assertEqual(declared_constant("FS_ROOT_INODE"), myfs.ROOT_INODE)
        self.assertEqual(declared_constant("FS_NAME_MAX"), myfs.NAME_MAX)
        self.assertEqual(declared_constant("FS_DIRECT_BLOCKS"), myfs.DIRECT_BLOCKS)
        self.assertEqual(declared_constant("FS_INODE_SIZE"), myfs.INODE_SIZE)
        self.assertEqual(declared_constant("FS_DIRENT_SIZE"), myfs.DIRENT_SIZE)
        self.assertEqual(declared_constant("FS_INODE_TABLE_BLOCK"), myfs.INODE_TABLE_BLOCK)
        self.assertEqual(declared_constant("FS_BITMAP_BLOCK"), myfs.BITMAP_BLOCK)
        self.assertEqual(declared_constant("FS_ENTRIES_PER_BLOCK"), myfs.ENTRIES_PER_BLOCK)
        self.assertEqual(declared_constant("FS_INODE_BITMAP_BYTES"), myfs.INODE_BITMAP_BYTES)
        self.assertEqual(declared_constant("FS_BLOCK_BITMAP_BYTES"), myfs.BLOCK_BITMAP_BYTES)
        self.assertEqual(declared_constant("FS_PARTITION_TYPE"), myfs.PARTITION_TYPE)
        self.assertEqual(declared_constant("FS_SCRATCH_LBA"), myfs.SCRATCH_LBA)
        self.assertEqual(declared_constant("FS_SCRATCH_SECTORS"), myfs.SCRATCH_SECTORS)
        self.assertEqual(declared_constant("FS_LABEL_SIZE"), myfs.LABEL_SIZE)
        self.assertEqual(declared_constant("FS_MAX_FILE_SIZE"), myfs.MAX_FILE_SIZE)
        self.assertEqual(declared_constant("FS_MAX_FILE_BLOCKS"), myfs.MAX_FILE_BLOCKS)
        self.assertEqual(declared_constant("FS_POINTERS_PER_BLOCK"),
                         myfs.POINTERS_PER_BLOCK)

    def test_the_partition_layout_in_the_build_matches_the_tool(self) -> None:
        # build.py carves the disk with these numbers, so a build that disagreed
        # with the tool would put the volume somewhere the tool cannot find it.
        self.assertEqual(myos_build.FILES_DIR.name, "files")
        self.assertGreaterEqual(myfs.DISK_SECTORS,
                                myfs.PARTITION_LBA + myfs.PARTITION_SECTORS)

    def test_the_volume_fits_in_its_partition(self) -> None:
        self.assertEqual(myfs.PARTITION_SECTORS * image.SECTOR, myfs.VOLUME_BYTES)
        self.assertEqual(myfs.BLOCK_SIZE, image.SECTOR)


class MyfsImageLayoutTests(unittest.TestCase):
    """Where the volume sits in the images a build produces."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.hard = cls.result.hard_disk.read_bytes()

    def test_the_boot_image_has_a_myfs_partition(self) -> None:
        entries = image.mbr_partition_entries(self.hard)
        entry = entries[myfs.PARTITION_INDEX]
        self.assertEqual(entry["type"], myfs.PARTITION_TYPE)
        self.assertEqual(entry["lba_start"], myfs.PARTITION_LBA)
        self.assertEqual(entry["sectors"], myfs.PARTITION_SECTORS)

    def test_the_boot_partition_stops_before_the_volume(self) -> None:
        entries = image.mbr_partition_entries(self.hard)
        boot = entries[0]
        self.assertTrue(boot["active"])
        self.assertEqual(boot["lba_start"], 1)
        self.assertEqual(boot["lba_start"] + boot["sectors"], myfs.PARTITION_LBA)

    def test_the_kernel_and_loader_stay_clear_of_the_volume(self) -> None:
        # The loader is at LBA 1 and the kernel at LBA 128 with a 896-sector
        # budget; the volume starts after both, or the kernel would be reading
        # sectors that belong to the filesystem.
        self.assertLessEqual(myos_build.KERNEL_LBA + myos_build.MAX_KERNEL32_SECTORS,
                             myfs.PARTITION_LBA)

    def test_the_volume_in_the_image_is_the_packed_volume(self) -> None:
        start = myfs.PARTITION_LBA * image.SECTOR
        end = start + myfs.VOLUME_BYTES
        packed = self.result.volume.read_bytes()
        self.assertEqual(self.hard[start:end], packed)
        self.assertEqual(myfs.Volume(packed).check(), [])

    def test_the_scratch_tail_is_outside_every_partition(self) -> None:
        # `blk test` is the only thing that writes to the disk, and it must write
        # where no partition lives: otherwise proving the driver works would mean
        # damaging the filesystem.
        entries = image.mbr_partition_entries(self.hard)
        scratch = range(myfs.SCRATCH_LBA, myfs.SCRATCH_LBA + myfs.SCRATCH_SECTORS)
        for entry in entries:
            if entry["sectors"] == 0:
                continue
            covered = range(entry["lba_start"], entry["lba_start"] + entry["sectors"])
            self.assertEqual(set(scratch) & set(covered), set(),
                             f"the scratch tail overlaps partition {entry['index']}")
        self.assertEqual(myfs.SCRATCH_LBA + myfs.SCRATCH_SECTORS, myfs.DISK_SECTORS)
        self.assertEqual(len(self.hard) // image.SECTOR, myfs.DISK_SECTORS)

    def test_the_16_bit_images_are_unaffected(self) -> None:
        # The filesystem is a 32-bit milestone: the 16-bit disk image keeps its
        # old size and its single whole-disk partition.
        result = myos_build.build(arch=16)
        data = result.hard_disk.read_bytes()
        self.assertIsNone(result.volume)
        self.assertEqual(len(data), 1474560)
        entries = image.mbr_partition_entries(data)
        self.assertEqual(entries[1]["type"], 0)
        self.assertEqual(entries[0]["lba_start"] + entries[0]["sectors"],
                         len(data) // image.SECTOR)

    def test_the_user_data_disk_has_the_same_layout(self) -> None:
        # run.py attaches this as the primary master; the kernel finds the volume
        # the same way it finds the one on the boot disk.
        with scratch_directory() as directory:
            path = Path(directory) / "data.img"
            myfs.write_data_disk(path, files_dir=ROOT / "files")
            data = path.read_bytes()
            entries = image.mbr_partition_entries(data)
            entry = entries[myfs.PARTITION_INDEX]
            self.assertEqual(entry["type"], myfs.PARTITION_TYPE)
            self.assertEqual(entry["lba_start"], myfs.PARTITION_LBA)
            self.assertEqual(entry["sectors"], myfs.PARTITION_SECTORS)
            volume = myfs.volume_from_image(path, myfs.PARTITION_INDEX)
            self.assertEqual(volume.check(), [])
            self.assertEqual(volume.read_file(volume.resolve("/hello.txt")),
                             (ROOT / "files" / "hello.txt").read_bytes())

    def test_a_data_disk_with_no_files_still_has_a_root_directory(self) -> None:
        with scratch_directory() as directory:
            path = Path(directory) / "empty.img"
            volume = myfs.write_data_disk(path)
            self.assertEqual(volume.check(), [])
            self.assertEqual(list(volume.iter_files()), [])


class DataDiskClipboardTests(unittest.TestCase):
    """The disk the user's own files live on, and who may rewrite it.

    build.py deliberately does not build it: `build.py all` rewrites images/, and a
    rebuild that deletes what you saved is not a filesystem.  These tests pin that
    behaviour down, because it is a promise rather than an implementation detail.
    """

    def setUp(self) -> None:
        self.original = myos_build.DATA_DISK

    def tearDown(self) -> None:
        myos_build.DATA_DISK = self.original

    def test_run_py_attaches_it_as_the_primary_ata_master(self) -> None:
        from tools import qemu
        import run as myos_run
        self.assertEqual(qemu.data_disk_arguments(Path("data.img")),
                         ["-drive", "file=data.img,format=raw,if=ide,index=0"])
        # run.py has to use it, or a 32-bit interactive session would boot without
        # a filesystem and every file command would only report why it cannot work.
        source = (ROOT / "run.py").read_text()
        self.assertIn("data_disk_arguments(ensure_data_disk())", source)
        self.assertEqual(myos_run.data_disk_arguments(Path("data.img")),
                         qemu.data_disk_arguments(Path("data.img")))

    def test_a_missing_data_disk_is_created_from_the_packed_files(self) -> None:
        import run as myos_run
        with scratch_directory() as directory:
            path = directory / "created.img"
            if path.exists():
                path.unlink()
            myos_build.DATA_DISK = path
            self.assertEqual(myos_run.ensure_data_disk(), path)
            volume = myfs.volume_from_image(path, myfs.PARTITION_INDEX)
            self.assertEqual(volume.check(), [])
            self.assertEqual(volume.read_file(volume.resolve("/hello.txt")),
                             (ROOT / "files" / "hello.txt").read_bytes())

    def test_an_existing_data_disk_is_kept_byte_for_byte(self) -> None:
        import run as myos_run
        with scratch_directory() as directory:
            path = directory / "kept.img"
            path.unlink(missing_ok=True)
            myos_build.DATA_DISK = path
            volume = myfs.Volume()
            volume.create_file("/mine.txt", b"do not overwrite me")
            path.write_bytes(myfs.build_disk_image(volume))
            before = path.read_bytes()
            myos_run.ensure_data_disk()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                myfs.volume_from_image(path, myfs.PARTITION_INDEX)
                    .read_file(myfs.volume_from_image(path, myfs.PARTITION_INDEX)
                               .resolve("/mine.txt")),
                b"do not overwrite me")

    def test_the_build_command_keeps_an_existing_data_disk(self) -> None:
        with scratch_directory() as directory:
            path = directory / "command.img"
            # The scratch directory is deliberately left behind between runs, so a
            # fixed name has to be cleared rather than assumed missing.
            path.unlink(missing_ok=True)
            myos_build.DATA_DISK = path
            self.assertEqual(myos_build.main(["data-disk"]), 0)
            first = path.read_bytes()
            wrote = myfs.volume_from_image(path, myfs.PARTITION_INDEX)
            wrote.create_file("/from-a-user.txt", b"hello")
            # build_disk_image is not how a user writes; write through the tool's
            # own volume API and put it back, which is what the kernel will do.
            path.write_bytes(myfs.build_disk_image(wrote))
            with_extra = path.read_bytes()
            self.assertEqual(myos_build.main(["data-disk"]), 0)
            self.assertEqual(path.read_bytes(), with_extra,
                             "`build.py data-disk` rebuilt a disk that already existed")
            self.assertNotEqual(first, with_extra)
            self.assertEqual(
                myfs.volume_from_image(path, myfs.PARTITION_INDEX).read_file(
                    myfs.volume_from_image(path, myfs.PARTITION_INDEX)
                        .resolve("/from-a-user.txt")),
                b"hello")


if __name__ == "__main__":
    unittest.main()
