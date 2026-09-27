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
#include "heap.h"
#include "idt.h"
#include "io.h"
#include "kernel.h"
#include "keyboard.h"
#include "klog.h"
#include "libc.h"
#include "mbr.h"
#include "panic.h"
#include "pic.h"
#include "pit.h"
#include "paging.h"
#include "pmm.h"
#include "sched.h"
#include "shell.h"
#include "task.h"
#include "tss.h"
#include "types.h"
#include "user.h"
#include "usercopy.h"

extern "C" {
void debug_exit(myos::uint32 value);
// boot.asm: the top of the kernel's own stack, which the TSS needs before any user
// program can make a syscall.
extern myos::uint32 kernel_stack_top;
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

// The memory-layer checks.  Every one of them is a claim the rest of the kernel
// depends on: the allocator hands out pages it owns, paging is really on and
// identity-mapping what the kernel already uses, the heap gives memory back, and a
// page fault can be survived.
void run_memory_checks() {
    const PmmStats before = pmm_stats();
    check(before.managed_pages > 0 && before.free_pages > 0,
          "the physical allocator found usable memory");

    // Sixty-four pages, checked as a set: distinct, aligned, and none of them from
    // the low megabyte or from the kernel image itself.
    constexpr uint32 COUNT = 64;
    uint32 pages[COUNT] = {0};
    bool distinct = true;
    bool aligned = true;
    bool clear_of_used = true;
    for (uint32 index = 0; index < COUNT; ++index) {
        pages[index] = pmm_alloc_page();
        if (pages[index] == 0) {
            distinct = false;
            break;
        }
        if (pages[index] % PAGE_SIZE != 0) {
            aligned = false;
        }
        if (pages[index] < PMM_MIN_ADDRESS ||
            (pages[index] >= KERNEL_LOAD_ADDRESS &&
             pages[index] < KERNEL_LOAD_ADDRESS + image_header.size)) {
            clear_of_used = false;
        }
        for (uint32 other = 0; other < index; ++other) {
            if (pages[index] == pages[other]) {
                distinct = false;
            }
        }
    }
    for (uint32 index = 0; index < COUNT; ++index) {
        if (pages[index] != 0) {
            pmm_free_page(pages[index]);
        }
    }
    check(distinct, "pages come back distinct");
    check(aligned, "pages come back page-aligned");
    check(clear_of_used,
          "no page is handed out from the low megabyte or the kernel image");
    check(pmm_stats().free_pages == before.free_pages,
          "freeing every page puts the allocator back where it started");

    check(page_translate(KERNEL_LOAD_ADDRESS) == KERNEL_LOAD_ADDRESS &&
              page_translate(0xB8000) == 0xB8000,
          "the kernel image and the video buffer are identity mapped");
    check(page_directory_physical() != 0 &&
              read_cr3() == page_directory_physical(),
          "the CPU is using the page directory the kernel built");

    // A fresh virtual address, mapped to a fresh physical page: the write has to go
    // through the new mapping and not somewhere else.
    const uint32 physical = pmm_alloc_page();
    const uint32 elsewhere = 0xC0000000u;
    bool mapped = physical != 0 &&
                  page_map(elsewhere, physical, PAGE_PRESENT | PAGE_WRITE) &&
                  page_translate(elsewhere) == physical;
    if (mapped) {
        volatile uint32* slot = reinterpret_cast<volatile uint32*>(elsewhere);
        *slot = 0xC0FFEE;
        mapped = (*slot == 0xC0FFEE);
    }
    const bool unmapped = page_unmap(elsewhere) &&
                          page_translate(elsewhere) == 0;
    if (physical != 0) {
        pmm_free_page(physical);
    }
    check(mapped, "a new virtual address can be mapped and written through");
    check(unmapped, "unmapping it takes the address away again");

    // The heap: two blocks written to the brim must not disturb each other, and
    // everything must come back.
    const HeapStats heap_before = heap_stats();
    uint8* first = static_cast<uint8*>(kmalloc(100));
    uint8* second = static_cast<uint8*>(kmalloc(100));
    uint8* big = static_cast<uint8*>(kmalloc(4096));
    bool heap_ok = first != nullptr && second != nullptr && big != nullptr;
    if (heap_ok) {
        for (uint32 index = 0; index < 100; ++index) {
            first[index] = static_cast<uint8>(index);
            second[index] = static_cast<uint8>(index ^ 0xFF);
        }
        for (uint32 index = 0; index < 100; ++index) {
            heap_ok = heap_ok && first[index] == static_cast<uint8>(index);
            heap_ok = heap_ok && second[index] == static_cast<uint8>(index ^ 0xFF);
        }
        heap_ok = heap_ok && heap_stats().bad_frees == heap_before.bad_frees;
    }
    kfree(big);
    kfree(second);
    kfree(first);
    const HeapStats heap_after = heap_stats();
    check(heap_ok, "kmalloc hands out blocks that do not disturb each other");
    check(heap_after.used_bytes == 0 &&
              heap_after.bad_frees == heap_before.bad_frees,
          "kfree gives every byte back and refuses nothing it was given");
    check(kmalloc(HEAP_ARENA_BYTES * 2) == nullptr,
          "a request bigger than the whole arena is refused, not truncated");

    // The one page fault the kernel can recover from: nothing is mapped until the
    // first touch, and the page handed over has to read as zero.  The page is given
    // back at the end so that running the self-test twice is the same as running it
    // once -- `selftest` is a command, not a boot-time ritual.
    const uint32 lazy = 0xD0000000u;
    const uint32 faults_before = paging_faults();
    const bool reserved = paging_reserve_lazy(lazy, PAGE_SIZE * 2);
    bool read_zero = false;
    bool recovered = false;
    if (reserved) {
        volatile uint32* first_word = reinterpret_cast<volatile uint32*>(lazy);
        read_zero = (*first_word == 0);
        const uint32 mapped_physical = page_translate(lazy);
        recovered = paging_faults() == faults_before + 1 && mapped_physical != 0;
        if (mapped_physical != 0) {
            page_unmap(lazy);
            pmm_free_page(mapped_physical);
        }
    }
    check(reserved && recovered,
          "an untouched demand-zero page faults, is mapped, and the read retries");
    check(reserved && read_zero, "the page handed over on that fault reads as zero");
}

// The privilege-level checks.  They are read back from the tables the kernel built
// and from the CPU's own registers, because "we called the function with the right
// arguments" and "ring 3 can actually use this" are different claims -- and the
// failure mode of the second one is a general protection fault inside a user program,
// which is a long way from the line that got it wrong.
void run_privilege_checks() {
    // The CPU sets the *accessed* bit (bit 0) of a descriptor the first time it loads
    // the segment, so the type and privilege bits have to be compared with that bit
    // masked off.  Without the mask these checks pass at boot and fail the second time
    // the checks are run, which is a false alarm caused by the CPU doing its job.
    constexpr uint8 ACCESS_MASK = 0xFC;
    check((gdt_entry_access(3) & ACCESS_MASK) == (0xFA & ACCESS_MASK) &&
              (gdt_entry_access(4) & ACCESS_MASK) == (0xF2 & ACCESS_MASK),
          "the GDT has ring-3 code and data descriptors");
    check((gdt_entry_access(1) & ACCESS_MASK) == (0x9A & ACCESS_MASK) &&
              (gdt_entry_access(2) & ACCESS_MASK) == (0x92 & ACCESS_MASK),
          "the ring-0 descriptors are unchanged");
    check(tss_loaded_selector() == tss_selector() && tss_address() != 0,
          "the CPU has loaded the kernel's TSS");
    check(tss_kernel_stack() == reinterpret_cast<uint32>(&kernel_stack_top) &&
              tss_kernel_stack() >= KERNEL32_STAGE_LIN,
          "the TSS names a kernel stack for ring 3 traps to land on");
    check(idt_gate_present(0x80) && idt_gate_dpl(0x80) == 3,
          "int 0x80 is a present gate at privilege level 3");
    check(idt_gate_dpl(0x0E) == 0 && idt_gate_dpl(0x20) == 0,
          "every other gate stays at privilege level 0");

    // The user-copy checks, exercised the way a syscall would: one page mapped for
    // user mode, one page that belongs to the kernel, and the same request against
    // both.
    const uint32 physical = pmm_alloc_page();
    const uint32 user_page = USER_BASE + 2 * 0x00100000u;     // inside the window
    bool user_mapped = physical != 0 &&
                       page_map(user_page, physical,
                                PAGE_PRESENT | PAGE_WRITE | PAGE_USER);
    char scratch[16];
    bool copied = false;
    if (user_mapped) {
        const char* text = "user data";
        copied = copy_to_user(user_page, text, 10) == 10 &&
                 copy_from_user(scratch, user_page, 10) == 10 &&
                 memcmp(scratch, text, 10) == 0;
    }
    check(user_mapped && copied, "a user page can be copied to and from");
    check(copy_from_user(scratch, KERNEL_LOAD_ADDRESS, 8) == E_FAULT,
          "reading through a kernel address on behalf of a user is refused");
    check(copy_to_user(KERNEL_LOAD_ADDRESS, scratch, 8) == E_FAULT,
          "writing to a kernel address on behalf of a user is refused");
    check(!user_range_ok(0x1000, 8) && !user_range_ok(USER_BASE - PAGE_SIZE, 8) &&
              !user_range_ok(user_page, 0x20000000u),
          "addresses outside the user window are refused whatever their length");
    check(user_range_ok(user_page, 0) == true,
          "an empty range inside the user window is allowed");
    // The page flags are what actually stops ring 3; the window is just a range test.
    // Both of these are checked unconditionally, with the flags read as zero when the
    // mapping failed, so the number of checks does not depend on whether the machine
    // got that far.
    const uint32 user_flags = user_mapped ? page_flags(user_page) : 0;
    check((user_flags & PAGE_USER) != 0, "a user page is mapped with the user bit set");
    check((page_flags(KERNEL_LOAD_ADDRESS) & PAGE_USER) == 0,
          "kernel pages are not mapped for user access");
    if (user_mapped) {
        page_unmap(user_page);
        pmm_free_page(physical);
    }

    check(user_image_ok(reinterpret_cast<const uint8*>(&image_header), 16, nullptr,
                        nullptr) == false,
          "the kernel image is not accepted as a user image");
    check(user_image_ok(nullptr, 0, nullptr, nullptr) == false,
          "a short buffer is not accepted as a user image");
}

// The scheduler checks.  Creating tasks, being preempted by them, collecting their
// exit codes and getting their stacks back is the whole of phase 2a, so this is where
// "there is more than one thing running" stops being a claim and becomes a
// measurement.
//
// Every wait here is bounded the same way the timer check is: a scheduler that is
// broken must end in a failed check, never in a boot that never finishes.
namespace {
constexpr uint32 SPINNER_ROUNDS = 2;
constexpr uint32 SPINNER_TICKS_PER_ROUND = 2;
constexpr uint32 SPINNER_SPIN_LIMIT = 400000000u;
constexpr uint32 SPINNER_EXIT_ALPHA = 11;
constexpr uint32 SPINNER_EXIT_BETA = 22;
}  // namespace

// What a task created by the checks runs.  It does nothing at all to give the CPU up:
// it spins until the *timer* has charged it another SPINNER_TICKS_PER_ROUND ticks.
// That is the difference between a demo and a test.  A cooperative round robin would
// pass with `sched_yield()` here; this loop only ends if the timer really interrupted
// this task, really took the CPU away to somebody else, and really came back.
void spinner_entry() {
    Task* self = task_current();
    if (self == nullptr) {
        task_exit(1);
        return;
    }
    // The counter is written by the timer's interrupt handler, so the read has to be
    // volatile: a plain load may be hoisted out of the loop (nothing in the loop
    // writes it), and the task would then spin forever on a stale value while the
    // compiler reports nothing wrong.
    volatile uint32* ticks = &self->ticks;
    for (uint32 round = 1; round <= SPINNER_ROUNDS; ++round) {
        // The target is computed under the same pause as the print, deliberately.
        // Reading the counter *after* letting other tasks run would make a round's
        // length depend on when this task happened to get the CPU back -- the first
        // version did that, and a round that was meant to be two ticks long read its
        // start value late and ran for four.
        //
        // Preemption is suspended for the print because a console line written in
        // 20 ms slices interleaves with the other task's, and a test that reads lines
        // would then fail for a reason that has nothing to do with the scheduler.
        sched_pause();
        const uint32 start = *ticks;
        const uint32 goal = start + SPINNER_TICKS_PER_ROUND;
        kprintf("sched: %s round %u (ticks %u)\n", self->name, round, start);
        sched_resume();

        volatile uint32 spin = 0;
        while (*ticks < goal && spin < SPINNER_SPIN_LIMIT) {
            ++spin;
        }
        if (*ticks < goal) {
            break;              // the timer stopped: leave rather than spin forever
        }
    }
    task_exit(self->argument);
}

void run_scheduler_checks() {
    Task* boot = task_current();
    const uint32 boot_directory = boot != nullptr ? boot->directory : 0;
    const uint32 boot_pid = boot != nullptr ? boot->pid : TASK_NO_PARENT;
    check(boot != nullptr && boot->pid == 0 &&
              boot->state == TaskState::Running && task_live_count() == 1,
          "the boot context is task 0, it is running, and it is alone");

    HeapStats heap_before = heap_stats();
    Task* alpha = task_create_kernel("alpha", spinner_entry, SPINNER_EXIT_ALPHA);
    Task* beta = task_create_kernel("beta", spinner_entry, SPINNER_EXIT_BETA);
    const bool created = alpha != nullptr && beta != nullptr;
    // From here on, a failure to create tasks makes the rest of these checks
    // unevaluatable rather than failed: they are reported as skipped, which is what a
    // self-test owes a machine it could not exercise.
    const char* const no_tasks = "no task could be created";

    check_or_skip(created, created && alpha->pid != beta->pid && alpha->pid != 0 &&
                               beta->pid != 0 && alpha->pid != boot_pid,
                  "each new task gets a pid of its own", no_tasks);
    check_or_skip(created,
                  created && alpha->state == TaskState::New && alpha->directory != 0 &&
                      alpha->directory != boot_directory,
                  "a new task is not yet runnable and runs in a page directory of its own",
                  no_tasks);
    check_or_skip(created,
                  created && alpha->stack_base >= PMM_MIN_ADDRESS &&
                      alpha->stack_bytes == TASK_STACK_BYTES &&
                      task_stack_intact(alpha) && task_stack_intact(beta),
                  "a new task's kernel stack is heap memory with its canary in place",
                  no_tasks);

    uint32 alpha_code = 0;
    uint32 beta_code = 0;
    uint32 alpha_ticks = 0;
    uint32 beta_ticks = 0;
    uint32 alpha_switches = 0;
    uint32 beta_switches = 0;
    bool alpha_intact = false;
    bool beta_intact = false;
    bool flag_survived = false;
    uint32 reaped = 0;
    bool joined = false;
    if (created) {
        sched_add(alpha);
        sched_add(beta);
        // The interrupt flag is part of a context.  This yield happens with interrupts
        // on, and it is followed by a switch away and back; if EFLAGS were not saved
        // with the context, this task would return with the timer masked and would
        // never be preempted again -- a bug whose only symptom is "one task hogs the
        // CPU afterwards".  Checking the flag here is what makes that a failed check
        // instead of a mystery.
        const uint32 flags_before = read_eflags();
        sched_yield();
        const uint32 flags_after = read_eflags();
        flag_survived = (flags_before & EFLAGS_INTERRUPT_ENABLE) ==
                        (flags_after & EFLAGS_INTERRUPT_ENABLE);
        // `join` waits for the exit and keeps the slot, so the counters below are
        // still readable; task_reap() is what gives the stack back.
        joined = task_join(alpha->pid, &alpha_code) && task_join(beta->pid, &beta_code);
        alpha_ticks = alpha->ticks;
        beta_ticks = beta->ticks;
        alpha_switches = alpha->switches;
        beta_switches = beta->switches;
        alpha_intact = task_stack_intact(alpha);
        beta_intact = task_stack_intact(beta);
        reaped = task_reap();
    }

    check_or_skip(created, joined && alpha_code == SPINNER_EXIT_ALPHA &&
                               beta_code == SPINNER_EXIT_BETA,
                  "a task's exit code reaches the task that created it", no_tasks);
    check_or_skip(created, flag_survived,
                  "the interrupt flag survives being switched away and back", no_tasks);
    check_or_skip(created,
                  created && alpha_ticks >= SPINNER_ROUNDS * SPINNER_TICKS_PER_ROUND &&
                      beta_ticks >= SPINNER_ROUNDS * SPINNER_TICKS_PER_ROUND,
                  "the timer charged CPU time to every runnable task", no_tasks);
    check_or_skip(created, created && alpha_switches > 0 && beta_switches > 0,
                  "the round robin switched into every task", no_tasks);
    check_or_skip(created, created && alpha_intact && beta_intact,
                  "neither task overran its kernel stack", no_tasks);
    check_or_skip(created,
                  created && reaped == 2 && task_live_count() == 1 &&
                      heap_stats().used_bytes == heap_before.used_bytes,
                  "reaping both tasks gives their stacks back to the heap", no_tasks);

    // Two pieces of CPU state belong to a task, and both are read back from where
    // they actually live: esp0 from the TSS the CPU has loaded, CR3 from the
    // register.  After the demos above this task is running again, so both must name
    // *this* task.
    Task* here = task_current();
    check(here != nullptr && tss_kernel_stack() == here->stack_top &&
              read_cr3() == paging_active_directory() && read_cr3() == here->directory,
          "the TSS and CR3 name the task that is running");
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

    run_memory_checks();
    run_privilege_checks();
    run_filesystem_checks();
    run_scheduler_checks();

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

uint32 kernel_checks_skipped() {
    return checks_skipped;
}

uint32 kernel_scheduler_test() {
    checks_run = 0;
    checks_failed = 0;
    checks_skipped = 0;
    run_scheduler_checks();
    return checks_failed;
}

}  // namespace myos

extern "C" void kmain() {
    using namespace myos;

    serial_init();
    console_init();
    // Ownership of the CPU, in the order the hardware needs it: a GDT before any
    // segment register can be reloaded, an IDT before any interrupt can be
    // delivered, the PIC before it can be unmasked, and the devices last.  The TSS is
    // part of "ownership of the CPU" now: without it loaded, the first exception or
    // syscall taken from ring 3 would run on the user's own stack.
    gdt_init(tss_address(), sizeof(Tss));
    // The *address* of the symbol, not what it holds: `kernel_stack_top` is a label at
    // the top of the kernel's stack area, and passing its contents sets esp0 to
    // whatever happens to be in that memory.
    tss_init(reinterpret_cast<uint32>(&kernel_stack_top));
    idt_init();
    pic_init();
    pit_init();
    keyboard_init();
    klog("kernel started: gdt, tss, idt, pic, pit, keyboard");

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

    // Memory, in the order the hardware forces: the allocator reads the E820 map the
    // loader left behind, paging identity-maps everything the allocator can hand out
    // (so the kernel keeps running while the tables are built), and the heap takes
    // its arena once that mapping exists.
    pmm_init();
    pmm_reserve(KERNEL_LOAD_ADDRESS, image_header.size);
    paging_init();
    heap_init();

    // Tasks, before interrupts are ever enabled: the first timer tick must find a
    // scheduler and a task 0 rather than an empty table.  Task 0 is the context kmain
    // is already running in, so nothing switches here -- it is registered, not
    // started.
    sched_init();
    task_init(reinterpret_cast<uint32>(&kernel_stack_top));
    klog("scheduler started with the boot context as task 0");

    const PmmStats memory = pmm_stats();
    const HeapStats heap = heap_stats();
    kprintf("memory: %u KiB managed, %u KiB free, identity map to %p\n",
            memory.managed_pages * (PAGE_SIZE / 1024),
            memory.free_pages * (PAGE_SIZE / 1024), paging_identity_end());
    kprintf("heap: %u KiB arena, %u KiB free\n",
            heap.arena_bytes / 1024, heap.free_bytes / 1024);
    kprintf("tasks: task 0 is %s (pid 0), %u slot(s), %u KiB per kernel stack\n",
            task_current() != nullptr ? task_current()->name : "missing", TASK_MAX,
            TASK_STACK_BYTES / 1024);
    klog("memory: paging on, identity map in place, heap arena reserved");
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
        klog("myfs mounted, volume is clean");
    } else {
        kprintf("fs: not mounted: %s (%s)\n", fs_error_text(mounted),
                fs_mount_problem());
        klog("no filesystem mounted; file and user commands will say why");
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
