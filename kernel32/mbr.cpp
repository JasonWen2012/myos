// myos 32-bit kernel: the MBR walk behind kernel32/mbr.h.

#include "mbr.h"

#include "ata.h"
#include "libc.h"

namespace myos {
namespace {

// Named g_mbr_problem rather than g_problem because the in-tree linker keys
// internal symbols by their mangled name, and two anonymous-namespace variables
// with the same name in different translation units then collide as if they were
// the same object -- which is exactly what happened when this file and fs.cpp
// both called theirs g_problem.
const char* g_mbr_problem = "not looked at yet";

// Fields are read byte by byte rather than through the PartitionEntry struct: the
// table starts at offset 446 of a stack buffer, and reading a 32-bit member
// through a pointer the compiler may assume is 4-byte aligned is exactly the kind
// of thing that works until it does not.
uint32 read_u32(const uint8* at) {
    return static_cast<uint32>(at[0]) | (static_cast<uint32>(at[1]) << 8) |
           (static_cast<uint32>(at[2]) << 16) | (static_cast<uint32>(at[3]) << 24);
}

}  // namespace

const char* mbr_last_problem() {
    return g_mbr_problem;
}

bool mbr_find_partition(uint8 type, Partition* out) {
    const AtaDevice* device = ata_probe();
    if (!device->present) {
        g_mbr_problem = "no ATA device on the primary channel";
        return false;
    }
    // Sector 0 of the disk, which is both the boot sector and the partition table
    // on the media this project builds.
    uint8 sector[ATA_SECTOR_SIZE];
    if (!ata_read(0, 1, sector)) {
        g_mbr_problem = "reading LBA 0 failed";
        return false;
    }
    if (sector[MBR_SIGNATURE_OFFSET] != MBR_SIGNATURE_LOW ||
        sector[MBR_SIGNATURE_OFFSET + 1] != MBR_SIGNATURE_HIGH) {
        g_mbr_problem = "LBA 0 has no 0x55AA signature, so it is not an MBR";
        return false;
    }

    for (uint32 index = 0; index < MBR_PARTITION_ENTRY_COUNT; ++index) {
        const uint8* entry = sector + MBR_PARTITION_TABLE_OFFSET +
                             index * sizeof(PartitionEntry);
        const uint8 entry_type = entry[4];
        if (entry_type == 0) {
            continue;                       // an unused slot, not an error
        }
        const uint32 lba_start = read_u32(entry + 8);
        const uint32 sectors = read_u32(entry + 12);
        if (sectors == 0) {
            continue;                       // an empty entry with a type left in it
        }
        if (lba_start == 0) {
            g_mbr_problem = "a partition claims to start at LBA 0, where the MBR is";
            return false;
        }
        // A partition that runs past the end of the device would have the kernel
        // read sectors the disk does not have, so it is refused here rather than
        // turning into an I/O error in the middle of a file.
        if (lba_start + sectors > device->sectors) {
            g_mbr_problem = "a partition runs past the end of the device";
            return false;
        }
        if (entry_type != type) {
            continue;
        }
        out->type = entry_type;
        out->lba_start = lba_start;
        out->sectors = sectors;
        g_mbr_problem = "found";
        return true;
    }
    g_mbr_problem = "no partition of the expected type in the MBR";
    return false;
}

}  // namespace myos
