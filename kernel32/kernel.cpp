// myos 32-bit kernel: entry point and in-guest self-test.
//
// kernel32/boot.asm holds the entry point the loader jumps to; it switches to the
// kernel's own stack and then calls kmain with flat segments in place.  Everything
// from there on is C++.

#include "bootinfo.h"
#include "ata.h"
#include "console.h"
#include "cpu.h"
#include "fs.h"
#include "gdt.h"
#include "idt.h"
#include "io.h"
#include "kernel.h"
#include "keyboard.h"
#include "libc.h"
#include "mbr.h"
#include "pic.h"
#include "pit.h"
#include "shell.h"
#include "types.h"

extern "C" {
void debug_exit(myos::uint32 value);
}

namespace myos {
namespace {

// The loader's isa-debug-exit values, matching DEBUG_EXIT_* in boot/boot.inc.
constexpr uint32 DEBUG_EXIT_PASS = 0x10;
constexpr uint32 DEBUG_EXIT_FAIL = 0x02;

uint32 checks_run = 0;
uint32 checks_failed = 0;
uint32 checks_skipped = 0;

void check(bool condition, const char* what) {
    ++checks_run;
    if (condition) {
        kprintf("  ok   %s\n", what);
    } else {
        ++checks_failed;
        kprintf("  FAIL %s\n", what);
    }
}

// A check that needs hardware this machine may not have is skipped, not failed:
// booting from a floppy with no disk attached is a supported configuration, and a
// self-test that fails there would be lying about the kernel.  Skips are counted
// and printed, so they cannot quietly become the normal case either.
void skip(const char* what, const char* why) {
    ++checks_skipped;
    kprintf("  skip %s (%s)\n", what, why);
}

// One line per claim, whether or not this machine can evaluate it.  The summary
// then says how much of the kernel was actually exercised, instead of a smaller
// number of checks looking like an equally thorough run.
void check_or_skip(bool evaluated, bool condition, const char* what, const char* why) {
    if (evaluated) {
        check(condition, what);
    } else {
        skip(what, why);
    }
}

// Memory above 1 MiB only works with the A20 gate open, and the kernel runs there,
// so this is the check that the loader's A20 work actually took effect.
bool memory_above_1m_is_usable() {
    volatile uint32* high = reinterpret_cast<volatile uint32*>(0x00110000);
    volatile uint32* low = reinterpret_cast<volatile uint32*>(0x00070000);
    uint32 saved_high = *high;
    uint32 saved_low = *low;
    *high = 0xA5A5A5A5;
    *low = 0x5A5A5A5A;
    bool distinct = (*high == 0xA5A5A5A5) && (*low == 0x5A5A5A5A);
    *high = saved_high;
    *low = saved_low;
    return distinct;
}

// The whole interrupt path in one check: the PIC must be remapped and unmasked,
// the IDT must hold the timer stub, the stub must reach the dispatcher, the
// handler must run and the end-of-interrupt must be sent -- otherwise the counter
// either never moves or moves exactly once.
bool timer_interrupts_arrive() {
    const uint64 before = pit_ticks();
    interrupts_enable();
    // Bounded: without interrupts this loop would otherwise spin forever and the
    // self-test would hang instead of reporting the failure.  A 100 Hz timer means
    // three ticks is tens of milliseconds, so the bound is generous by orders of
    // magnitude on any real machine and on QEMU alike.
    for (volatile uint32 spin = 0; spin < 200000000u; ++spin) {
        if (pit_ticks() >= before + 3) {
            return true;
        }
    }
    return false;
}

// The disk and filesystem checks.  They are read-only on purpose: the self-test
// runs on every boot, and a boot that writes to the volume would be a boot that
// can corrupt it.  Anything that creates, grows or removes a file belongs to the
// `fstest` command, where a test asks for it explicitly.
void run_filesystem_checks() {
    const AtaDevice* device = ata_probe();
    const bool have_device = device->present;
    const char* const no_device = "no ATA device on the primary channel";
    if (!have_device) {
        // Two ways to have no disk, and they are worth telling apart: a channel
        // with nothing on it, and a device that answered but would not identify.
        kprintf("  (blk: %s)\n", no_device);
    }
    check_or_skip(have_device,
                  have_device && device->sectors > 0 && device->model[0] != '\0',
                  "the ATA disk identifies itself", no_device);

    Partition partition;
    const bool found = have_device && mbr_find_partition(FS_PARTITION_TYPE, &partition);
    if (have_device && !found) {
        kprintf("  (mbr: %s)\n", mbr_last_problem());
    }
    check_or_skip(have_device, found, "the MBR declares a myfs partition",
                  have_device ? mbr_last_problem() : no_device);
    const char* const no_partition = have_device ? "the partition is not usable"
                                                 : no_device;
    check_or_skip(found, found && partition.sectors == FS_BLOCK_COUNT,
                  "the myfs partition is the size this kernel expects", no_partition);

    const int32 mounted = found ? fs_mount() : FS_NOT_MOUNTED;
    const bool is_mounted = found && mounted == FS_OK;
    const char* const no_volume = found ? fs_mount_problem() : no_partition;
    if (found && !is_mounted) {
        kprintf("  (mount: %s)\n", no_volume);
    }
    check_or_skip(found, is_mounted, "the myfs volume mounts", no_volume);

    check_or_skip(is_mounted, is_mounted && !fs_counts_disagree(),
                  "the volume's free counts agree with its bitmaps", no_volume);

    ManifestReport report;
    int32 manifest_status = FS_NOT_MOUNTED;
    if (is_mounted) {
        manifest_status = fs_verify_manifest(&report);
    }
    const bool manifest_ok = is_mounted && manifest_status == FS_OK;
    check_or_skip(is_mounted, manifest_ok, "the volume has a readable manifest",
                  no_volume);
    if (manifest_ok) {
        kprintf("  (manifest: %u lines, %u verified, %u mismatched)\n",
                report.lines, report.checked, report.mismatched);
        if (report.mismatched != 0) {
            kprintf("  (first problem: %s)\n", report.first_problem);
        }
    } else if (is_mounted) {
        kprintf("  (manifest: %s)\n", fs_error_text(manifest_status));
    }
    const char* const no_manifest = manifest_ok ? "" : "no readable manifest";
    check_or_skip(manifest_ok, manifest_ok && report.lines >= 3,
                  "the manifest lists the files the build packed", no_manifest);
    check_or_skip(manifest_ok,
                  manifest_ok && report.mismatched == 0 && report.checked == report.lines,
                  "every packed file matches its manifest checksum", no_manifest);

    // Error paths, which are the other half of "it works": a filesystem that
    // cannot say no is a filesystem that will happily return the wrong file.
    uint32 index = 0;
    const int32 missing = is_mounted ? fs_resolve("/no-such-file", &index) : FS_NOT_MOUNTED;
    check_or_skip(is_mounted, missing == FS_NO_ENT,
                  "a path that does not exist is refused", no_volume);
    const int32 dotdot = is_mounted ? fs_resolve("/docs/../..", &index) : FS_NOT_MOUNTED;
    check_or_skip(is_mounted, dotdot == FS_UNSUPPORTED,
                  "'..' is refused rather than guessed", no_volume);

    uint32 hello = 0;
    const bool has_hello = is_mounted && fs_resolve("/hello.txt", &hello) == FS_OK;
    Inode inode;
    const bool has_inode = has_hello && fs_inode_read(hello, &inode) == FS_OK;
    const char* const no_file = has_hello ? "the inode is unreadable"
                                          : "/hello.txt is missing";
    int32 whole = 0;
    int32 past = 0;
    if (has_inode) {
        char scratch[64];
        whole = fs_read_data(inode, 0, scratch, sizeof(scratch));
        past = fs_read_data(inode, inode.size + 100, scratch, sizeof(scratch));
    }
    check_or_skip(has_inode,
                  has_inode && whole > 0 && static_cast<uint32>(whole) <= inode.size,
                  "a file's bytes read back", is_mounted ? no_file : no_volume);
    check_or_skip(has_inode, has_inode && past == 0,
                  "reading past the end of a file returns nothing", no_file);
    check_or_skip(has_inode, has_inode && inode.size > 0 &&
                                inode.size <= FS_MAX_FILE_SIZE,
                  "the file's size is inside the format's limits", no_file);
}

// The checksum the packer uses for the manifest, applied to the whole staged image.
// It exists so the host can ask a much stronger question than "did the kernel
// start": any single byte the loader read wrongly changes this number, and the test
// has the bytes that were supposed to be read.
uint32 image_checksum(const uint8* data, uint32 size) {
    uint32 value = 0;
    for (uint32 index = 0; index < size; ++index) {
        value = value * 31u + data[index];
    }
    return value;
}

void run_self_test() {
    const BootInfo* info = boot_info();

    kprintf("self-test\n");
    check(image_header.magic == IMAGE_MAGIC, "the kernel image header is readable");
    check(image_header.arch == 2, "the header says this is the 32-bit kernel");
    check(image_header.size > 0 && image_header.entry < image_header.size,
          "the header's entry offset is inside the image");
    // memcmp, not strcmp: the magic is four bytes with no terminator, so strcmp
    // walks straight past it into the version byte and reports every good block
    // as bad.
    check(memcmp(info->magic, "MYBI", 4) == 0, "the loader left a boot info block");

    // The loader stages the image below 1 MiB and copies it up, so the kernel can
    // still see both copies.  The loaded copy's .data is the kernel's own mutable
    // state by the time this runs, so a whole-image compare against it would be
    // comparing a running kernel with a pristine copy -- the first version of this
    // check did exactly that and failed on a byte the console had legitimately
    // changed.  Instead: a bounded byte compare proves the copy step, and the
    // checksum of the *staged* copy is printed for the host, which holds the image
    // that was supposed to be read and can check every byte of it.
    {
        const uint8* staged = reinterpret_cast<const uint8*>(KERNEL32_STAGE_LIN);
        const uint8* loaded = reinterpret_cast<const uint8*>(KERNEL_LOAD_ADDRESS);
        const uint32 limit = IMAGE_HEADER_SIZE + 256;
        uint32 mismatch = 0;
        while (mismatch < limit && staged[mismatch] == loaded[mismatch]) {
            ++mismatch;
        }
        if (mismatch < limit) {
            kprintf("  copy differs at +%x: staged %x, loaded %x\n",
                    mismatch, staged[mismatch], loaded[mismatch]);
        }
        check(mismatch == limit, "the loader copied the image's first bytes faithfully");
        kprintf("  (staged image: %u bytes, checksum %08x)\n", image_header.size,
                image_checksum(staged, image_header.size));
    }

    check(memory_above_1m_is_usable(), "memory above 1 MiB is distinct from low memory");

    // The tables the kernel loaded itself, read back from the CPU rather than from
    // the variables that were written: "lgdt was called" and "lgdt worked" are
    // different claims.
    {
        GdtPointer loaded{};
        gdt_read_loaded(&loaded);
        check(loaded.base == gdt_address() && loaded.limit + 1 == gdt_size(),
              "the CPU is using the kernel's own GDT");
    }
    {
        IdtPointer loaded{};
        idt_read_loaded(&loaded);
        check(loaded.base == idt_address() && loaded.limit + 1 == idt_size(),
              "the CPU is using the kernel's own IDT");
    }

    // The keyboard decoder, with the scan codes for "hi" pressed by hand: the IRQ
    // path is what delivers them in practice, but the table is where a wrong entry
    // shows up, and a key that produces the wrong letter is a data bug rather than
    // a timing one.  The last check also releases shift, which the second one holds
    // down -- otherwise every later keystroke would be capitalised.
    check(keyboard_translate(0x23) == 'h' && keyboard_translate(0x17) == 'i',
          "the keyboard decoder translates scan codes");
    check(keyboard_translate(0x2A) == 0 && keyboard_translate(0x23) == 'H',
          "shift is tracked across scan codes");
    check(keyboard_translate(0xAA) == 0 && keyboard_translate(0x23) == 'h',
          "key releases are not characters, and shift is released");

    check(timer_interrupts_arrive(), "the timer interrupt arrives and is counted");

    // The string helpers, which the console depends on, checked against values a
    // person can read rather than against themselves.
    char buffer[16];
    utoa(3040, buffer);
    check(strcmp(buffer, "3040") == 0, "utoa writes decimal");
    utoa_base(0xDEAD, buffer, 16);
    check(strcmp(buffer, "dead") == 0, "utoa_base writes hex");
    char overlap[8] = {'a', 'b', 'c', 'd', 'e', 'f', 'g', '\0'};
    memmove(overlap + 1, overlap, 6);
    check(overlap[1] == 'a' && overlap[6] == 'f', "memmove handles overlap");

    run_filesystem_checks();

    kprintf("%u ok, %u skipped, %u failed\n", checks_run - checks_failed,
            checks_skipped, checks_failed);
}

}  // namespace

uint32 kernel_self_test() {
    checks_run = 0;
    checks_failed = 0;
    checks_skipped = 0;
    run_self_test();
    return checks_failed;
}

uint32 kernel_checks_run() {
    return checks_run;
}

}  // namespace myos

extern "C" void kmain() {
    using namespace myos;

    serial_init();
    console_init();
    // Ownership of the CPU, in the order the hardware needs it: a GDT before any
    // segment register can be reloaded, an IDT before any interrupt can be
    // delivered, the PIC before it can be unmasked, and the devices last.
    gdt_init();
    idt_init();
    pic_init();
    pit_init();
    keyboard_init();

    const BootInfo* info = boot_info();

    console_set_color(attr(COLOR_LIGHT_GREEN, COLOR_BLACK));
    console_puts("myos 32-bit kernel\n");
    console_set_color(attr(COLOR_LIGHT_GREY, COLOR_BLACK));
    console_puts("version 0.1 -- protected mode, flat segments\n");
    kprintf("image %u bytes, entry %p, boot drive %u\n",
            image_header.size, KERNEL_LOAD_ADDRESS + image_header.entry,
            info->boot_drive);
    kprintf("firmware memory map: %u entries of %u bytes\n",
            info->memory_map_count, info->memory_map_entry_size);
    kprintf("a20 is open: memory above 1 MiB is addressable\n");

    // The disk and its filesystem, brought up before the self-test so the checks
    // examine a mounted volume, and before interrupts are enabled so the polling
    // driver runs without a timer tick landing in the middle of a transfer.
    const AtaDevice* device = ata_probe();
    if (device->present) {
        kprintf("ata: '%s', %u sectors of %u bytes (%u MiB)\n", device->model,
                device->sectors, ATA_SECTOR_SIZE, device->sectors / 2048);
    } else {
        console_puts("ata: no device on the primary channel\n");
    }
    const int32 mounted = fs_mount();
    if (mounted == FS_OK) {
        kprintf("fs: myfs volume mounted from LBA %u, %u free blocks\n",
                fs_partition_lba(), fs_bitmap_free_blocks());
    } else {
        kprintf("fs: not mounted: %s (%s)\n", fs_error_text(mounted),
                fs_mount_problem());
    }
    console_putc('\n');

    console_set_color(attr(COLOR_YELLOW, COLOR_BLACK));
    kernel_self_test();
    console_set_color(attr(COLOR_LIGHT_GREY, COLOR_BLACK));
    console_puts("\n");

    // The shell never returns.  `selftest` is what ends a headless run, through
    // the debug port, so that a test gets a verdict rather than a timeout.
    shell_run();
    hlt_forever();
}
