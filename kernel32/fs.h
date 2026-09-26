// myos 32-bit kernel: the myfs filesystem.
//
// tools/myfs.py builds the volume this code mounts, and the two have to agree byte
// for byte: the constants below are stated a second time in Python, and a test
// compares them.  That is the same discipline the boot info block follows, for the
// same reason -- a quiet disagreement here does not look like a bug, it looks like
// a plausible volume full of garbage.
//
// Layout, little endian, 512-byte blocks:
//
//     block 0        superblock
//     blocks 1..4    inode table: 64 inodes of 32 bytes
//     block 5        bitmaps: inode bitmap (8 bytes), then block bitmap (256 bytes)
//     blocks 6..     data blocks
//
// An inode is 32 bytes: type, padding, size, then 12 block numbers of 2 bytes --
// 11 direct blocks and one single-indirect block holding 256 more.  A file is
// therefore at most (11 + 256) * 512 = 136704 bytes.  A directory is an inode whose
// data blocks hold 32-byte dirents (a 29-character NUL-terminated name and a
// 2-byte inode number), 16 per block.

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 FS_MAGIC = 0x5346594D;              // 'M','Y','F','S' little endian
constexpr uint32 FS_VERSION = 1;
constexpr uint32 FS_BLOCK_SIZE = 512;
constexpr uint32 FS_BLOCK_COUNT = 2048;
constexpr uint32 FS_INODE_COUNT = 64;
constexpr uint32 FS_FIRST_DATA_BLOCK = 6;
constexpr uint32 FS_ROOT_INODE = 1;                  // inode 0 is reserved
constexpr uint32 FS_NAME_MAX = 29;
constexpr uint32 FS_DIRECT_BLOCKS = 11;
constexpr uint32 FS_POINTERS_PER_BLOCK = FS_BLOCK_SIZE / 2;
constexpr uint32 FS_MAX_FILE_BLOCKS = FS_DIRECT_BLOCKS + FS_POINTERS_PER_BLOCK;
constexpr uint32 FS_MAX_FILE_SIZE = FS_MAX_FILE_BLOCKS * FS_BLOCK_SIZE;
constexpr uint32 FS_INODE_SIZE = 32;
constexpr uint32 FS_DIRENT_SIZE = 32;
constexpr uint32 FS_INODE_TABLE_BLOCK = 1;
constexpr uint32 FS_BITMAP_BLOCK = 5;
constexpr uint32 FS_INODE_BITMAP_BYTES = FS_INODE_COUNT / 8;
constexpr uint32 FS_BLOCK_BITMAP_BYTES = FS_BLOCK_COUNT / 8;
constexpr uint32 FS_ENTRIES_PER_BLOCK = FS_BLOCK_SIZE / FS_DIRENT_SIZE;
constexpr uint32 FS_LABEL_SIZE = 24;
constexpr uint32 FS_MAX_PATH = 96;
constexpr uint32 FS_MAX_DEPTH = 8;

// The MBR partition type the volume lives in, matching PARTITION_TYPE in
// tools/myfs.py.  Nothing standard describes this filesystem, so it has a type of
// its own rather than pretending to be FAT.
constexpr uint8 FS_PARTITION_TYPE = 0x7F;

// The tail of the disk, outside every partition: the only place a device test may
// write.  Writing inside the volume to prove the driver works would be writing to
// the filesystem to prove the disk works, and the two failures would then look the
// same.  These two numbers are stated in tools/myfs.py as well.
constexpr uint32 FS_SCRATCH_LBA = 4096;
constexpr uint32 FS_SCRATCH_SECTORS = 8;

constexpr uint8 FS_TYPE_FREE = 0;
constexpr uint8 FS_TYPE_FILE = 1;
constexpr uint8 FS_TYPE_DIR = 2;

constexpr uint32 FS_STATE_DIRTY = 0;
constexpr uint32 FS_STATE_CLEAN = 1;

struct SuperBlock {
    uint32 magic;               // +0
    uint32 version;             // +4
    uint32 block_size;          // +8
    uint32 block_count;         // +12
    uint32 inode_count;         // +16
    uint32 first_data_block;    // +20
    uint32 free_blocks;         // +24
    uint32 free_inodes;         // +28
    uint32 root_inode;          // +32
    uint32 state;               // +36
    char label[FS_LABEL_SIZE];  // +40
    uint8 reserved[448];        // +64, so the superblock fills one block
};
static_assert(sizeof(SuperBlock) == FS_BLOCK_SIZE,
              "the superblock is exactly one block");
static_assert(__builtin_offsetof(SuperBlock, free_blocks) == 24,
              "the packer writes the free block count at +24");

struct Inode {
    uint8 type;                 // +0
    uint8 pad[3];               // +1
    uint32 size;                // +4, bytes for a file, used dirent bytes for a dir
    uint16 blocks[FS_DIRECT_BLOCKS + 1];   // +8: 11 direct, then the indirect block
};
static_assert(sizeof(Inode) == FS_INODE_SIZE, "an inode is 32 bytes");
static_assert(__builtin_offsetof(Inode, size) == 4, "the size is at +4 in an inode");
static_assert(__builtin_offsetof(Inode, blocks) == 8,
              "the block numbers start at +8 in an inode");

struct Dirent {
    char name[30];              // +0, NUL terminated, so 29 characters at most
    uint16 inode;               // +30
};
static_assert(sizeof(Dirent) == FS_DIRENT_SIZE, "a dirent is 32 bytes");

// Every way an operation can fail, as a value the shell can print.  Negative so
// that "how many bytes" and "which error" can share one return type, which is what
// makes the file layer read like the file layer of a real system.
enum FsError : int32 {
    FS_OK = 0,
    FS_NO_DEVICE = -1,
    FS_NO_PARTITION = -2,
    FS_BAD_VOLUME = -3,
    FS_NOT_MOUNTED = -4,
    FS_NO_ENT = -5,
    FS_EXISTS = -6,
    FS_NOT_DIR = -7,
    FS_IS_DIR = -8,
    FS_NO_SPACE = -9,
    FS_NO_INODE = -10,
    FS_NAME_TOO_LONG = -11,
    FS_FILE_TOO_BIG = -12,
    FS_IO = -13,
    FS_BAD_FD = -14,
    FS_UNSUPPORTED = -15,
    FS_DIR_NOT_EMPTY = -16,
};

const char* fs_error_text(int32 error);

struct FsStat {
    uint32 inode;
    uint8 type;
    uint32 size;
    uint32 blocks;              // data blocks in use, including the indirect block
    bool indirect;
};

// Mounts the volume found in the MBR of the primary master.  Every failure leaves
// the filesystem unmounted and a one-sentence reason in fs_mount_problem().  It is
// safe to call twice.
int32 fs_mount();
bool fs_mounted();
const SuperBlock* fs_super();
const char* fs_mount_problem();
uint32 fs_partition_lba();
uint32 fs_partition_sectors();

// True when the superblock's free counts disagree with the bitmaps.  The mount
// still succeeds -- the bitmaps are what the allocator trusts -- but the
// disagreement is reported rather than hidden, because only a repair pass should
// decide which side is wrong.
bool fs_counts_disagree();
uint32 fs_bitmap_free_blocks();
uint32 fs_bitmap_free_inodes();

int32 fs_inode_read(uint32 index, Inode* out);

// Copies up to `count` bytes from `offset`; the return value is how many were
// copied, so a read that runs past the end is a short read rather than an error.
int32 fs_read_data(const Inode& inode, uint32 offset, void* buffer, uint32 count);

int32 fs_resolve(const char* path, uint32* out_inode);
int32 fs_stat(const char* path, FsStat* out);
uint32 fs_dir_slots(const Inode& directory);
int32 fs_dir_entry(const Inode& directory, uint32 slot, Dirent* out);
uint32 fs_dir_live_entries(const Inode& directory);

// --------------------------------------------------------------- the writes
//
// The volume is written through: data blocks reach the disk as they are written,
// and the metadata of an operation (bitmaps, superblock, inodes) is flushed before
// that operation returns.  There is no dirty block cache, so `sync` has nothing to
// flush -- it exists to say so honestly and to mark the volume cleanly unmounted.
//
// Every write is ordered so that a crash can only ever leave *leaked* blocks,
// never a file whose inode points at a block the allocator thinks is free:
//
//   create/allocate   bitmap (block marked used) -> data -> inode -> superblock
//   unlink/free       directory entry -> inode -> bitmap (blocks marked free)
//
// A leak costs space and `fsck` reclaims it; the other order costs data.

// Creates a file or a directory, refusing to replace anything that already exists.
int32 fs_create(const char* path, uint8 type);

// Creates the file if it is missing, then writes at offset 0.  With `truncate` the
// file ends where the write does, which is what an overwrite means.
int32 fs_write_file(const char* path, const void* data, uint32 count, bool truncate);

// Removes a file, or a directory that is empty.
int32 fs_unlink(const char* path);

// Flushes the metadata, marks the volume cleanly unmounted, and returns how many
// bytes were written for it (nothing, on this volume).
int32 fs_sync();

struct FsckReport {
    uint32 problems;             // everything found, repaired or not
    uint32 reclaimed_blocks;     // marked used and referenced by nothing: freed
    uint32 rescued_blocks;       // referenced but marked free: marked used
    uint32 orphan_inodes;        // allocated but unreachable: reported, not freed
    bool counts_wrong;           // the superblock's free counts had to be fixed
};

// Scans the whole volume against itself and repairs what is safe to repair: leaked
// blocks are freed, blocks a live inode points at are marked used, and the free
// counts are recomputed.  An inode nothing refers to is reported and left alone --
// deleting it would be guessing what the user meant.
int32 fs_fsck(FsckReport* report);

// The manifest the packer wrote: one "name size checksum" line per packed file.
// Verifying it is how the kernel proves it reads what the host wrote.
struct ManifestReport {
    uint32 lines;
    uint32 checked;
    uint32 mismatched;
    char first_problem[FS_MAX_PATH];
};
int32 fs_verify_manifest(ManifestReport* report);

// Human-readable reports used by the shell: `fs` describes the volume and `df`
// its usage.
void fs_report_volume();
void fs_report_usage();

}  // namespace myos
