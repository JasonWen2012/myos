// myos 32-bit kernel: the ATA PIO block device.
//
// The kernel drives the primary IDE channel by polling: no DMA, no interrupt, no
// PCI scan.  For a single disk in QEMU and on any machine with a legacy IDE
// controller that is enough, and it keeps the driver small enough to read in one
// sitting -- the same reason the rest of this kernel exists.
//
// Everything here is bounded.  A hard disk that never answers must produce an
// error, not a hang: a kernel that spins forever on a status register looks
// exactly like a kernel that works, right up until someone waits for it.

#pragma once

#include "types.h"

namespace myos {

constexpr uint16 ATA_SECTOR_SIZE = 512;

// The primary channel's command block and its alternate status port.  The
// alternate port is readable without touching the interrupt flag, and writing bit
// 1 of it disables the drive's interrupts, which is what a polling driver wants:
// otherwise every completed command raises IRQ14 at a PIC that has no handler for
// it and the interrupt is silently lost.
constexpr uint16 ATA_IO_BASE = 0x01F0;
constexpr uint16 ATA_CONTROL = 0x03F6;
constexpr uint8 ATA_CONTROL_NIEN = 0x02;
constexpr uint8 ATA_CONTROL_RESET = 0x04;

// Status register bits, at ATA_IO_BASE + 7.
constexpr uint8 ATA_STATUS_ERR = 0x01;
constexpr uint8 ATA_STATUS_DRQ = 0x08;
constexpr uint8 ATA_STATUS_DF = 0x20;
constexpr uint8 ATA_STATUS_DRDY = 0x40;
constexpr uint8 ATA_STATUS_BSY = 0x80;

// Commands used here.
constexpr uint8 ATA_CMD_READ_SECTORS = 0x20;
constexpr uint8 ATA_CMD_WRITE_SECTORS = 0x30;
constexpr uint8 ATA_CMD_FLUSH_CACHE = 0xE7;
constexpr uint8 ATA_CMD_IDENTIFY = 0xEC;

struct AtaDevice {
    bool present;
    bool lba_supported;
    uint32 sectors;             // LBA28 capacity, as IDENTIFY reports it
    char model[41];             // IDENTIFY words 27..46, byte-swapped and trimmed
};

// Probes the primary master once and remembers the answer; later calls return the
// same record, so asking the hardware does not depend on asking at the right time.
const AtaDevice* ata_probe();

// Sector I/O.  `count` sectors are transferred to and from a flat buffer, one
// sector at a time, so a caller never has to care about alignment or the drive's
// 8-bit sector count.  Both return false and leave the buffer partly written when
// the device reports an error or stops answering.
bool ata_read(uint32 lba, uint32 count, void* buffer);
bool ata_write(uint32 lba, uint32 count, const void* buffer);
bool ata_flush();

// How many commands had to be abandoned because the device never cleared BSY or
// never set DRQ.  Reported by `blk`, and the only way to tell "the disk is slow"
// from "the driver gave up".
uint32 ata_timeouts();

}  // namespace myos
