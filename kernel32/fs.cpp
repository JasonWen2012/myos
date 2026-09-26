// myos 32-bit kernel: the myfs implementation behind kernel32/fs.h.
//
// The read path is the whole of this milestone: mount, walk, read, report.  Writes
// (creating, growing, unlinking) belong to the next one, and keeping them out of
// here for now means every failure this file can produce is a read failure, which
// is a much smaller thing to reason about.

#include "fs.h"

#include "ata.h"
#include "console.h"
#include "libc.h"
#include "mbr.h"

namespace myos {
namespace {

SuperBlock g_super;
uint8 g_inode_bitmap[FS_INODE_BITMAP_BYTES];
uint8 g_block_bitmap[FS_BLOCK_BITMAP_BYTES];
bool g_mounted = false;
bool g_counts_disagree = false;
bool g_metadata_dirty = false;      // bitmaps changed in RAM and not yet written
uint32 g_partition_lba = 0;
uint32 g_partition_sectors = 0;
char g_problem[96] = "not mounted";

// A release list, in BSS rather than on the stack: releasing a large file touches
// up to FS_MAX_FILE_BLOCKS blocks plus its indirect block, and the kernel's stack
// is 16 KiB.
uint16 g_release_list[FS_DIRECT_BLOCKS + FS_POINTERS_PER_BLOCK + 1];
uint32 g_release_count = 0;

constexpr uint32 MANIFEST_MAX = 1024;

void set_problem(const char* text) {
    uint32 index = 0;
    while (text[index] != '\0' && index + 1 < sizeof(g_problem)) {
        g_problem[index] = text[index];
        ++index;
    }
    g_problem[index] = '\0';
}

bool bit_used(const uint8* bitmap, uint32 index) {
    return (bitmap[index / 8] & (1u << (index % 8))) != 0;
}

uint32 count_free(const uint8* bitmap, uint32 first, uint32 limit) {
    uint32 free = 0;
    for (uint32 index = first; index < limit; ++index) {
        if (!bit_used(bitmap, index)) {
            ++free;
        }
    }
    return free;
}

bool block_read(uint32 block, void* buffer) {
    return ata_read(g_partition_lba + block, 1, buffer);
}

bool block_write(uint32 block, const void* buffer) {
    return ata_write(g_partition_lba + block, 1, buffer);
}

void set_bit(uint8* bitmap, uint32 index, bool value) {
    const uint8 mask = static_cast<uint8>(1u << (index % 8));
    if (value) {
        bitmap[index / 8] = static_cast<uint8>(bitmap[index / 8] | mask);
    } else {
        bitmap[index / 8] = static_cast<uint8>(bitmap[index / 8] & ~mask);
    }
}

// The two bitmaps share one block, so they are written together: a partial update
// would leave the inode bitmap describing a different volume from the block one.
bool bitmaps_write() {
    uint8 block[FS_BLOCK_SIZE];
    if (!block_read(FS_BITMAP_BLOCK, block)) {
        return false;
    }
    memcpy(block, g_inode_bitmap, FS_INODE_BITMAP_BYTES);
    memcpy(block + FS_INODE_BITMAP_BYTES, g_block_bitmap, FS_BLOCK_BITMAP_BYTES);
    return block_write(FS_BITMAP_BLOCK, block);
}

bool super_write() {
    uint8 block[FS_BLOCK_SIZE];
    memset(block, 0, sizeof(block));
    memcpy(block, &g_super, sizeof(SuperBlock));
    return block_write(0, block);
}

bool inode_write(uint32 index, const Inode& inode) {
    const uint32 offset = index * FS_INODE_SIZE;
    const uint32 block = FS_INODE_TABLE_BLOCK + offset / FS_BLOCK_SIZE;
    const uint32 within = offset % FS_BLOCK_SIZE;
    uint8 raw[FS_BLOCK_SIZE];
    if (!block_read(block, raw)) {
        return false;
    }
    memcpy(raw + within, &inode, sizeof(Inode));
    return block_write(block, raw);
}

uint16 read_u16(const uint8* at) {
    return static_cast<uint16>(at[0] | (at[1] << 8));
}

void write_u16(uint8* at, uint16 value) {
    at[0] = static_cast<uint8>(value & 0xFF);
    at[1] = static_cast<uint8>(value >> 8);
}

// --------------------------------------------------------------- name helpers

uint32 text_length(const char* text) {
    uint32 length = 0;
    while (text[length] != '\0') {
        ++length;
    }
    return length;
}

bool text_equal(const char* a, const char* b) {
    while (*a != '\0' && *b != '\0') {
        if (*a != *b) {
            return false;
        }
        ++a;
        ++b;
    }
    return *a == *b;
}

// A dirent's name field is 30 bytes and may not be terminated if a volume was
// written by something less careful than the packer, so the comparison stops at
// the field's end rather than trusting a NUL to be there.
bool dirent_matches(const Dirent& entry, const char* name) {
    for (uint32 index = 0; index < sizeof(entry.name); ++index) {
        const char c = entry.name[index];
        if (c == '\0') {
            return name[index] == '\0';
        }
        if (name[index] != c) {
            return false;
        }
    }
    return false;                           // 30 characters and no terminator
}

void copy_name(char* dest, const char* source, uint32 limit) {
    uint32 index = 0;
    while (index + 1 < limit && source[index] != '\0') {
        dest[index] = source[index];
        ++index;
    }
    dest[index] = '\0';
}

// ------------------------------------------------ mapping bytes to disk blocks

// Physical block holding logical block ``logical`` of an inode, or 0 when the
// file does not reach that far.  Reading the indirect block has to read a sector
// from the device, which can fail, so the failure is reported through a flag
// rather than by returning 0 -- the two cases mean opposite things.
uint32 block_number(const Inode& inode, uint32 logical, bool* io_error) {
    if (logical < FS_DIRECT_BLOCKS) {
        return inode.blocks[logical];
    }
    const uint32 indirect = inode.blocks[FS_DIRECT_BLOCKS];
    if (indirect == 0) {
        return 0;
    }
    const uint32 entry = logical - FS_DIRECT_BLOCKS;
    if (entry >= FS_POINTERS_PER_BLOCK) {
        return 0;
    }
    uint8 raw[FS_BLOCK_SIZE];
    if (!block_read(indirect, raw)) {
        *io_error = true;
        return 0;
    }
    return static_cast<uint32>(raw[entry * 2]) |
           (static_cast<uint32>(raw[entry * 2 + 1]) << 8);
}

uint32 inode_data_blocks(const Inode& inode) {
    uint32 blocks = 0;
    for (uint32 index = 0; index < FS_DIRECT_BLOCKS; ++index) {
        if (inode.blocks[index] != 0) {
            ++blocks;
        }
    }
    if (inode.blocks[FS_DIRECT_BLOCKS] != 0) {
        ++blocks;                           // the indirect block itself
    }
    return blocks;
}

// -------------------------------------------------------------------- parsing

bool is_digit(char c) {
    return c >= '0' && c <= '9';
}

int hex_value(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

}  // namespace

// ------------------------------------------------------------------- mounting

const char* fs_error_text(int32 error) {
    switch (error) {
        case FS_OK: return "ok";
        case FS_NO_DEVICE: return "no disk";
        case FS_NO_PARTITION: return "no myfs partition";
        case FS_BAD_VOLUME: return "not a myfs volume";
        case FS_NOT_MOUNTED: return "no filesystem mounted";
        case FS_NO_ENT: return "no such file or directory";
        case FS_EXISTS: return "already exists";
        case FS_NOT_DIR: return "not a directory";
        case FS_IS_DIR: return "is a directory";
        case FS_NO_SPACE: return "no space left";
        case FS_NO_INODE: return "no free inode left";
        case FS_NAME_TOO_LONG: return "name too long";
        case FS_FILE_TOO_BIG: return "file too big";
        case FS_IO: return "disk I/O error";
        case FS_BAD_FD: return "bad file descriptor";
        case FS_UNSUPPORTED: return "not supported yet";
        case FS_DIR_NOT_EMPTY: return "directory not empty";
        default: return "unknown error";
    }
}

int32 fs_mount() {
    if (g_mounted) {
        return FS_OK;
    }
    const AtaDevice* device = ata_probe();
    if (!device->present) {
        set_problem("no ATA device on the primary channel");
        return FS_NO_DEVICE;
    }
    Partition partition;
    if (!mbr_find_partition(FS_PARTITION_TYPE, &partition)) {
        set_problem(mbr_last_problem());
        return FS_NO_PARTITION;
    }
    if (partition.sectors != FS_BLOCK_COUNT) {
        set_problem("the myfs partition is not the size this kernel expects");
        return FS_BAD_VOLUME;
    }
    g_partition_lba = partition.lba_start;
    g_partition_sectors = partition.sectors;

    // The superblock is read before the state is marked mounted, so block_read
    // does not have to know that it is being used during the mount itself: from
    // here on g_partition_lba is set, which is all block_read needs.
    uint8 block[FS_BLOCK_SIZE];
    if (!ata_read(g_partition_lba, 1, block)) {
        set_problem("reading the superblock failed");
        return FS_IO;
    }
    memcpy(&g_super, block, sizeof(SuperBlock));
    if (g_super.magic != FS_MAGIC) {
        set_problem("the volume's magic number is not MYFS");
        return FS_BAD_VOLUME;
    }
    if (g_super.version != FS_VERSION) {
        set_problem("the volume was written by another myfs version");
        return FS_BAD_VOLUME;
    }
    if (g_super.block_size != FS_BLOCK_SIZE || g_super.block_count != FS_BLOCK_COUNT) {
        set_problem("the volume geometry is not what this kernel expects");
        return FS_BAD_VOLUME;
    }
    if (g_super.inode_count != FS_INODE_COUNT ||
        g_super.first_data_block != FS_FIRST_DATA_BLOCK) {
        set_problem("the volume's inode table or data area is not where it should be");
        return FS_BAD_VOLUME;
    }
    if (g_super.root_inode == 0 || g_super.root_inode >= FS_INODE_COUNT) {
        set_problem("the volume's root inode is outside the inode table");
        return FS_BAD_VOLUME;
    }
    if (!ata_read(g_partition_lba + FS_BITMAP_BLOCK, 1, block)) {
        set_problem("reading the allocation bitmaps failed");
        return FS_IO;
    }
    memcpy(g_inode_bitmap, block, sizeof(g_inode_bitmap));
    memcpy(g_block_bitmap, block + FS_INODE_BITMAP_BYTES, sizeof(g_block_bitmap));

    // The bitmaps are what the allocator will trust, so the superblock's counts
    // are checked against them here and the disagreement is remembered rather
    // than silently believed.
    const uint32 free_blocks = count_free(g_block_bitmap, FS_FIRST_DATA_BLOCK,
                                          FS_BLOCK_COUNT);
    const uint32 free_inodes = count_free(g_inode_bitmap, 1, FS_INODE_COUNT);
    g_counts_disagree = free_blocks != g_super.free_blocks ||
                        free_inodes != g_super.free_inodes;

    // The root inode is read with g_mounted already true, because every accessor
    // checks that flag first; a volume whose root is not a directory is then
    // unmounted again below.
    g_mounted = true;
    Inode root;
    const int32 status = fs_inode_read(g_super.root_inode, &root);
    if (status < 0 || root.type != FS_TYPE_DIR) {
        g_mounted = false;
        set_problem("the volume's root inode is not a directory");
        return FS_BAD_VOLUME;
    }
    set_problem("mounted");
    if (g_super.state != FS_STATE_CLEAN) {
        // Worth saying at boot rather than only when asked: a volume that was not
        // shut down cleanly may have lost a write, and `fsck` is the answer.
        kprintf("fs: warning: the volume was not unmounted cleanly; run `fsck`\n");
    }
    return FS_OK;
}

bool fs_mounted() {
    return g_mounted;
}

const SuperBlock* fs_super() {
    return &g_super;
}

const char* fs_mount_problem() {
    return g_problem;
}

uint32 fs_partition_lba() {
    return g_partition_lba;
}

uint32 fs_partition_sectors() {
    return g_partition_sectors;
}

bool fs_counts_disagree() {
    return g_counts_disagree;
}

uint32 fs_bitmap_free_blocks() {
    return count_free(g_block_bitmap, FS_FIRST_DATA_BLOCK, FS_BLOCK_COUNT);
}

uint32 fs_bitmap_free_inodes() {
    return count_free(g_inode_bitmap, 1, FS_INODE_COUNT);
}

// ---------------------------------------------------------------------- reads

int32 fs_inode_read(uint32 index, Inode* out) {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    if (index >= FS_INODE_COUNT) {
        return FS_NO_ENT;
    }
    const uint32 offset = index * FS_INODE_SIZE;
    const uint32 block = FS_INODE_TABLE_BLOCK + offset / FS_BLOCK_SIZE;
    const uint32 within = offset % FS_BLOCK_SIZE;
    uint8 raw[FS_BLOCK_SIZE];
    if (!block_read(block, raw)) {
        return FS_IO;
    }
    memcpy(out, raw + within, sizeof(Inode));
    return FS_OK;
}

int32 fs_read_data(const Inode& inode, uint32 offset, void* buffer, uint32 count) {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    // A size beyond what the format can express means the inode is corrupt, and
    // clamping it is not enough: reading it would walk logical blocks that cannot
    // exist until the engine ran out of patience.  Refusing is both honest and
    // fast.
    if (inode.size > FS_MAX_FILE_SIZE) {
        return FS_BAD_VOLUME;
    }
    if (offset >= inode.size) {
        return 0;
    }
    uint32 remaining = inode.size - offset;
    if (count < remaining) {
        remaining = count;
    }
    uint8* out = static_cast<uint8*>(buffer);
    uint32 done = 0;
    while (done < remaining) {
        const uint32 logical = offset / FS_BLOCK_SIZE;
        const uint32 within = offset % FS_BLOCK_SIZE;
        uint32 chunk = FS_BLOCK_SIZE - within;
        if (chunk > remaining - done) {
            chunk = remaining - done;
        }
        bool io_error = false;
        const uint32 block = block_number(inode, logical, &io_error);
        if (io_error) {
            return FS_IO;
        }
        if (block == 0) {
            // A hole inside the size is not possible with the packer this project
            // builds volumes with, but reading it as zeros is the only sane answer
            // and costs one branch.
            memset(out + done, 0, chunk);
        } else {
            uint8 raw[FS_BLOCK_SIZE];
            if (!block_read(block, raw)) {
                return FS_IO;
            }
            memcpy(out + done, raw + within, chunk);
        }
        done += chunk;
        offset += chunk;
    }
    return static_cast<int32>(done);
}

// --------------------------------------------------------------- directories

uint32 fs_dir_slots(const Inode& directory) {
    if (directory.type != FS_TYPE_DIR) {
        return 0;
    }
    return directory.size / FS_DIRENT_SIZE;
}

int32 fs_dir_entry(const Inode& directory, uint32 slot, Dirent* out) {
    if (directory.type != FS_TYPE_DIR) {
        return FS_NOT_DIR;
    }
    if (slot >= fs_dir_slots(directory)) {
        return FS_NO_ENT;
    }
    const int32 copied = fs_read_data(directory, slot * FS_DIRENT_SIZE, out,
                                      FS_DIRENT_SIZE);
    if (copied < 0) {
        return copied;
    }
    if (copied != static_cast<int32>(FS_DIRENT_SIZE)) {
        return FS_IO;
    }
    return FS_OK;
}

uint32 fs_dir_live_entries(const Inode& directory) {
    uint32 live = 0;
    const uint32 slots = fs_dir_slots(directory);
    for (uint32 slot = 0; slot < slots; ++slot) {
        Dirent entry;
        if (fs_dir_entry(directory, slot, &entry) < 0) {
            break;
        }
        if (entry.name[0] != '\0') {
            ++live;
        }
    }
    return live;
}

namespace {

int32 lookup_in(const Inode& directory, const char* name, uint32* out_inode) {
    const uint32 slots = fs_dir_slots(directory);
    for (uint32 slot = 0; slot < slots; ++slot) {
        Dirent entry;
        const int32 status = fs_dir_entry(directory, slot, &entry);
        if (status < 0) {
            return status;
        }
        if (entry.name[0] == '\0') {
            continue;                       // a slot left by a removed entry
        }
        if (dirent_matches(entry, name)) {
            *out_inode = entry.inode;
            return FS_OK;
        }
    }
    return FS_NO_ENT;
}

}  // namespace

int32 fs_resolve(const char* path, uint32* out_inode) {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    uint32 current = g_super.root_inode;
    uint32 depth = 0;
    const char* cursor = path;
    while (*cursor != '\0') {
        while (*cursor == '/') {
            ++cursor;
        }
        if (*cursor == '\0') {
            break;
        }
        char component[FS_NAME_MAX + 1];
        uint32 length = 0;
        while (*cursor != '\0' && *cursor != '/') {
            if (length >= FS_NAME_MAX) {
                return FS_NAME_TOO_LONG;
            }
            component[length++] = *cursor++;
        }
        component[length] = '\0';

        if (text_equal(component, ".")) {
            continue;
        }
        if (text_equal(component, "..")) {
            // No parent links are stored, so ".." cannot be answered honestly.
            // Saying so is better than guessing a directory.
            return FS_UNSUPPORTED;
        }
        if (++depth > FS_MAX_DEPTH) {
            return FS_UNSUPPORTED;
        }
        Inode directory;
        int32 status = fs_inode_read(current, &directory);
        if (status < 0) {
            return status;
        }
        if (directory.type != FS_TYPE_DIR) {
            return FS_NOT_DIR;
        }
        uint32 child = 0;
        status = lookup_in(directory, component, &child);
        if (status < 0) {
            return status;
        }
        if (child >= FS_INODE_COUNT) {
            return FS_NO_ENT;
        }
        current = child;
    }
    *out_inode = current;
    return FS_OK;
}

int32 fs_stat(const char* path, FsStat* out) {
    uint32 index = 0;
    int32 status = fs_resolve(path, &index);
    if (status < 0) {
        return status;
    }
    Inode inode;
    status = fs_inode_read(index, &inode);
    if (status < 0) {
        return status;
    }
    if (inode.type != FS_TYPE_FILE && inode.type != FS_TYPE_DIR) {
        return FS_NO_ENT;
    }
    if (inode.size > FS_MAX_FILE_SIZE) {
        return FS_BAD_VOLUME;
    }
    out->inode = index;
    out->type = inode.type;
    out->size = inode.size;
    out->blocks = inode_data_blocks(inode);
    out->indirect = inode.blocks[FS_DIRECT_BLOCKS] != 0;
    return FS_OK;
}

// -------------------------------------------------------------- the writes

namespace {

// Marks the volume as not cleanly unmounted, once per session.  This is the flag
// the next boot reports, and the only way `fsck` can tell a power cut from an
// ordinary reboot.
void mark_dirty() {
    if (g_super.state != FS_STATE_DIRTY) {
        g_super.state = FS_STATE_DIRTY;
        super_write();
    }
}

// Writes the metadata an operation changed.  The bitmaps go first: an inode that
// reaches the disk while the allocator still believes its blocks are free is the
// one inconsistency that loses data, and the order is what prevents it.
bool flush_metadata() {
    if (g_metadata_dirty) {
        if (!bitmaps_write()) {
            return false;
        }
        g_metadata_dirty = false;
    }
    return super_write();
}

int32 alloc_block(uint32* out) {
    for (uint32 index = g_super.first_data_block; index < FS_BLOCK_COUNT; ++index) {
        if (bit_used(g_block_bitmap, index)) {
            continue;
        }
        set_bit(g_block_bitmap, index, true);
        // Written now rather than at the end of the operation: the caller is about
        // to record this block in an inode, and that inode must not reach the disk
        // before the allocation does.
        if (!bitmaps_write()) {
            set_bit(g_block_bitmap, index, false);
            return FS_IO;
        }
        if (g_super.free_blocks > 0) {
            --g_super.free_blocks;
        }
        *out = index;
        return FS_OK;
    }
    return FS_NO_SPACE;
}

// Freeing only clears the bit in RAM; the bitmap is written by flush_metadata() at
// the end of the operation.  The delay is safe in this direction -- the worst a
// crash can leave behind is a block that is marked used and referenced by nothing,
// which is a leak, and leaks are what fsck is for.
void release_block(uint32 index) {
    if (index < g_super.first_data_block || index >= FS_BLOCK_COUNT) {
        return;
    }
    set_bit(g_block_bitmap, index, false);
    ++g_super.free_blocks;
    g_metadata_dirty = true;
}

int32 alloc_inode(uint8 type, uint32* out) {
    for (uint32 index = 1; index < FS_INODE_COUNT; ++index) {
        if (bit_used(g_inode_bitmap, index)) {
            continue;
        }
        set_bit(g_inode_bitmap, index, true);
        if (!bitmaps_write()) {
            set_bit(g_inode_bitmap, index, false);
            return FS_IO;
        }
        if (g_super.free_inodes > 0) {
            --g_super.free_inodes;
        }
        Inode fresh;
        memset(&fresh, 0, sizeof(fresh));
        fresh.type = type;
        if (!inode_write(index, fresh)) {
            return FS_IO;
        }
        *out = index;
        return FS_OK;
    }
    return FS_NO_INODE;
}

// An inode bitmap that says "used" next to an inode table entry that says "free" is
// a contradiction a reader will trip over, so both are written here rather than
// letting one lag behind the other.
void release_inode(uint32 index) {
    if (index == 0 || index >= FS_INODE_COUNT) {
        return;
    }
    Inode empty;
    memset(&empty, 0, sizeof(empty));
    inode_write(index, empty);
    set_bit(g_inode_bitmap, index, false);
    ++g_super.free_inodes;
    bitmaps_write();
}

// The physical block that holds logical block `logical`, allocating it (and the
// indirect block, and a slot in it) as needed.
int32 block_for_write(Inode& inode, uint32 logical, uint32* out_block) {
    if (logical < FS_DIRECT_BLOCKS) {
        if (inode.blocks[logical] == 0) {
            uint32 block = 0;
            const int32 status = alloc_block(&block);
            if (status < 0) {
                return status;
            }
            inode.blocks[logical] = static_cast<uint16>(block);
        }
        *out_block = inode.blocks[logical];
        return FS_OK;
    }
    const uint32 entry = logical - FS_DIRECT_BLOCKS;
    if (entry >= FS_POINTERS_PER_BLOCK) {
        return FS_FILE_TOO_BIG;
    }
    if (inode.blocks[FS_DIRECT_BLOCKS] == 0) {
        uint32 block = 0;
        const int32 status = alloc_block(&block);
        if (status < 0) {
            return status;
        }
        inode.blocks[FS_DIRECT_BLOCKS] = static_cast<uint16>(block);
        // A pointer table holding whatever was on those sectors before is how a
        // file ends up owning blocks that belong to another file, so it starts as
        // 256 zeroes rather than as whatever the last file left there.
        uint8 fresh[FS_BLOCK_SIZE];
        memset(fresh, 0, sizeof(fresh));
        if (!block_write(block, fresh)) {
            return FS_IO;
        }
    }
    const uint32 indirect = inode.blocks[FS_DIRECT_BLOCKS];
    uint8 raw[FS_BLOCK_SIZE];
    if (!block_read(indirect, raw)) {
        return FS_IO;
    }
    uint16 block = read_u16(raw + entry * 2);
    if (block == 0) {
        uint32 allocated = 0;
        const int32 status = alloc_block(&allocated);
        if (status < 0) {
            return status;
        }
        write_u16(raw + entry * 2, static_cast<uint16>(allocated));
        if (!block_write(indirect, raw)) {
            return FS_IO;
        }
        block = static_cast<uint16>(allocated);
    }
    *out_block = block;
    return FS_OK;
}

int32 write_data(Inode& inode, uint32 offset, const void* buffer, uint32 count) {
    if (offset + count > FS_MAX_FILE_SIZE) {
        return FS_FILE_TOO_BIG;
    }
    const uint8* in = static_cast<const uint8*>(buffer);
    uint32 done = 0;
    while (done < count) {
        const uint32 position = offset + done;
        const uint32 logical = position / FS_BLOCK_SIZE;
        const uint32 within = position % FS_BLOCK_SIZE;
        uint32 chunk = FS_BLOCK_SIZE - within;
        if (chunk > count - done) {
            chunk = count - done;
        }
        uint32 block = 0;
        const int32 status = block_for_write(inode, logical, &block);
        if (status < 0) {
            return status;
        }
        uint8 raw[FS_BLOCK_SIZE];
        if (!block_read(block, raw)) {
            return FS_IO;
        }
        memcpy(raw + within, in + done, chunk);
        if (!block_write(block, raw)) {
            return FS_IO;
        }
        done += chunk;
    }
    return FS_OK;
}

// Frees every block at or past `size`, in the order that keeps the medium
// consistent: the inode stops referring to the blocks before any of them becomes
// free, so a crash leaves leaked space rather than a file pointing at free space.
int32 release_from(uint32 index, Inode& inode, uint32 size) {
    const uint32 first_free = (size + FS_BLOCK_SIZE - 1) / FS_BLOCK_SIZE;
    g_release_count = 0;

    for (uint32 logical = first_free; logical < FS_DIRECT_BLOCKS; ++logical) {
        if (inode.blocks[logical] != 0) {
            g_release_list[g_release_count++] = inode.blocks[logical];
            inode.blocks[logical] = 0;
        }
    }
    const uint32 indirect = inode.blocks[FS_DIRECT_BLOCKS];
    if (indirect != 0) {
        uint8 raw[FS_BLOCK_SIZE];
        if (!block_read(indirect, raw)) {
            return FS_IO;
        }
        for (uint32 entry = 0; entry < FS_POINTERS_PER_BLOCK; ++entry) {
            if (FS_DIRECT_BLOCKS + entry < first_free) {
                continue;
            }
            const uint16 block = read_u16(raw + entry * 2);
            if (block != 0) {
                g_release_list[g_release_count++] = block;
                write_u16(raw + entry * 2, 0);
            }
        }
        if (first_free <= FS_DIRECT_BLOCKS) {
            memset(raw, 0, sizeof(raw));    // nothing left for the table to hold
        }
        if (!block_write(indirect, raw)) {
            return FS_IO;
        }
        if (first_free <= FS_DIRECT_BLOCKS) {
            g_release_list[g_release_count++] = indirect;
            inode.blocks[FS_DIRECT_BLOCKS] = 0;
        }
    }
    inode.size = size;
    if (!inode_write(index, inode)) {
        return FS_IO;
    }
    for (uint32 item = 0; item < g_release_count; ++item) {
        release_block(g_release_list[item]);
    }
    g_release_count = 0;
    return FS_OK;
}

// Places one dirent in a directory, growing the directory by a block if the slot
// is past its end.  A slot inside a zeroed hole reuses the block it lives in.
int32 write_dirent(uint32 dir_index, Inode& directory, uint32 slot,
                   const Dirent& entry) {
    const uint32 offset = slot * FS_DIRENT_SIZE;
    const uint32 within = offset % FS_BLOCK_SIZE;
    uint32 block = 0;
    const int32 status = block_for_write(directory, offset / FS_BLOCK_SIZE, &block);
    if (status < 0) {
        return status;
    }
    uint8 raw[FS_BLOCK_SIZE];
    if (!block_read(block, raw)) {
        return FS_IO;
    }
    memcpy(raw + within, &entry, FS_DIRENT_SIZE);
    if (!block_write(block, raw)) {
        return FS_IO;
    }
    if (offset + FS_DIRENT_SIZE > directory.size) {
        directory.size = offset + FS_DIRENT_SIZE;
        if (!inode_write(dir_index, directory)) {
            return FS_IO;
        }
    }
    return FS_OK;
}

// "a/b/c" -> parent "/a/b", name "c".  The last component is the one being created
// or removed, so it must be a plain name: no dots, no slashes, no emptiness.
int32 resolve_parent(const char* path, char* name, uint32 name_size, uint32* parent) {
    const uint32 length = text_length(path);
    uint32 slash = length;
    for (uint32 index = length; index > 0; --index) {
        if (path[index - 1] == '/') {
            slash = index - 1;
            break;
        }
    }
    const char* base = (slash == length) ? path : path + slash + 1;
    const uint32 base_length = text_length(base);
    if (base_length == 0) {
        return FS_NO_ENT;
    }
    if (base_length > FS_NAME_MAX) {
        return FS_NAME_TOO_LONG;
    }
    if (text_equal(base, ".") || text_equal(base, "..")) {
        return FS_UNSUPPORTED;
    }
    copy_name(name, base, name_size);
    if (slash == length) {
        *parent = g_super.root_inode;
        return FS_OK;
    }
    char directory[FS_MAX_PATH];
    if (slash >= sizeof(directory)) {
        return FS_UNSUPPORTED;
    }
    for (uint32 index = 0; index < slash; ++index) {
        directory[index] = path[index];
    }
    directory[slash] = '\0';
    return fs_resolve(directory, parent);
}

int32 dir_add(uint32 dir_index, const char* name, uint16 child) {
    Inode directory;
    int32 status = fs_inode_read(dir_index, &directory);
    if (status < 0) {
        return status;
    }
    if (directory.type != FS_TYPE_DIR) {
        return FS_NOT_DIR;
    }
    Dirent entry;
    memset(&entry, 0, sizeof(entry));
    copy_name(entry.name, name, sizeof(entry.name));
    entry.inode = child;

    const uint32 slots = fs_dir_slots(directory);
    for (uint32 slot = 0; slot < slots; ++slot) {
        Dirent existing;
        status = fs_dir_entry(directory, slot, &existing);
        if (status < 0) {
            return status;
        }
        if (existing.name[0] == '\0') {
            return write_dirent(dir_index, directory, slot, entry);
        }
    }
    return write_dirent(dir_index, directory, slots, entry);
}

int32 dir_remove(uint32 dir_index, const char* name, uint16* removed) {
    Inode directory;
    int32 status = fs_inode_read(dir_index, &directory);
    if (status < 0) {
        return status;
    }
    if (directory.type != FS_TYPE_DIR) {
        return FS_NOT_DIR;
    }
    const uint32 slots = fs_dir_slots(directory);
    for (uint32 slot = 0; slot < slots; ++slot) {
        Dirent entry;
        status = fs_dir_entry(directory, slot, &entry);
        if (status < 0) {
            return status;
        }
        if (entry.name[0] == '\0' || !dirent_matches(entry, name)) {
            continue;
        }
        if (removed != nullptr) {
            *removed = entry.inode;
        }
        // The slot is zeroed rather than the directory shrunk: the next creation
        // reuses the hole, so a directory that has held many files does not keep
        // growing while the files are gone.
        Dirent empty;
        memset(&empty, 0, sizeof(empty));
        return write_dirent(dir_index, directory, slot, empty);
    }
    return FS_NO_ENT;
}

}  // namespace

int32 fs_create(const char* path, uint8 type) {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    char name[FS_NAME_MAX + 1];
    uint32 parent_index = 0;
    int32 status = resolve_parent(path, name, sizeof(name), &parent_index);
    if (status < 0) {
        return status;
    }
    Inode parent;
    status = fs_inode_read(parent_index, &parent);
    if (status < 0) {
        return status;
    }
    if (parent.type != FS_TYPE_DIR) {
        return FS_NOT_DIR;
    }
    uint32 existing = 0;
    if (lookup_in(parent, name, &existing) == FS_OK) {
        return FS_EXISTS;
    }

    mark_dirty();
    uint32 index = 0;
    status = alloc_inode(type, &index);
    if (status < 0) {
        return status;
    }
    status = dir_add(parent_index, name, static_cast<uint16>(index));
    if (status < 0) {
        // Leave nothing behind: a half-created file is how a failed write turns
        // into a permanently full volume.
        release_inode(index);
        flush_metadata();
        return status;
    }
    if (!flush_metadata()) {
        return FS_IO;
    }
    return FS_OK;
}

int32 fs_write_file(const char* path, const void* data, uint32 count, bool truncate) {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    if (count > FS_MAX_FILE_SIZE) {
        return FS_FILE_TOO_BIG;
    }
    uint32 index = 0;
    int32 status = fs_resolve(path, &index);
    if (status == FS_NO_ENT) {
        status = fs_create(path, FS_TYPE_FILE);
        if (status < 0) {
            return status;
        }
        status = fs_resolve(path, &index);
    }
    if (status < 0) {
        return status;
    }
    Inode inode;
    status = fs_inode_read(index, &inode);
    if (status < 0) {
        return status;
    }
    if (inode.type != FS_TYPE_FILE) {
        return FS_IS_DIR;
    }

    mark_dirty();
    if (truncate && inode.size != 0) {
        status = release_from(index, inode, 0);
        if (status < 0) {
            return status;
        }
    }
    status = write_data(inode, 0, data, count);
    if (status < 0) {
        return status;
    }
    if (truncate || count > inode.size) {
        inode.size = count;
    }
    if (!inode_write(index, inode)) {
        return FS_IO;
    }
    if (!flush_metadata()) {
        return FS_IO;
    }
    return FS_OK;
}

int32 fs_unlink(const char* path) {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    char name[FS_NAME_MAX + 1];
    uint32 parent_index = 0;
    int32 status = resolve_parent(path, name, sizeof(name), &parent_index);
    if (status < 0) {
        return status;
    }
    Inode parent;
    status = fs_inode_read(parent_index, &parent);
    if (status < 0) {
        return status;
    }
    if (parent.type != FS_TYPE_DIR) {
        return FS_NOT_DIR;
    }
    uint32 index = 0;
    status = lookup_in(parent, name, &index);
    if (status < 0) {
        return status;
    }
    Inode victim;
    status = fs_inode_read(index, &victim);
    if (status < 0) {
        return status;
    }
    if (victim.type == FS_TYPE_DIR && fs_dir_live_entries(victim) != 0) {
        return FS_DIR_NOT_EMPTY;
    }
    if (victim.type != FS_TYPE_FILE && victim.type != FS_TYPE_DIR) {
        return FS_NO_ENT;
    }

    mark_dirty();
    // The directory entry goes first: from that moment the file is unreachable, so
    // freeing its blocks -- which happens next -- can only ever leak space.
    status = dir_remove(parent_index, name, nullptr);
    if (status < 0) {
        return status;
    }
    status = release_from(index, victim, 0);
    if (status < 0) {
        return status;
    }
    release_inode(index);
    if (!flush_metadata()) {
        return FS_IO;
    }
    return FS_OK;
}

int32 fs_sync() {
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    if (!flush_metadata()) {
        return FS_IO;
    }
    // Only the flag is left to write: every operation has already pushed its data
    // and metadata to the disk, so "sync" here means "this volume was unmounted
    // cleanly", which is what the next boot wants to know.
    if (g_super.state != FS_STATE_CLEAN) {
        g_super.state = FS_STATE_CLEAN;
        if (!super_write()) {
            return FS_IO;
        }
    }
    return FS_OK;
}

int32 fs_fsck(FsckReport* report) {
    memset(report, 0, sizeof(*report));
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    // Start from what is on the medium.  The RAM copy is what the allocator has
    // been using, and a repair pass that trusts it could preserve the very
    // inconsistency it was asked to find.
    uint8 block[FS_BLOCK_SIZE];
    if (!block_read(FS_BITMAP_BLOCK, block)) {
        return FS_IO;
    }
    memcpy(g_inode_bitmap, block, FS_INODE_BITMAP_BYTES);
    memcpy(g_block_bitmap, block + FS_INODE_BITMAP_BYTES, FS_BLOCK_BITMAP_BYTES);

    static uint8 referenced[FS_BLOCK_BITMAP_BYTES];
    static uint8 reachable[FS_INODE_BITMAP_BYTES];
    memset(referenced, 0, sizeof(referenced));
    memset(reachable, 0, sizeof(reachable));

    // The metadata blocks hold the volume's own bookkeeping; if one of them is
    // free, the next allocation would hand it to a file.
    for (uint32 index = 0; index < FS_FIRST_DATA_BLOCK; ++index) {
        if (!bit_used(g_block_bitmap, index)) {
            set_bit(g_block_bitmap, index, true);
            ++report->rescued_blocks;
            ++report->problems;
        }
    }

    for (uint32 index = 1; index < FS_INODE_COUNT; ++index) {
        if (!bit_used(g_inode_bitmap, index)) {
            continue;
        }
        Inode inode;
        if (fs_inode_read(index, &inode) < 0) {
            ++report->problems;
            continue;
        }
        if (inode.type == FS_TYPE_FREE) {
            // Allocated in the bitmap, free in the table: one of the two is wrong
            // and the table is the one that carries the meaning, so the bit goes.
            set_bit(g_inode_bitmap, index, false);
            if (g_super.free_inodes < FS_INODE_COUNT) {
                ++g_super.free_inodes;
            }
            ++report->problems;
            continue;
        }
        if (inode.type != FS_TYPE_FILE && inode.type != FS_TYPE_DIR) {
            ++report->problems;
            continue;
        }
        for (uint32 logical = 0; logical < FS_DIRECT_BLOCKS; ++logical) {
            const uint32 pointed = inode.blocks[logical];
            if (pointed == 0) {
                continue;
            }
            if (pointed >= FS_BLOCK_COUNT) {
                ++report->problems;         // a pointer off the volume: not repairable
            } else {
                set_bit(referenced, pointed, true);
            }
        }
        const uint32 indirect = inode.blocks[FS_DIRECT_BLOCKS];
        if (indirect != 0 && indirect < FS_BLOCK_COUNT) {
            set_bit(referenced, indirect, true);
            uint8 raw[FS_BLOCK_SIZE];
            if (block_read(indirect, raw)) {
                for (uint32 entry = 0; entry < FS_POINTERS_PER_BLOCK; ++entry) {
                    const uint32 pointed = read_u16(raw + entry * 2);
                    if (pointed == 0) {
                        continue;
                    }
                    if (pointed >= FS_BLOCK_COUNT) {
                        ++report->problems;
                    } else {
                        set_bit(referenced, pointed, true);
                    }
                }
            } else {
                ++report->problems;
            }
        }
    }

    // Reachability, for the report: an inode nothing points at is space the user
    // cannot get back, but freeing it would be guessing what they meant.
    static uint32 stack[FS_INODE_COUNT];
    uint32 stack_size = 0;
    stack[stack_size++] = g_super.root_inode;
    while (stack_size > 0) {
        const uint32 index = stack[--stack_size];
        if (index >= FS_INODE_COUNT || bit_used(reachable, index)) {
            continue;
        }
        set_bit(reachable, index, true);
        Inode directory;
        if (fs_inode_read(index, &directory) < 0 || directory.type != FS_TYPE_DIR) {
            continue;
        }
        const uint32 slots = fs_dir_slots(directory);
        for (uint32 slot = 0; slot < slots; ++slot) {
            Dirent entry;
            if (fs_dir_entry(directory, slot, &entry) < 0) {
                break;
            }
            if (entry.name[0] == '\0') {
                continue;
            }
            if (entry.inode >= FS_INODE_COUNT || !bit_used(g_inode_bitmap, entry.inode)) {
                ++report->problems;         // a name pointing at nothing
                continue;
            }
            if (stack_size < FS_INODE_COUNT) {
                stack[stack_size++] = entry.inode;
            }
        }
    }
    for (uint32 index = 1; index < FS_INODE_COUNT; ++index) {
        if (bit_used(g_inode_bitmap, index) && !bit_used(reachable, index)) {
            ++report->orphan_inodes;
            ++report->problems;
        }
    }

    for (uint32 index = FS_FIRST_DATA_BLOCK; index < FS_BLOCK_COUNT; ++index) {
        const bool used = bit_used(g_block_bitmap, index);
        const bool points = bit_used(referenced, index);
        if (points && !used) {
            // A live file pointing at a block the allocator would hand out again.
            set_bit(g_block_bitmap, index, true);
            ++report->rescued_blocks;
            ++report->problems;
        } else if (!points && used) {
            set_bit(g_block_bitmap, index, false);
            ++report->reclaimed_blocks;
            ++report->problems;
        }
    }

    const uint32 free_blocks = count_free(g_block_bitmap, FS_FIRST_DATA_BLOCK,
                                          FS_BLOCK_COUNT);
    const uint32 free_inodes = count_free(g_inode_bitmap, 1, FS_INODE_COUNT);
    report->counts_wrong = free_blocks != g_super.free_blocks ||
                           free_inodes != g_super.free_inodes;
    g_super.free_blocks = free_blocks;
    g_super.free_inodes = free_inodes;
    if (!bitmaps_write()) {
        return FS_IO;
    }
    g_metadata_dirty = false;
    g_counts_disagree = false;
    // The structure is consistent again whether or not an orphan was reported, so
    // the volume may call itself cleanly unmounted.
    g_super.state = FS_STATE_CLEAN;
    if (!super_write()) {
        return FS_IO;
    }
    return FS_OK;
}

// --------------------------------------------------------------- self check

int32 fs_verify_manifest(ManifestReport* report) {
    memset(report, 0, sizeof(*report));
    if (!g_mounted) {
        return FS_NOT_MOUNTED;
    }
    uint32 manifest_index = 0;
    int32 status = fs_resolve("/manifest", &manifest_index);
    if (status < 0) {
        copy_name(report->first_problem, "there is no /manifest on the volume",
                  sizeof(report->first_problem));
        return status;
    }
    Inode manifest;
    status = fs_inode_read(manifest_index, &manifest);
    if (status < 0) {
        return status;
    }
    if (manifest.type != FS_TYPE_FILE || manifest.size >= MANIFEST_MAX) {
        copy_name(report->first_problem, "/manifest is not a readable file",
                  sizeof(report->first_problem));
        return FS_BAD_VOLUME;
    }
    static char text[MANIFEST_MAX];
    const int32 copied = fs_read_data(manifest, 0, text, manifest.size);
    if (copied != static_cast<int32>(manifest.size)) {
        return copied < 0 ? copied : FS_IO;
    }
    text[manifest.size] = '\0';

    const char* cursor = text;
    while (*cursor != '\0') {
        char name[FS_MAX_PATH];
        uint32 length = 0;
        while (*cursor != '\0' && *cursor != ' ' && *cursor != '\n') {
            if (length + 1 < sizeof(name)) {
                name[length++] = *cursor;
            }
            ++cursor;
        }
        name[length] = '\0';
        while (*cursor == ' ') {
            ++cursor;
        }
        uint32 stated_size = 0;
        while (is_digit(*cursor)) {
            stated_size = stated_size * 10 + static_cast<uint32>(*cursor - '0');
            ++cursor;
        }
        while (*cursor == ' ') {
            ++cursor;
        }
        uint32 stated_sum = 0;
        uint32 digits = 0;
        while (digits < 8) {
            const int value = hex_value(*cursor);
            if (value < 0) {
                break;
            }
            stated_sum = stated_sum * 16 + static_cast<uint32>(value);
            ++cursor;
            ++digits;
        }
        while (*cursor != '\0' && *cursor != '\n') {
            ++cursor;
        }
        if (*cursor == '\n') {
            ++cursor;
        }
        if (length == 0) {
            continue;                       // a blank line is not a claim
        }
        ++report->lines;

        char path[FS_MAX_PATH + 2];
        path[0] = '/';
        copy_name(path + 1, name, FS_MAX_PATH + 1);
        uint32 index = 0;
        status = fs_resolve(path, &index);
        if (status < 0) {
            ++report->mismatched;
            copy_name(report->first_problem, name, sizeof(report->first_problem));
            continue;
        }
        Inode file;
        status = fs_inode_read(index, &file);
        if (status < 0 || file.type != FS_TYPE_FILE) {
            ++report->mismatched;
            copy_name(report->first_problem, name, sizeof(report->first_problem));
            continue;
        }
        if (file.size != stated_size) {
            ++report->mismatched;
            copy_name(report->first_problem, name, sizeof(report->first_problem));
            continue;
        }
        uint32 computed = 0;
        uint32 offset = 0;
        uint8 chunk[FS_BLOCK_SIZE];
        bool failed = false;
        while (offset < file.size) {
            uint32 want = file.size - offset;
            if (want > sizeof(chunk)) {
                want = sizeof(chunk);
            }
            const int32 got = fs_read_data(file, offset, chunk, want);
            if (got <= 0) {
                failed = true;
                break;
            }
            for (int32 index2 = 0; index2 < got; ++index2) {
                computed = computed * 31u + chunk[index2];
            }
            offset += static_cast<uint32>(got);
        }
        if (failed) {
            ++report->mismatched;
            copy_name(report->first_problem, name, sizeof(report->first_problem));
            continue;
        }
        ++report->checked;
        if (computed != stated_sum) {
            ++report->mismatched;
            copy_name(report->first_problem, name, sizeof(report->first_problem));
        }
    }
    return FS_OK;
}

// ----------------------------------------------------------------- reporting

void fs_report_volume() {
    if (!g_mounted) {
        kprintf("fs: not mounted (%s)\n", g_problem);
        return;
    }
    char label[FS_LABEL_SIZE + 1];
    copy_name(label, g_super.label, sizeof(label));
    kprintf("fs: myfs v%u '%s', %u blocks of %u bytes, %u inodes\n",
            g_super.version, label, g_super.block_count, g_super.block_size,
            g_super.inode_count);
    kprintf("fs: partition LBA %u, %u sectors; %u data blocks after metadata\n",
            g_partition_lba, g_partition_sectors,
            FS_BLOCK_COUNT - FS_FIRST_DATA_BLOCK);
    kprintf("fs: state %s, %u free blocks, %u free inodes\n",
            g_super.state == FS_STATE_CLEAN ? "clean" : "dirty",
            g_super.free_blocks, g_super.free_inodes);
    if (g_super.state != FS_STATE_CLEAN) {
        console_puts("fs: the volume was not unmounted cleanly; `fsck` will check "
                     "it and reclaim whatever a lost write left behind\n");
    }
    if (g_counts_disagree) {
        kprintf("fs: warning: the bitmaps say %u free blocks and %u free inodes\n",
                fs_bitmap_free_blocks(), fs_bitmap_free_inodes());
    }
}

void fs_report_usage() {
    if (!g_mounted) {
        kprintf("df: no filesystem mounted (%s)\n", g_problem);
        return;
    }
    const uint32 free_blocks = fs_bitmap_free_blocks();
    const uint32 free_inodes = fs_bitmap_free_inodes();
    kprintf("df: %u blocks of %u bytes: %u used, %u free\n",
            FS_BLOCK_COUNT, FS_BLOCK_SIZE,
            FS_BLOCK_COUNT - FS_FIRST_DATA_BLOCK - free_blocks, free_blocks);
    kprintf("df: %u inodes: %u used, %u free\n",
            FS_INODE_COUNT, FS_INODE_COUNT - 1 - free_inodes, free_inodes);
}

}  // namespace myos
