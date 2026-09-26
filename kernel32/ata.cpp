// myos 32-bit kernel: the ATA PIO driver behind kernel32/ata.h.

#include "ata.h"

#include "io.h"
#include "libc.h"

namespace myos {
namespace {

// Register offsets from the command block base.
constexpr uint16 REG_DATA = 0;
constexpr uint16 REG_ERROR = 1;
constexpr uint16 REG_FEATURES = 1;
constexpr uint16 REG_SECTOR_COUNT = 2;
constexpr uint16 REG_LBA_LOW = 3;
constexpr uint16 REG_LBA_MID = 4;
constexpr uint16 REG_LBA_HIGH = 5;
constexpr uint16 REG_DRIVE = 6;
constexpr uint16 REG_STATUS = 7;
constexpr uint16 REG_COMMAND = 7;

// Polling bound.  A status read is a bus cycle, so a few hundred thousand of them
// is tens of milliseconds of real time -- far longer than any working disk takes
// to clear BSY, and short enough that a dead device fails before a user gives up.
constexpr uint32 ATA_POLL_LIMIT = 200000;

AtaDevice g_device = {false, false, 0, {0}};
bool g_probed = false;
uint32 g_timeouts = 0;

// The 400 ns a drive may need to update its status after a command: four reads of
// an unused port, which is the classic delay and costs nothing.
void delay_400ns() {
    io_wait();
    io_wait();
    io_wait();
    io_wait();
}

uint8 status() {
    return inb(ATA_IO_BASE + REG_STATUS);
}

void select_drive(uint8 head_and_mode) {
    outb(ATA_IO_BASE + REG_DRIVE, head_and_mode);
    delay_400ns();
}

// Wait for the drive to be ready to accept a command.  A status of 0 means the
// bus is floating and 0xFF means nothing is driving it, which are the two ways a
// channel with no drive behind it answers.
bool wait_not_busy() {
    for (uint32 spin = 0; spin < ATA_POLL_LIMIT; ++spin) {
        const uint8 value = status();
        if (value == 0x00 || value == 0xFF) {
            return false;
        }
        if ((value & ATA_STATUS_BSY) == 0) {
            return true;
        }
    }
    ++g_timeouts;
    return false;
}

// Wait for the drive to hand over data, and refuse to proceed when it says the
// command failed.  Checking ERR here rather than at the end is what keeps a bad
// sector from being read as 512 bytes of whatever was in the buffer.
bool wait_for_data() {
    for (uint32 spin = 0; spin < ATA_POLL_LIMIT; ++spin) {
        const uint8 value = status();
        if (value == 0x00 || value == 0xFF) {
            return false;
        }
        if ((value & (ATA_STATUS_ERR | ATA_STATUS_DF)) != 0) {
            return false;
        }
        if ((value & ATA_STATUS_BSY) == 0 && (value & ATA_STATUS_DRQ) != 0) {
            return true;
        }
    }
    ++g_timeouts;
    return false;
}

void read_sector_words(uint8* out) {
    for (uint32 index = 0; index < ATA_SECTOR_SIZE / 2; ++index) {
        const uint16 word = inw(ATA_IO_BASE + REG_DATA);
        out[index * 2] = static_cast<uint8>(word & 0xFF);
        out[index * 2 + 1] = static_cast<uint8>(word >> 8);
    }
}

void write_sector_words(const uint8* in) {
    for (uint32 index = 0; index < ATA_SECTOR_SIZE / 2; ++index) {
        const uint16 word = static_cast<uint16>(in[index * 2] |
                                                (in[index * 2 + 1] << 8));
        outw(ATA_IO_BASE + REG_DATA, word);
    }
}

// The LBA28 register set and command byte shared by read and write.
bool start_transfer(uint32 lba, uint8 command) {
    if (!wait_not_busy()) {
        return false;
    }
    select_drive(static_cast<uint8>(0xE0 | ((lba >> 24) & 0x0F)));
    outb(ATA_IO_BASE + REG_FEATURES, 0);
    outb(ATA_IO_BASE + REG_SECTOR_COUNT, 1);
    outb(ATA_IO_BASE + REG_LBA_LOW, static_cast<uint8>(lba & 0xFF));
    outb(ATA_IO_BASE + REG_LBA_MID, static_cast<uint8>((lba >> 8) & 0xFF));
    outb(ATA_IO_BASE + REG_LBA_HIGH, static_cast<uint8>((lba >> 16) & 0xFF));
    outb(ATA_IO_BASE + REG_COMMAND, command);
    delay_400ns();
    return true;
}

bool read_one(uint32 lba, uint8* out) {
    if (!start_transfer(lba, ATA_CMD_READ_SECTORS)) {
        return false;
    }
    if (!wait_for_data()) {
        return false;
    }
    read_sector_words(out);
    return true;
}

bool write_one(uint32 lba, const uint8* in) {
    if (!start_transfer(lba, ATA_CMD_WRITE_SECTORS)) {
        return false;
    }
    if (!wait_for_data()) {
        return false;
    }
    write_sector_words(in);
    // The drive may report completion while the sector is still in its write
    // cache, so waiting for BSY to clear is not enough on its own; the flush in
    // ata_write is what makes "the command returned" mean "the bytes are on the
    // medium".
    return wait_not_busy();
}

// A software reset puts the channel into a known state, which matters because the
// firmware may have left a command half-issued.  Doing it here rather than
// assuming a clean handover is the difference between working on one machine and
// working on the next one.
bool reset_channel() {
    outb(ATA_CONTROL, static_cast<uint8>(ATA_CONTROL_RESET | ATA_CONTROL_NIEN));
    delay_400ns();
    outb(ATA_CONTROL, ATA_CONTROL_NIEN);
    delay_400ns();
    return wait_not_busy();
}

void trim_model(char* model) {
    // IDENTIFY returns the model as big-endian word pairs, so every pair has to
    // be swapped before the string reads correctly.
    char swapped[41];
    for (uint32 index = 0; index < 40; index += 2) {
        swapped[index] = model[index + 1];
        swapped[index + 1] = model[index];
    }
    swapped[40] = '\0';
    uint32 length = strlen(swapped);
    while (length > 0 && (swapped[length - 1] == ' ' || swapped[length - 1] == '\0')) {
        --length;
    }
    swapped[length] = '\0';
    for (uint32 index = 0; index <= length; ++index) {
        model[index] = swapped[index];
    }
}

bool identify(uint16* words) {
    outb(ATA_IO_BASE + REG_FEATURES, 0);
    outb(ATA_IO_BASE + REG_SECTOR_COUNT, 0);
    outb(ATA_IO_BASE + REG_LBA_LOW, 0);
    outb(ATA_IO_BASE + REG_LBA_MID, 0);
    outb(ATA_IO_BASE + REG_LBA_HIGH, 0);
    outb(ATA_IO_BASE + REG_COMMAND, ATA_CMD_IDENTIFY);
    delay_400ns();

    uint8 value = status();
    if (value == 0x00 || value == 0xFF) {
        return false;                       // nothing on the channel
    }
    if (!wait_not_busy()) {
        return false;
    }
    // ATAPI and SATA devices answer IDENTIFY with a signature in the LBA mid and
    // high registers; this driver only speaks plain ATA.
    if (inb(ATA_IO_BASE + REG_LBA_MID) != 0 || inb(ATA_IO_BASE + REG_LBA_HIGH) != 0) {
        return false;
    }
    value = status();
    if (value == 0x00 || value == 0xFF) {
        return false;
    }
    if ((value & ATA_STATUS_ERR) != 0) {
        return false;
    }
    if ((value & ATA_STATUS_DRQ) == 0 && !wait_for_data()) {
        return false;
    }
    for (uint32 index = 0; index < 256; ++index) {
        words[index] = inw(ATA_IO_BASE + REG_DATA);
    }
    return true;
}

}  // namespace

const AtaDevice* ata_probe() {
    if (g_probed) {
        return &g_device;
    }
    g_probed = true;

    outb(ATA_CONTROL, ATA_CONTROL_NIEN);     // polled, so no IRQ14
    select_drive(0xA0);                      // master, CHS for the reset
    const uint8 initial = status();
    if (initial == 0x00 || initial == 0xFF) {
        return &g_device;                    // an empty channel, which is normal
    }
    if (!reset_channel()) {
        return &g_device;
    }
    select_drive(0xA0);

    static uint16 words[256];
    if (!identify(words)) {
        return &g_device;
    }

    g_device.present = true;
    g_device.lba_supported = (words[49] & 0x0200) != 0;
    g_device.sectors = static_cast<uint32>(words[60]) |
                       (static_cast<uint32>(words[61]) << 16);
    // words 27..46 hold the model, one character per byte, in the order they
    // arrive -- which is byte-swapped, so trim_model fixes it in place.
    char raw[41];
    for (uint32 index = 0; index < 20; ++index) {
        raw[index * 2] = static_cast<char>(words[27 + index] & 0xFF);
        raw[index * 2 + 1] = static_cast<char>(words[27 + index] >> 8);
    }
    raw[40] = '\0';
    for (uint32 index = 0; index <= 40; ++index) {
        g_device.model[index] = raw[index];
    }
    trim_model(g_device.model);
    return &g_device;
}

bool ata_read(uint32 lba, uint32 count, void* buffer) {
    if (!ata_probe()->present) {
        return false;
    }
    uint8* out = static_cast<uint8*>(buffer);
    for (uint32 index = 0; index < count; ++index) {
        if (!read_one(lba + index, out + index * ATA_SECTOR_SIZE)) {
            return false;
        }
    }
    return true;
}

bool ata_write(uint32 lba, uint32 count, const void* buffer) {
    if (!ata_probe()->present) {
        return false;
    }
    const uint8* in = static_cast<const uint8*>(buffer);
    for (uint32 index = 0; index < count; ++index) {
        if (!write_one(lba + index, in + index * ATA_SECTOR_SIZE)) {
            return false;
        }
    }
    return ata_flush();
}

bool ata_flush() {
    if (!ata_probe()->present) {
        return false;
    }
    if (!wait_not_busy()) {
        return false;
    }
    select_drive(0xE0);
    outb(ATA_IO_BASE + REG_COMMAND, ATA_CMD_FLUSH_CACHE);
    delay_400ns();
    return wait_not_busy();
}

uint32 ata_timeouts() {
    return g_timeouts;
}

}  // namespace myos
