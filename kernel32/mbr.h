// myos 32-bit kernel: reading the MBR partition table.
//
// The kernel has to find the filesystem volume on whatever disk holds it, and the
// only thing both disks in this project have in common is a partition table at
// LBA 0.  So the volume is located by type byte, never by a hard-coded LBA: the
// boot image puts it in one place, a user's data disk could put it anywhere, and
// nothing in the kernel has to know which disk it was booted from.

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 MBR_PARTITION_TABLE_OFFSET = 446;
constexpr uint32 MBR_PARTITION_ENTRY_COUNT = 4;
constexpr uint32 MBR_SIGNATURE_OFFSET = 510;
constexpr uint8 MBR_SIGNATURE_LOW = 0x55;
constexpr uint8 MBR_SIGNATURE_HIGH = 0xAA;

struct PartitionEntry {
    uint8 status;               // +0  0x80 for the bootable entry
    uint8 chs_start[3];         // +1
    uint8 type;                 // +4  0 means the entry is unused
    uint8 chs_end[3];           // +5
    uint32 lba_start;           // +8
    uint32 sectors;             // +12
};
static_assert(sizeof(PartitionEntry) == 16, "an MBR partition entry is 16 bytes");
static_assert(__builtin_offsetof(PartitionEntry, type) == 4,
              "the type byte is at +4 in a partition entry");
static_assert(__builtin_offsetof(PartitionEntry, lba_start) == 8,
              "the LBA of a partition is at +8");
static_assert(__builtin_offsetof(PartitionEntry, sectors) == 12,
              "the sector count of a partition is at +12");

struct Partition {
    uint8 type;
    uint32 lba_start;
    uint32 sectors;
};

// Fills `out` and returns true when the disk at LBA 0 has a partition table and
// the entry with `type` in it is usable.  False covers every way that can fail --
// no disk, no signature, no such entry, or an entry that runs off the end of the
// device -- because the caller's response is the same in all of them: the volume
// is not there.
bool mbr_find_partition(uint8 type, Partition* out);

// Why the last mbr_find_partition call failed, for the message the shell prints.
// A single sentence, so the difference between "no disk" and "malformed table" is
// visible without a debugger.
const char* mbr_last_problem();

}  // namespace myos
