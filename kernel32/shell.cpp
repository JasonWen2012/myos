// myos 32-bit kernel: the command shell.
//
// The same shape as the 16-bit shell, deliberately: one line at a time, a table of
// names and handlers, and the first word of the line matched case-insensitively.
// What is different is where the characters come from -- the shared queue fed by
// IRQ1 and IRQ4, rather than polled BIOS calls -- and that every output line goes
// to the serial port as well as the screen, which is what makes a headless run
// assertable.

#include "shell.h"

#include "ata.h"
#include "bootinfo.h"
#include "console.h"
#include "cpu.h"
#include "file.h"
#include "fs.h"
#include "gdt.h"
#include "heap.h"
#include "idt.h"
#include "io.h"
#include "kernel.h"
#include "keyboard.h"
#include "klog.h"
#include "libc.h"
#include "mbr.h"
#include "mem.h"
#include "paging.h"
#include "pic.h"
#include "pit.h"
#include "pmm.h"
#include "sched.h"
#include "syscall.h"
#include "task.h"
#include "types.h"
#include "user.h"

namespace myos {
namespace {

constexpr uint32 LINE_LENGTH = 80;
constexpr uint32 MAX_ARGUMENTS = 8;

// The loader's isa-debug-exit values, matching DEBUG_EXIT_* in boot/boot.inc.
constexpr uint32 DEBUG_EXIT_PASS = 0x10;
constexpr uint32 DEBUG_EXIT_FAIL = 0x02;

char line[LINE_LENGTH];
char* arguments[MAX_ARGUMENTS];
uint32 argument_count = 0;

// ------------------------------------------------------------------ utilities

bool is_space(char c) {
    return c == ' ' || c == '\t';
}

char upper(char c) {
    return (c >= 'a' && c <= 'z') ? static_cast<char>(c - 'a' + 'A') : c;
}

bool equal_ignore_case(const char* a, const char* b) {
    while (*a != '\0' && *b != '\0') {
        if (upper(*a) != upper(*b)) {
            return false;
        }
        ++a;
        ++b;
    }
    return *a == *b;
}

// Splits the line in place: each word is NUL-terminated and recorded in
// `arguments`.  Doing it in place is what keeps the shell free of allocation.
void split_line() {
    argument_count = 0;
    char* p = line;
    while (*p != '\0' && argument_count < MAX_ARGUMENTS) {
        while (is_space(*p)) {
            ++p;
        }
        if (*p == '\0') {
            break;
        }
        arguments[argument_count++] = p;
        while (*p != '\0' && !is_space(*p)) {
            ++p;
        }
        if (*p != '\0') {
            *p++ = '\0';
        }
    }
}

// Decimal, unsigned, or -1 when the text is not a number.
int32 parse_dec(const char* text) {
    int32 value = 0;
    uint32 digits = 0;
    for (const char* p = text; *p != '\0'; ++p) {
        if (*p < '0' || *p > '9') {
            return -1;
        }
        value = value * 10 + (*p - '0');
        if (++digits > 6) {
            return -1;
        }
    }
    return digits == 0 ? -1 : value;
}

uint32 factorial(uint32 n) {
    return n <= 1 ? 1 : n * factorial(n - 1);
}

// ------------------------------------------------------------------- commands

void cmd_help();
void cmd_echo();
void cmd_clear();
void cmd_info();
void cmd_mem();
void cmd_ticks();
void cmd_fact();
void cmd_keylog();
void cmd_reboot();
void cmd_selftest();
void cmd_check();
void cmd_blk();
void cmd_vm();
void cmd_run();
void cmd_dmesg();
void cmd_fs();
void cmd_df();
void cmd_ls();
void cmd_cat();
void cmd_stat();
void cmd_write();
void cmd_mkdir();
void cmd_rm();
void cmd_sync();
void cmd_fsck();
void cmd_fstest();
void cmd_ps();
void cmd_schedtest();

struct Command {
    const char* name;
    void (*handler)();
    const char* summary;
};

// Keep this table and the text in cmd_help in step by hand, exactly as the 16-bit
// shell does: a table that generates its own help would need string handling the
// kernel does not have yet.  It does not: cmd_help walks this table.
const Command commands[] = {
    {"help", cmd_help, "this list"},
    {"echo", cmd_echo, "print the text back"},
    {"clear", cmd_clear, "blank the screen"},
    {"info", cmd_info, "kernel version, image, tables, ticks"},
    {"mem", cmd_mem, "the firmware's memory map"},
    {"vm", cmd_vm, "physical pages, page tables, heap (`vm fault` proves the panic path)"},
    {"run", cmd_run, "run a program from the volume in ring 3"},
    {"dmesg", cmd_dmesg, "the kernel's own log, oldest first"},
    {"ticks", cmd_ticks, "timer ticks since boot"},
    {"fact", cmd_fact, "factorial of 0-8, computed recursively"},
    {"keylog", cmd_keylog, "what the keyboard driver has seen"},
    {"blk", cmd_blk, "the ATA disk, its partitions, and `blk test`"},
    {"fs", cmd_fs, "the mounted filesystem"},
    {"df", cmd_df, "how much of the filesystem is free"},
    {"ls", cmd_ls, "list a directory (default /)"},
    {"cat", cmd_cat, "print a file"},
    {"write", cmd_write, "write text to a file, creating or replacing it"},
    {"mkdir", cmd_mkdir, "create a directory"},
    {"rm", cmd_rm, "remove a file, or an empty directory"},
    {"stat", cmd_stat, "size, inode and blocks of a path"},
    {"sync", cmd_sync, "flush and mark the volume cleanly unmounted"},
    {"fsck", cmd_fsck, "check the volume and reclaim what a lost write leaked"},
    {"fstest", cmd_fstest, "write, read, shrink and remove, then check it all back"},
    {"ps", cmd_ps, "the task table: pid, state, CPU ticks, page directory"},
    {"schedtest", cmd_schedtest, "create, preempt and reap tasks, then report"},
    {"reboot", cmd_reboot, "restart the machine"},
    {"check", cmd_check, "run the in-guest checks and stay in the shell"},
    {"selftest", cmd_selftest, "run the in-guest checks and exit with the result"},
};

void cmd_help() {
    console_puts("commands\n");
    for (const Command& command : commands) {
        console_puts("  ");
        console_puts(command.name);
        // Two spaces after the name, then pad to a fixed column so the list lines
        // up whether the name is four characters or eight.
        uint32 length = strlen(command.name);
        for (uint32 i = length; i < 10; ++i) {
            console_putc(' ');
        }
        console_puts(command.summary);
        console_putc('\n');
    }
}

void cmd_echo() {
    for (uint32 i = 1; i < argument_count; ++i) {
        if (i > 1) {
            console_putc(' ');
        }
        console_puts(arguments[i]);
    }
    console_putc('\n');
}

void cmd_clear() {
    console_clear();
}

void cmd_info() {
    console_puts("myos 0.1, 32-bit protected mode, flat segments\n");
    kprintf("kernel image %u bytes at %p, entry offset %x\n",
            image_header.size, KERNEL_LOAD_ADDRESS, image_header.entry);
    kprintf("gdt %p (%u bytes), idt %p (%u bytes)\n",
            gdt_address(), gdt_size(), idt_address(), idt_size());
    kprintf("pic remapped to vectors %u..%u, timer %u Hz\n",
            IRQ_BASE, IRQ_BASE + IRQ_COUNT - 1, pit_ticks_per_second());
    kprintf("boot drive %u, boot info at %p\n",
            boot_info()->boot_drive, BOOT_INFO_ADDRESS);
}

void cmd_mem() {
    mem_report();
}

void cmd_ticks() {
    const uint64 ticks = pit_ticks();
    kprintf("ticks %u (high %u) at %u Hz\n", static_cast<uint32>(ticks),
            static_cast<uint32>(ticks >> 32), pit_ticks_per_second());
}

void cmd_fact() {
    if (argument_count < 2) {
        console_puts("usage: fact <0-8>\n");
        return;
    }
    const int32 value = parse_dec(arguments[1]);
    if (value < 0) {
        console_puts("usage: fact <0-8>\n");
        return;
    }
    if (value > 8) {
        console_puts("fact: only 0-8 is supported (9! overflows 32 bits)\n");
        return;
    }
    console_put_dec(factorial(static_cast<uint32>(value)));
    console_putc('\n');
}

void cmd_keylog() {
    kprintf("keyboard: %u scan codes decoded, %u characters waiting\n",
            keyboard_scancode_count(), key_available() ? 1u : 0u);
    kprintf("serial: %u bytes received on IRQ4\n", serial_bytes_received());
    console_puts("input comes from IRQ1 (PS/2) and IRQ4 (COM1) into one queue\n");
}

void cmd_reboot() {
    if (fs_mounted()) {
        // Shutting down is the one moment a filesystem gets to say "all of this is
        // really on the disk", so it must not depend on the user remembering
        // `sync` first -- otherwise every ordinary reboot leaves a volume the next
        // boot has to warn about.
        if (fs_sync() == FS_OK) {
            console_puts("reboot: the volume is marked cleanly unmounted\n");
        } else {
            console_puts("reboot: WARNING: the volume could not be flushed\n");
        }
    }
    console_puts("rebooting...\n");
    // The 8042's reset line.  QEMU turns this into an exit (with -no-reboot),
    // which is how a test can prove the command ran.
    outb(0x64, 0xFE);
    for (;;) {
        asm volatile("hlt");
    }
}

// -------------------------------------------------------- disk and filesystem

// The pattern the device test writes: a function of the sector and the offset
// inside it, so a shifted or duplicated sector cannot pass by accident.
uint8 scratch_byte(uint32 sector, uint32 index) {
    return static_cast<uint8>((sector * 17 + index * 3 + 0x5A) & 0xFF);
}

// Every file command starts here, so "there is no filesystem" is said once, with
// the reason the mount gave, instead of each command inventing its own wording.
bool require_filesystem(const char* command) {
    if (fs_mounted()) {
        return true;
    }
    kprintf("%s: %s (%s)\n", command, fs_error_text(FS_NOT_MOUNTED),
            fs_mount_problem());
    return false;
}

// The disk's own test: write a pattern to the scratch tail of the device and read
// it back byte for byte.  This is the only thing in the kernel that writes to the
// disk, and it writes where nothing else lives -- so a failure here is a driver
// failure and cannot be confused with a filesystem one.
void disk_write_test() {
    const AtaDevice* device = ata_probe();
    if (!device->present) {
        console_puts("blk test: no device to test\n");
        return;
    }
    if (device->sectors < FS_SCRATCH_LBA + FS_SCRATCH_SECTORS) {
        kprintf("blk test: the device has %u sectors, too small for the scratch area "
                "at LBA %u\n", device->sectors, FS_SCRATCH_LBA);
        return;
    }
    uint8 buffer[ATA_SECTOR_SIZE];
    for (uint32 sector = 0; sector < FS_SCRATCH_SECTORS; ++sector) {
        for (uint32 index = 0; index < ATA_SECTOR_SIZE; ++index) {
            buffer[index] = scratch_byte(sector, index);
        }
        if (!ata_write(FS_SCRATCH_LBA + sector, 1, buffer)) {
            kprintf("blk test: writing LBA %u failed\n", FS_SCRATCH_LBA + sector);
            return;
        }
    }
    uint32 checked = 0;
    for (uint32 sector = 0; sector < FS_SCRATCH_SECTORS; ++sector) {
        if (!ata_read(FS_SCRATCH_LBA + sector, 1, buffer)) {
            kprintf("blk test: reading LBA %u back failed\n", FS_SCRATCH_LBA + sector);
            return;
        }
        for (uint32 index = 0; index < ATA_SECTOR_SIZE; ++index) {
            const uint8 expected = scratch_byte(sector, index);
            if (buffer[index] != expected) {
                kprintf("blk test: LBA %u byte %u reads %02x, expected %02x\n",
                        FS_SCRATCH_LBA + sector, index, buffer[index], expected);
                return;
            }
            ++checked;
        }
    }
    kprintf("blk test: wrote and read back %u sectors (%u bytes) at LBA %u\n",
            FS_SCRATCH_SECTORS, checked, FS_SCRATCH_LBA);
}

void cmd_blk() {
    if (argument_count > 1 && equal_ignore_case(arguments[1], "test")) {
        disk_write_test();
        return;
    }

    const AtaDevice* device = ata_probe();
    if (!device->present) {
        console_puts("blk: no ATA device on the primary channel (0x1F0)\n");
        return;
    }
    kprintf("blk: primary master, model '%s'\n", device->model);
    kprintf("blk: %u sectors of %u bytes (%u MiB), %s\n",
            device->sectors, ATA_SECTOR_SIZE, device->sectors / 2048,
            device->lba_supported ? "LBA28 supported" : "no LBA28: unusable here");
    Partition partition;
    if (mbr_find_partition(FS_PARTITION_TYPE, &partition)) {
        kprintf("blk: myfs partition at LBA %u, %u sectors\n",
                partition.lba_start, partition.sectors);
    } else {
        kprintf("blk: no myfs partition in the MBR (%s)\n", mbr_last_problem());
    }
    if (ata_timeouts() != 0) {
        kprintf("blk: %u command(s) timed out and were abandoned\n", ata_timeouts());
    }
}

void cmd_vm() {
    if (argument_count > 1 && equal_ignore_case(arguments[1], "fault")) {
        // Deliberately write to memory that is not mapped.  The point is not the
        // data, it is what the kernel does about it: a page fault that cannot be
        // satisfied has to end in a report with the address in it, not in a triple
        // fault that leaves nothing behind.
        console_puts("vm: writing to 0xdeadb000, which is not mapped...\n");
        volatile uint32* nowhere = reinterpret_cast<volatile uint32*>(0xDEADB000u);
        *nowhere = 1;
        console_puts("vm: the write did not fault, so the mapping is wrong\n");
        return;
    }
    pmm_report();
    paging_report();
    heap_report();
    user_report();
}

void cmd_dmesg() {
    klog_report();
}

void cmd_run() {
    if (!require_filesystem("run")) {
        return;
    }
    if (argument_count < 2) {
        console_puts("usage: run <path>\n");
        console_puts("       run /bin/hello\n");
        return;
    }
    // The kernel's shell is still the only shell; `run` hands control to a program in
    // ring 3 and waits for its exit syscall, which comes back through
    // return_to_kernel.  A program that faults instead of exiting leaves the kernel
    // in the panic path, which is the point of running it at all.
    UserRun result;
    const int32 status = exec_user(arguments[1], &result);
    if (status < 0) {
        kprintf("run: %s: %s\n", arguments[1], sys_error_text(status));
        return;
    }
    kprintf("run: %u page(s) of user address space, %u syscall(s)\n",
            result.pages, result.syscalls);
}

void cmd_fs() {
    fs_report_volume();
}

void cmd_df() {
    fs_report_usage();
}

void cmd_ls() {
    if (!require_filesystem("ls")) {
        return;
    }
    const char* path = argument_count > 1 ? arguments[1] : "/";
    uint32 index = 0;
    int32 status = fs_resolve(path, &index);
    if (status < 0) {
        kprintf("ls: %s: %s\n", path, fs_error_text(status));
        return;
    }
    Inode directory;
    status = fs_inode_read(index, &directory);
    if (status < 0) {
        kprintf("ls: %s: %s\n", path, fs_error_text(status));
        return;
    }
    if (directory.type != FS_TYPE_DIR) {
        kprintf("ls: %s: %s\n", path, fs_error_text(FS_NOT_DIR));
        return;
    }

    kprintf("ls %s:\n", path);
    uint32 shown = 0;
    const uint32 slots = fs_dir_slots(directory);
    for (uint32 slot = 0; slot < slots; ++slot) {
        Dirent entry;
        if (fs_dir_entry(directory, slot, &entry) < 0) {
            break;
        }
        if (entry.name[0] == '\0') {
            continue;                       // a slot left by a removed entry
        }
        Inode child;
        if (fs_inode_read(entry.inode, &child) < 0) {
            kprintf("  ?  %s (inode %u is unreadable)\n", entry.name, entry.inode);
            ++shown;
            continue;
        }
        if (child.type == FS_TYPE_DIR) {
            kprintf("  d  %s/  (%u entries)\n", entry.name,
                    fs_dir_live_entries(child));
        } else {
            kprintf("  f  %s  (%u bytes, inode %u)\n", entry.name, child.size,
                    entry.inode);
        }
        ++shown;
    }
    kprintf("%u entries\n", shown);
}

void cmd_cat() {
    if (!require_filesystem("cat")) {
        return;
    }
    if (argument_count < 2) {
        console_puts("usage: cat <path>\n");
        return;
    }
    const char* path = arguments[1];
    const int32 fd = file_open(path);
    if (fd < 0) {
        kprintf("cat: %s: %s\n", path, fs_error_text(fd));
        return;
    }
    const int32 size = file_size(fd);
    if (size < 0) {
        kprintf("cat: %s: %s\n", path, fs_error_text(size));
        file_close(fd);
        return;
    }
    // The size is printed before the contents, not after: the contents are the
    // file, and anything appended to them would make `cat` unable to reproduce a
    // file exactly.
    kprintf("cat: %s (%u bytes)\n", path, static_cast<uint32>(size));
    char buffer[FS_BLOCK_SIZE];
    uint32 total = 0;
    bool ends_with_newline = true;
    for (;;) {
        const int32 got = file_read(fd, buffer, sizeof(buffer));
        if (got < 0) {
            kprintf("cat: %s: %s\n", path, fs_error_text(got));
            break;
        }
        if (got == 0) {
            break;
        }
        for (int32 index2 = 0; index2 < got; ++index2) {
            console_putc(buffer[index2]);
        }
        ends_with_newline = buffer[got - 1] == '\n';
        total += static_cast<uint32>(got);
    }
    file_close(fd);
    if (!ends_with_newline) {
        console_putc('\n');
    }
    kprintf("cat: %u bytes read\n", total);
}

void cmd_stat() {
    if (!require_filesystem("stat")) {
        return;
    }
    if (argument_count < 2) {
        console_puts("usage: stat <path>\n");
        return;
    }
    const char* path = arguments[1];
    FsStat info;
    const int32 status = fs_stat(path, &info);
    if (status < 0) {
        kprintf("stat: %s: %s\n", path, fs_error_text(status));
        return;
    }
    if (info.type == FS_TYPE_DIR) {
        Inode directory;
        fs_inode_read(info.inode, &directory);
        kprintf("stat: %s is a directory, inode %u, %u entries, %u bytes of dirents\n",
                path, info.inode, fs_dir_live_entries(directory), info.size);
        return;
    }
    kprintf("stat: %s is a file, inode %u, %u bytes in %u block(s)%s\n",
            path, info.inode, info.size, info.blocks,
            info.indirect ? ", including the indirect block" : "");
}

void cmd_write() {
    if (!require_filesystem("write")) {
        return;
    }
    if (argument_count < 2) {
        console_puts("usage: write <path> <text>\n");
        console_puts("       write /note.txt hello there   (no text empties the file)\n");
        return;
    }
    const char* path = arguments[1];
    // The rest of the line is the text, spaces and all: a shell that can only
    // write one word at a time is not a shell anyone would use to keep notes.
    char text[LINE_LENGTH];
    uint32 length = 0;
    for (uint32 index = 2; index < argument_count; ++index) {
        if (index > 2 && length + 1 < sizeof(text)) {
            text[length++] = ' ';
        }
        for (const char* cursor = arguments[index];
             *cursor != '\0' && length + 1 < sizeof(text); ++cursor) {
            text[length++] = *cursor;
        }
    }
    text[length] = '\0';

    const int32 status = fs_write_file(path, text, length, true);
    if (status < 0) {
        kprintf("write: %s: %s\n", path, fs_error_text(status));
        return;
    }
    kprintf("write: %s, %u bytes, %u free blocks\n", path, length,
            fs_bitmap_free_blocks());
}

void cmd_mkdir() {
    if (!require_filesystem("mkdir")) {
        return;
    }
    if (argument_count < 2) {
        console_puts("usage: mkdir <path>\n");
        return;
    }
    const int32 status = fs_create(arguments[1], FS_TYPE_DIR);
    if (status < 0) {
        kprintf("mkdir: %s: %s\n", arguments[1], fs_error_text(status));
        return;
    }
    kprintf("mkdir: %s\n", arguments[1]);
}

void cmd_rm() {
    if (!require_filesystem("rm")) {
        return;
    }
    if (argument_count < 2) {
        console_puts("usage: rm <path>\n");
        return;
    }
    const int32 status = fs_unlink(arguments[1]);
    if (status < 0) {
        kprintf("rm: %s: %s\n", arguments[1], fs_error_text(status));
        return;
    }
    kprintf("rm: %s, %u free blocks\n", arguments[1], fs_bitmap_free_blocks());
}

void cmd_sync() {
    if (!require_filesystem("sync")) {
        return;
    }
    const int32 status = fs_sync();
    if (status < 0) {
        kprintf("sync: %s\n", fs_error_text(status));
        return;
    }
    console_puts("sync: nothing was pending -- writes go straight through -- and the "
                 "volume is now marked cleanly unmounted\n");
}

void cmd_fsck() {
    if (!require_filesystem("fsck")) {
        return;
    }
    FsckReport report;
    const int32 status = fs_fsck(&report);
    if (status < 0) {
        kprintf("fsck: %s\n", fs_error_text(status));
        return;
    }
    kprintf("fsck: %u block(s) reclaimed, %u block(s) rescued, %u orphan inode(s)\n",
            report.reclaimed_blocks, report.rescued_blocks, report.orphan_inodes);
    if (report.counts_wrong) {
        console_puts("fsck: the free counts did not match the bitmaps and were "
                     "recomputed\n");
    }
    if (report.orphan_inodes != 0) {
        console_puts("fsck: orphan inodes are reported, not removed: freeing one would "
                     "be guessing which file it was\n");
    }
    if (report.problems == 0) {
        console_puts("fsck: the volume is consistent\n");
    } else {
        kprintf("fsck: %u problem(s) found\n", report.problems);
    }
    kprintf("fsck: %u free blocks, %u free inodes, state clean\n",
            fs_bitmap_free_blocks(), fs_bitmap_free_inodes());
}

// The pattern the write test writes: a function of the offset, so a block written
// to the wrong place, duplicated, or left over from an earlier file cannot pass.
uint8 fstest_pattern_byte(uint32 index) {
    return static_cast<uint8>((index * 31 + 7 + (index >> 8)) & 0xFF);
}

// The write path's own acceptance test, in the guest, because the guest is the only
// place the driver, the allocator and the shell meet.  It leaves the volume exactly
// as it found it: every file it creates, it removes, and it checks the free counts
// came back before it says anything nice.
void cmd_fstest() {
    if (!require_filesystem("fstest")) {
        return;
    }
    uint32 checks = 0;
    uint32 failures = 0;
    auto check = [&checks, &failures](bool condition, const char* what) {
        ++checks;
        if (condition) {
            kprintf("  ok   %s\n", what);
        } else {
            ++failures;
            kprintf("  FAIL %s\n", what);
        }
    };

    const uint32 free_blocks_before = fs_bitmap_free_blocks();
    const uint32 free_inodes_before = fs_bitmap_free_inodes();
    kprintf("fstest: %u free blocks and %u free inodes to start with\n",
            free_blocks_before, free_inodes_before);

    // A file large enough to need the indirect block: 6000 bytes is twelve blocks,
    // so the twelfth lands in the table beyond the inode's own pointers.
    constexpr uint32 BIG = 6000;
    static uint8 big[BIG];
    for (uint32 index = 0; index < BIG; ++index) {
        big[index] = fstest_pattern_byte(index);
    }

    int32 status = fs_write_file("/fstest.bin", big, BIG, true);
    check(status == FS_OK, "a 6000-byte file is created and written");
    FsStat info;
    status = fs_stat("/fstest.bin", &info);
    check(status == FS_OK && info.size == BIG, "it reports the size it was given");
    check(info.indirect, "it grew past the direct blocks into the indirect one");

    static uint8 readback[BIG];
    const int32 fd = file_open("/fstest.bin");
    if (fd < 0) {
        check(false, "the file it just wrote can be opened");
    } else {
        uint32 total = 0;
        bool same = true;
        for (;;) {
            const int32 got = file_read(fd, readback + total, BIG - total);
            if (got <= 0) {
                break;
            }
            total += static_cast<uint32>(got);
        }
        for (uint32 index = 0; index < BIG && total == BIG; ++index) {
            if (readback[index] != big[index]) {
                same = false;
                break;
            }
        }
        check(total == BIG && same, "every byte reads back the same");
        file_close(fd);
    }

    // A shorter overwrite has to let the blocks it no longer needs go, and the read
    // afterwards has to see the new end rather than the old data behind it.
    status = fs_write_file("/fstest.bin", big, 100, true);
    const int32 shrunk = file_open("/fstest.bin");
    check(status == FS_OK && shrunk >= 0, "it can be overwritten with something shorter");
    if (shrunk >= 0) {
        uint32 total = 0;
        for (;;) {
            static uint8 scratch[128];
            const int32 got = file_read(shrunk, scratch, sizeof(scratch));
            if (got <= 0) {
                break;
            }
            total += static_cast<uint32>(got);
        }
        file_close(shrunk);
        check(total == 100, "the file ends where the shorter write ended");
    }
    check(fs_stat("/fstest.bin", &info) == FS_OK && !info.indirect,
          "the indirect block was released with the data it described");

    check(fs_create("/fstest.dir", FS_TYPE_DIR) == FS_OK, "a directory can be created");
    check(fs_write_file("/fstest.dir/inner.txt", "inside", 6, true) == FS_OK,
          "a file can be created inside it");
    {
        uint32 dir_index = 0;
        Inode dir_inode;
        check(fs_resolve("/fstest.dir", &dir_index) == FS_OK &&
                  fs_inode_read(dir_index, &dir_inode) == FS_OK &&
                  fs_dir_live_entries(dir_inode) == 1,
              "the directory lists exactly one entry");
    }

    uint32 index = 0;
    check(fs_resolve("/fstest.missing", &index) == FS_NO_ENT,
          "a path that does not exist is refused");
    check(fs_create("/fstest.dir", FS_TYPE_DIR) == FS_EXISTS,
          "creating the same directory twice is refused");
    check(fs_create("/0123456789012345678901234567890", FS_TYPE_FILE)
              == FS_NAME_TOO_LONG,
          "a name longer than the format allows is refused");
    check(fs_unlink("/fstest.dir") == FS_DIR_NOT_EMPTY,
          "removing a directory that still has files is refused");
    check(fs_unlink("/fstest.missing") == FS_NO_ENT,
          "removing something that is not there is refused");

    check(fs_unlink("/fstest.dir/inner.txt") == FS_OK, "the inner file can be removed");
    check(fs_unlink("/fstest.dir") == FS_OK, "the empty directory can be removed");
    check(fs_unlink("/fstest.bin") == FS_OK, "the big file can be removed");

    const uint32 free_blocks_after = fs_bitmap_free_blocks();
    const uint32 free_inodes_after = fs_bitmap_free_inodes();
    check(free_blocks_before == free_blocks_after,
          "every data block the test used came back");
    check(free_inodes_before == free_inodes_after,
          "every inode the test used came back");

    // The files the build packed are the thing a bad write would damage first, so
    // they are checked again at the end rather than assumed.
    ManifestReport manifest;
    const int32 manifest_status = fs_verify_manifest(&manifest);
    check(manifest_status == FS_OK && manifest.mismatched == 0 &&
              manifest.checked == manifest.lines,
          "the files the build packed are still exactly as they were");

    // The write test restored everything it touched, so leaving the volume marked
    // dirty would tell the next boot that something was lost when nothing was.
    const int32 synced = fs_sync();
    check(synced == FS_OK, "the volume is marked cleanly unmounted again");

    kprintf("fstest: %u ok, %u failed; %u free blocks, %u free inodes\n",
            checks - failures, failures, fs_bitmap_free_blocks(),
            fs_bitmap_free_inodes());
}

// The checks without the exit.  `selftest` is for a headless run that wants a
// verdict and is willing to end for it; `check` is for a person at the keyboard who
// wants the same list and to keep their session -- and for a test that needs to run
// the checks more than once to prove they can be run more than once.
void cmd_check() {
    static uint32 run = 0;
    ++run;
    console_puts("check: running the in-guest checks, the session continues\n");
    const uint32 failed = kernel_self_test();
    const uint32 skipped = kernel_checks_skipped();
    kprintf("check: run %u: %u ok, %u skipped, %u failed\n", run,
            kernel_checks_run() - failed, skipped, failed);
    if (failed == 0) {
        console_puts("check: everything the kernel can test about itself passed\n");
    }
}

void cmd_ps() {
    task_report();
    sched_report();
}

// The scheduler checks on demand, for the same reason `check` exists: a session that
// is already up can ask again, and a test can watch tasks being created, preempted and
// reaped without booting for it.  The demo's own output (`sched: alpha round 1 ...`)
// is deliberately not repeated here -- it belongs to the checks.
void cmd_schedtest() {
    console_puts("schedtest: creating two tasks and letting the timer preempt them\n");
    const uint32 failed = kernel_scheduler_test();
    kprintf("schedtest: %u ok, %u skipped, %u failed\n",
            kernel_checks_run() - failed, kernel_checks_skipped(), failed);
    if (failed == 0) {
        console_puts("schedtest: every task ran, exited, and gave its stack back\n");
    }
}

void cmd_selftest() {
    const uint32 failed = kernel_self_test();
    if (failed == 0) {
        console_puts("\nSELFTEST PASS\n");
        debug_exit(DEBUG_EXIT_PASS);
    }
    console_puts("\nSELFTEST FAIL\n");
    debug_exit(DEBUG_EXIT_FAIL);
    // On real hardware the debug port is inert, so report what is left to do
    // instead of falling into whatever follows.
    console_puts("(no debug port here: fix the failing checks above)\n");
}

}  // namespace

void shell_run() {
    // The shell waits with `hlt`, which only wakes on an interrupt, so the machine
    // has to be interrupt-driven before the first prompt.  The interrupt system is
    // already up by now; this makes the dependency explicit rather than a fact
    // inherited from the self-test that ran before it.
    interrupts_enable();

    console_puts("Type `help` for a list of commands.\n\n");
    for (;;) {
        console_set_color(attr(COLOR_LIGHT_GREEN, COLOR_BLACK));
        console_puts("myos> ");
        console_set_color(attr(COLOR_LIGHT_GREY, COLOR_BLACK));

        uint32 length = 0;
        for (;;) {
            const char c = key_get();
            if (c == '\r' || c == '\n') {
                line[length] = '\0';
                console_putc('\n');
                break;
            }
            if (c == '\b' || c == 0x7F) {       // backspace, from a key or a terminal
                if (length > 0) {
                    --length;
                    console_putc('\b');
                }
                continue;
            }
            if (c < 32 || c > 126) {
                continue;                       // control characters, DEL and above
            }
            if (length + 1 >= LINE_LENGTH) {
                continue;                       // full: refuse rather than overflow
            }
            line[length++] = c;
            console_putc(c);                    // echo, since neither source echoes
        }

        split_line();
        if (argument_count == 0) {
            continue;
        }
        const Command* match = nullptr;
        for (const Command& command : commands) {
            if (equal_ignore_case(arguments[0], command.name)) {
                match = &command;
                break;
            }
        }
        if (match != nullptr) {
            match->handler();
        } else {
            // Say so, rather than printing a prompt and looking like the line was
            // never read.
            console_puts("unknown command: ");
            console_puts(arguments[0]);
            console_puts("  (try `help`)\n");
        }
    }
}

}  // namespace myos
