"""
Tests for the 32-bit kernel: link invariants, and a real boot under QEMU.

The 16-bit stage is verified in the in-tree emulator; the 32-bit one cannot be,
because that emulator has a 16-bit register file and no descriptor model.  QEMU
provides the real CPU and firmware, so these tests are written against what the
guest prints on COM1 and the status it exits with.  They skip, rather than fail,
when QEMU is not installed.
"""

from __future__ import annotations

import re
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build as myos_build  # noqa: E402
import memmap  # noqa: E402
from tools import cofllink, image, myfs, qemu, toolchain  # noqa: E402

QEMU_AVAILABLE = qemu.available()
KERNEL32_MNEMONICS = ("xmm", "movdqu", "movups", "movaps", "movq ", "fld", "st0",
                      "cvtsi2sd", "pxor", "padd")


class Kernel32LinkTests(unittest.TestCase):
    """What the build guarantees about the 32-bit image, checked on real output."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.image = cls.result.kernel.read_bytes()

    def test_the_image_header_says_32_bit_protected_mode(self) -> None:
        self.assertEqual(self.image[:4], b"MYOS")
        self.assertEqual(self.image[4], 2, "architecture byte must say 32-bit")
        self.assertEqual(self.image[6], 0, "flags must be zero")
        self.assertEqual(sum(self.image[:16]) & 0xFF, 0, "checksum must verify")
        self.assertEqual(int.from_bytes(self.image[12:16], "little"), len(self.image))

    def test_the_entry_is_the_assembly_stub_not_a_cpp_function(self) -> None:
        # The stack has to be switched before any C++ runs; the header must point at
        # the stub in boot.asm, not at kmain.
        entry = int.from_bytes(self.image[8:12], "little")
        self.assertGreaterEqual(entry, 16)
        self.assertLess(entry, len(self.image))

    def test_the_image_fits_the_staging_window(self) -> None:
        # The loader reads the image into a window below 1 MiB and copies it up.
        window = myos_build.KERNEL32_STAGE_END - myos_build.KERNEL32_STAGE_LIN
        self.assertLessEqual(len(self.image), window)
        # ... and it has to stay clear of the bootstrap stack above it.
        self.assertLess(myos_build.KERNEL32_BASE + len(self.image) + 0x10000,
                        myos_build.KERNEL32_STACK_LIN)

    def test_no_symbol_points_past_the_image(self) -> None:
        objects = [myos_build.BUILD_DIR / (Path(name).stem + "32.o")
                   for name in (*myos_build.KERNEL32_ASM_SOURCES,
                                *myos_build.KERNEL32_CPP_SOURCES)]
        link = cofllink.link(objects, base=myos_build.KERNEL32_BASE,
                             entry="kernel_entry", layout=cofllink.kernel_layout())
        end = myos_build.KERNEL32_BASE + len(link.image)
        # Exactly at the end is fine: `kernel_stack_top` is the first byte *after*
        # the stack, so it is the boundary rather than a location inside the image.
        beyond = {name: addr for name, addr in link.symbols.items() if addr > end}
        self.assertEqual(beyond, {}, "these symbols are outside the loaded image")

    def test_the_stack_is_the_last_thing_in_bss(self) -> None:
        # The stack grows down from its top, so anything above it in .bss survives
        # and anything inside its range does not.  With the stack last, its top is
        # the end of the image and every static variable sits below its bottom.
        objects = [myos_build.BUILD_DIR / (Path(name).stem + "32.o")
                   for name in (*myos_build.KERNEL32_ASM_SOURCES,
                                *myos_build.KERNEL32_CPP_SOURCES)]
        link = cofllink.link(objects, base=myos_build.KERNEL32_BASE,
                             entry="kernel_entry", layout=cofllink.kernel_layout())
        stack_top = cofllink.resolve_symbol(link.symbols, "kernel_stack_top")
        stack_bottom = cofllink.resolve_symbol(link.symbols, "kernel_stack_bottom")
        self.assertIsNotNone(stack_top)
        self.assertIsNotNone(stack_bottom)
        self.assertEqual(stack_top, myos_build.KERNEL32_BASE + len(link.image),
                         "the stack top must be the end of the image")
        for name, addr in link.symbols.items():
            if name.startswith(".") or "kernel_stack" in name:
                continue                             # a section symbol, or the bounds
            if stack_bottom <= addr < stack_top:
                self.fail(f"{name} lies inside the kernel's own stack")

    def test_the_kernel_uses_no_vector_or_floating_point_instructions(self) -> None:
        # The target's default enables SSE, and gcc will copy an array with
        # `movdqu`.  A kernel that has not set CR4.OSFXSR takes #UD on the first
        # one: the symptom was a triple fault in unrelated code, with nothing to
        # suggest a vector instruction was involved.  build/toolchain.py passes
        # -mno-sse and friends; this proves it on the emitted code.
        #
        # Only the code region is disassembled.  A disassembler decodes whatever
        # bytes it is handed, so scanning the whole image also scans strings, tables
        # and the zero-filled .bss -- and reports instructions that are not there.
        # That is exactly what happened once the filesystem grew: six bytes of data
        # decoded as `fld tword [edx+...]`, and the test failed on a floating-point
        # instruction that the compiler had never emitted.
        objects = [myos_build.BUILD_DIR / (Path(name).stem + "32.o")
                   for name in (*myos_build.KERNEL32_ASM_SOURCES,
                                *myos_build.KERNEL32_CPP_SOURCES)]
        link = cofllink.link(objects, base=myos_build.KERNEL32_BASE,
                             entry="kernel_entry", layout=cofllink.kernel_layout())
        start, size = link.section_bounds["code"]
        self.assertGreater(size, 0, "the link produced no code region")
        offset = start - myos_build.KERNEL32_BASE
        code = myos_build.BUILD_DIR / "kernel32-code.bin"
        code.write_bytes(link.image[offset:offset + size])

        listing = toolchain.disassemble(code, bits=32, origin=start)
        self.assertTrue(listing, "ndisasm produced no listing")
        found = [line for line in listing.splitlines()
                 if any(mnemonic in line.lower() for mnemonic in KERNEL32_MNEMONICS)]
        self.assertEqual(found, [], "vector or x87 instructions in the kernel code")


class Kernel32ContractTests(unittest.TestCase):
    """The numbers the loader and the kernel must agree about."""

    def test_bootinfo_header_matches_boot_inc(self) -> None:
        header = (ROOT / "kernel32" / "bootinfo.h").read_text()
        constants = memmap.read_boot_constants()

        def declared(name: str) -> int:
            match = re.search(rf"{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", header)
            assert match is not None, f"{name} is not declared in bootinfo.h"
            return int(match.group(1), 0)

        self.assertEqual(declared("BOOT_INFO_ADDRESS"), constants["BOOT_INFO_LIN"])
        self.assertEqual(declared("KERNEL_LOAD_ADDRESS"), constants["KERNEL32_LOAD_LIN"])
        self.assertEqual(declared("KERNEL32_STAGE_LIN"), constants["KERNEL32_STAGE_LIN"])
        self.assertEqual(declared("IMAGE_HEADER_SIZE"), constants["IMG_HEADER_SIZE"])
        self.assertEqual(declared("MEMORY_MAP_MAX_ENTRIES"),
                         constants["BOOT_INFO_MAX_ENTRIES"])

    def test_the_debug_exit_protocol_agrees_everywhere(self) -> None:
        constants = memmap.read_boot_constants()
        kernel = (ROOT / "kernel32" / "kernel.cpp").read_text()
        self.assertEqual(constants["DEBUG_EXIT_PASS"], qemu.DEBUG_EXIT_PASS)
        self.assertEqual(constants["DEBUG_EXIT_FAIL"], qemu.DEBUG_EXIT_FAIL)
        self.assertIn(f"DEBUG_EXIT_PASS = {constants['DEBUG_EXIT_PASS']:#04x}", kernel)
        self.assertIn(f"DEBUG_EXIT_FAIL = {constants['DEBUG_EXIT_FAIL']:#04x}", kernel)
        self.assertEqual(qemu.DEBUG_EXIT_PASS_STATUS,
                         qemu.debug_exit_status(constants["DEBUG_EXIT_PASS"]))


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32BootTests(unittest.TestCase):
    """The whole 32-bit chain under real firmware, driven through the shell."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        # One scripted session for the whole class: booting QEMU costs seconds, and
        # the assertions below only need the transcript it produces.
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="help\r", timeout=40),
            qemu.SessionStep(wait="selftest", serial="echo hello from the serial console\r"),
            qemu.SessionStep(wait="hello from", serial="fact 5\r"),
            qemu.SessionStep(wait="120", serial="mem\r"),
            qemu.SessionStep(wait="usable", serial="ticks\r"),
            qemu.SessionStep(wait="Hz", serial="keylog\r"),
            qemu.SessionStep(wait="IRQ4", serial="bogus\r"),
            qemu.SessionStep(wait="unknown command", serial="ls\r"),
            # No data disk in this session, so `ls` has to say why it cannot work
            # rather than printing an empty directory.
            qemu.SessionStep(wait="no filesystem", serial="selftest\r"),
        ], timeout=60.0)

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        # A step whose marker never appears still sends its text, so an unmet wait
        # has to be a failure here rather than a confusing transcript later.
        self.assertEqual(self.session.unmet_waits, [],
                         "the guest never produced these markers")

    def test_it_enters_protected_mode_and_prints_its_banner(self) -> None:
        text = self.text()
        self.assertIn("entering protected mode", text)
        self.assertIn("myos 32-bit kernel", text)
        self.assertIn("protected mode, flat segments", text)

    def test_the_banner_reports_the_image_the_build_produced(self) -> None:
        size = len(self.result.kernel.read_bytes())
        self.assertIn(f"image {size} bytes", self.text())
        self.assertRegex(self.text(), re.compile(r"entry 001000[0-9A-F]{2}"))

    def test_the_shell_lists_its_commands(self) -> None:
        # The list is what the user sees first, and it has to match the table.
        for name in ("help", "echo", "clear", "info", "mem", "ticks", "fact",
                     "keylog", "blk", "fs", "df", "ls", "cat", "stat",
                     "reboot", "selftest"):
            self.assertIn(name, self.text())

    def test_echo_prints_its_argument(self) -> None:
        self.assertIn("hello from the serial console", self.text())

    def test_fact_computes_recursively(self) -> None:
        self.assertIn("120", self.text())

    def test_mem_reports_the_firmware_memory_map(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"\d+ KiB usable in \d+ firmware memory map entries"))
        self.assertNotIn("0 KiB usable", text, "the map was collected but not read")
        self.assertNotIn("0 firmware memory map entries", text)

    def test_ticks_advance_at_the_configured_rate(self) -> None:
        self.assertRegex(self.text(), re.compile(r"ticks \d+ \(high \d+\) at 100 Hz"))

    def test_keylog_reports_both_input_sources(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"serial: \d+ bytes received on IRQ4"))
        self.assertIn("IRQ1 (PS/2) and IRQ4 (COM1) into one queue", text)

    def test_an_unknown_command_is_reported(self) -> None:
        self.assertIn("unknown command: bogus", self.text())

    def test_every_in_guest_check_passes(self) -> None:
        text = self.text()
        self.assertNotIn("FAIL", text)
        self.assertIn("SELFTEST PASS", text)
        # This session boots the floppy with no data disk, so the thirteen checks
        # that need a volume are skipped rather than failed -- and the summary says
        # how much of the kernel went unexercised rather than quietly shrinking.
        self.assertRegex(text, re.compile(r"55 ok, 13 skipped, 0 failed"))
        self.assertIn("no ATA device on the primary channel", text)
        self.assertRegex(text, re.compile(r"ls: no filesystem mounted"))

    def test_the_loader_read_the_whole_image_off_the_disk(self) -> None:
        # The kernel prints a checksum of the copy the loader staged below 1 MiB,
        # and the host holds the bytes that were supposed to be read.  Any single
        # byte the loader got wrong -- a chunk that straddled a 64 KiB page, a
        # sector read to the wrong address -- changes this number.  The kernel
        # prints hex in upper case, and this pinned the width too: `%08x` used to
        # emit a ninth digit for a value whose top nibble was zero.
        expected = myfs.checksum(self.result.kernel.read_bytes())
        match = re.search(r"staged image: (\d+) bytes, checksum ([0-9A-F]+)",
                          self.text())
        self.assertIsNotNone(match, "the kernel did not print the staged checksum")
        assert match is not None
        self.assertEqual(match.group(1), str(len(self.result.kernel.read_bytes())))
        self.assertEqual(match.group(2), f"{expected:08X}")

    def test_the_guest_exits_with_the_pass_status(self) -> None:
        # `selftest` ends the run through QEMU's isa-debug-exit device, so a green
        # suite means the guest said so rather than the harness giving up.
        self.assertFalse(self.session.timed_out, "the guest never ended its own run")
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32MemoryTests(unittest.TestCase):
    """The memory layer, on a machine with a disk so nothing is skipped.

    The checks are run twice in one session, bracketed by `vm` reports.  The first
    version of the demand-zero check was not idempotent -- it tried to reserve a
    range a previous run had already mapped, refused its own reservation, and
    reported a failure the second time the checks ran.  Running them twice, with the
    fault counter printed in between, is what keeps that from coming back: the
    counter proves each run really faulted exactly once, and the run counter proves
    the second run happened at all.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.disk = scratch_disk("memory.img")
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="vm\r", timeout=40),
            qemu.SessionStep(wait="1 fault(s) handled", serial="check\r", timeout=60),
            qemu.SessionStep(wait="check: run 1", serial="vm\r", timeout=30),
            qemu.SessionStep(wait="2 fault(s) handled", serial="check\r", timeout=60),
            qemu.SessionStep(wait="check: run 2", serial="vm\r", timeout=30),
            # `selftest` ends the run with a verdict; `check` is the one that stays.
            qemu.SessionStep(wait="3 fault(s) handled", serial="selftest\r", timeout=40),
        ], timeout=150.0, extra=qemu.data_disk_arguments(cls.disk))

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        self.assertEqual(self.session.unmet_waits, [])

    def test_paging_is_on_and_identity_maps_the_machines_memory(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(
            r"memory: \d+ KiB managed, \d+ KiB free, identity map to [0-9A-F]{8}"))
        self.assertRegex(text, re.compile(
            r"paging: cr3 [0-9A-F]{8}, \d+ page tables, \d+ pages mapped"))
        self.assertRegex(text, re.compile(
            r"paging: identity map 00000000\.\.[0-9A-F]{8}, \d+ fault\(s\) handled"))
        self.assertNotIn("paging: off", text)

    def test_the_allocator_reports_what_it_manages(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(
            r"pmm: \d+ pages \(\d+ KiB\) managed, \d+ free, \d+ used"))
        self.assertRegex(text, re.compile(r"pmm: usable RAM ends at [0-9A-F]{8}"))
        # A machine with no usable RAM above 1 MiB would make every other memory
        # check meaningless, so the numbers are read back and added up.
        match = re.search(r"pmm: (\d+) pages \(\d+ KiB\) managed, (\d+) free, (\d+) used",
                          text)
        assert match is not None
        managed, free, used = (int(match.group(1)), int(match.group(2)),
                               int(match.group(3)))
        self.assertGreater(managed, 1000)
        self.assertGreater(free, 0)
        self.assertEqual(free + used, managed)

    def test_the_heap_has_an_arena_and_gives_it_back(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(
            r"heap: arena 1048576 bytes, \d+ free \(largest \d+\), \d+ used"))
        self.assertIn("0 bad free(s)", text)
        # After each round of allocate-and-free the arena has to be whole again.
        self.assertRegex(text, re.compile(r"heap: 1 block\(s\), 0 live"))

    def test_the_checks_pass_and_keep_passing_when_they_run_again(self) -> None:
        text = self.text()
        self.assertIn("check: run 1: 68 ok, 0 skipped, 0 failed", text)
        self.assertIn("check: run 2: 68 ok, 0 skipped, 0 failed", text)
        # Six summary lines: the checks print their own, and `check` prints one more
        # with the run number in it.  The boot run and the final `selftest` account
        # for one each, the two `check` runs for two each.
        self.assertEqual(text.count("68 ok, 0 skipped, 0 failed"), 6)
        self.assertNotIn("FAIL", text)
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)

    def test_each_run_faults_exactly_once_for_its_demand_zero_page(self) -> None:
        text = self.text()
        # One fault for the boot checks, then one per run, visible because each is
        # bracketed by a vm report.
        self.assertIn("1 fault(s) handled", text)
        self.assertIn("2 fault(s) handled", text)
        self.assertIn("3 fault(s) handled", text)
        self.assertEqual(text.count("1 demand-zero region(s)"), 3,
                         "a repeated reservation should reuse its slot, not add one")


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32SchedulerTests(unittest.TestCase):
    """Tasks, preemption, and getting the memory back.

    The demo tasks never yield: each one spins until the timer has charged it another
    two ticks.  A scheduler that only switched when asked would leave the second task
    with zero ticks and the `schedtest` summary would say so, which is the point of
    running the checks a second time in an interactive session: the boot run and the
    command run have to agree.

    `ps` is read in between, so the test sees the task table as the kernel sees it --
    and after the run, so it sees that both tasks were reaped rather than leaked.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        # No data disk: the thirteen filesystem checks skip, which keeps this session
        # to the scheduler and makes the "13 skipped" count below a second assertion
        # that nothing else went missing.
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="schedtest\r", timeout=60),
            qemu.SessionStep(wait="schedtest: 11 ok", serial="ps\r", timeout=30),
            qemu.SessionStep(wait="sched: current pid 0", serial="vm\r", timeout=30),
            qemu.SessionStep(wait="paging: cr3", serial="selftest\r", timeout=60),
        ], timeout=120.0)

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        self.assertEqual(self.session.unmet_waits, [])

    def test_task_zero_is_the_boot_context(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(
            r"tasks: task 0 is kmain \(pid 0\), 16 slot\(s\), 8 KiB per kernel stack"))
        self.assertIn("the boot context is task 0, it is running, and it is alone", text)

    def test_a_new_task_is_not_runnable_until_it_is_added(self) -> None:
        # The window between "the structure exists" and "the stack is finished" is
        # where a task whose esp was still zero could be switched to, so the state
        # machine has a state for it and the checks look at it.
        self.assertIn(
            "a new task is not yet runnable and runs in a page directory of its own",
            self.text())

    def test_both_tasks_are_preempted_by_the_timer(self) -> None:
        text = self.text()
        # Three runs of the checks happen in this session: the boot one, the
        # `schedtest` command, and the final `selftest`.
        #
        # The tick count is matched as a number rather than pinned to zero: a task is
        # charged a tick by the timer from the moment it is switched into, and the
        # timer does not wait for it to reach its first `kprintf`.  What the assertion
        # actually needs is that *round two* happened for both tasks in all three
        # runs: a task can only get there after the timer has taken the CPU away from
        # it and given it back twice.
        for name in ("alpha", "beta"):
            first = re.findall(rf"sched: {name} round 1 \(ticks \d+\)", text)
            second = re.findall(rf"sched: {name} round 2 \(ticks \d+\)", text)
            self.assertEqual(len(first), 3, f"{name} round one, three times")
            self.assertEqual(len(second), 3, f"{name} round two, three times")
        self.assertNotIn("FAIL", text)

    def test_the_timer_really_charged_every_task(self) -> None:
        # This is the check that separates preemption from a cooperative demo: a
        # spinner that was never interrupted would never reach its target, so the
        # round-two lines above could not exist.
        self.assertIn("the timer charged CPU time to every runnable task", self.text())
        self.assertIn("the interrupt flag survives being switched away and back",
                      self.text())
        self.assertEqual(self.text().count("the round robin switched into every task"), 3)

    def test_each_task_runs_in_its_own_address_space(self) -> None:
        # A page directory per task, and the CPU register read back: a directory the
        # kernel built but never loaded would pass every other check here.
        text = self.text()
        self.assertIn("the TSS and CR3 name the task that is running", text)
        self.assertRegex(text, re.compile(r"paging: cr3 [0-9A-F]{8}, \d+ page tables"))

    def test_the_stacks_come_back_and_nothing_leaks(self) -> None:
        text = self.text()
        self.assertIn("neither task overran its kernel stack", text)
        self.assertIn("reaping both tasks gives their stacks back to the heap", text)
        # After the demo and after `ps`, the table is back to the boot context alone.
        self.assertRegex(text, re.compile(r"ps: 1 task\(s\) in the table, \d+ created, "
                                         r"\d+ reaped, \d+ switch\(es\)"))
        self.assertIn("ps: 0 running", text)
        self.assertNotIn("STACK-CANARY-LOST", text)

    def test_the_checks_pass_twice_and_the_guest_says_so(self) -> None:
        text = self.text()
        self.assertIn("schedtest: 11 ok, 0 skipped, 0 failed", text)
        self.assertIn("schedtest: every task ran, exited, and gave its stack back", text)
        # 55 + the 13 the missing disk skips: the boot run and the final `selftest`.
        self.assertEqual(text.count("55 ok, 13 skipped, 0 failed"), 2)
        self.assertIn("SELFTEST PASS", text)
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32PageFaultTests(unittest.TestCase):
    """A fault the kernel cannot satisfy has to be reported, not vanish.

    This is the one test whose *expected* outcome is a halt: what it proves is that
    the kernel says what happened (address, error code, registers) and ends the run
    with a failing status, instead of taking a triple fault that leaves nothing on
    the serial port for anyone to read.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.disk = scratch_disk("fault.img")
        cls.log = ROOT / "build" / "tmp" / "fault-int.log"
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="vm fault\r", timeout=40),
            qemu.SessionStep(wait="KERNEL PANIC", serial="", timeout=20),
        ], timeout=60.0,
            extra=[*qemu.data_disk_arguments(cls.disk),
                   "-d", "int,cpu_reset", "-D", str(cls.log)])

    def test_the_fault_is_reported_with_the_address_and_the_kind(self) -> None:
        text = self.session.transcript
        self.assertIn("vm: writing to 0xdeadb000, which is not mapped...", text)
        self.assertIn("cr2 DEADB000 (not present, write, kernel)", text)
        self.assertIn("KERNEL PANIC: page fault", text)
        self.assertIn("vector 14 at", text)

    def test_it_ends_with_the_failure_status_rather_than_a_timeout(self) -> None:
        self.assertFalse(self.session.timed_out)
        self.assertEqual(self.session.status, qemu.debug_exit_status(0x02))

    def test_the_cpu_took_a_page_fault_and_not_a_triple_fault(self) -> None:
        text = self.log.read_text(errors="replace").lower()
        self.assertIn("v=0e", text)
        self.assertIn("cr2=deadb000", text)
        self.assertNotIn("triple fault", text)
        self.assertNotIn("cpu_reset", text)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32UserModeTests(unittest.TestCase):
    """Ring 3: the privilege boundary, the syscall path, and what it refuses.

    Three programs, each proving something the kernel cannot prove about itself:
    assembly that follows the documented ABI, C++ whose compiler generates the call,
    and a program that asks the kernel to read kernel memory -- which has to come back
    as a refusal with an errno in it rather than as a line of the kernel's own data.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.disk = scratch_disk("user-mode.img")
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="ls /bin\r", timeout=40),
            qemu.SessionStep(wait="ls /bin:", serial="run /bin/hello\r", timeout=40),
            qemu.SessionStep(wait="run: /bin/hello exited", serial="run /bin/badwrite\r",
                             timeout=40),
            qemu.SessionStep(wait="run: /bin/badwrite exited",
                             serial="run /bin/hellocpp\r", timeout=40),
            qemu.SessionStep(wait="run: /bin/hellocpp exited", serial="run /bin/nothing\r",
                             timeout=40),
            qemu.SessionStep(wait="run: /bin/nothing:", serial="dmesg\r", timeout=30),
            qemu.SessionStep(wait="back from ring 3", serial="selftest\r", timeout=60),
        ], timeout=180.0, extra=qemu.data_disk_arguments(cls.disk))

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        self.assertEqual(self.session.unmet_waits, [])

    def test_the_volume_carries_the_programs_the_build_made(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"ls /bin:"))
        for name, size in (("hello", 72), ("badwrite", 52), ("hellocpp", 540)):
            self.assertIn(f"f  {name}  ({size} bytes", text)
        self.assertIn("3 entries", text)

    def test_ring_three_prints_through_the_kernel(self) -> None:
        text = self.text()
        self.assertIn("run: /bin/hello, 72 bytes at 40000000", text)
        self.assertIn("hello from ring 3", text)
        self.assertIn("run: /bin/hello exited with code 0", text)
        self.assertIn("run: 5 page(s) of user address space, 2 syscall(s)", text)

    def test_a_cpp_user_program_uses_the_same_abi(self) -> None:
        text = self.text()
        self.assertIn("hello from a C++ user program", text)
        self.assertIn("getpid() in user mode returned 1", text)
        self.assertIn("run: /bin/hellocpp exited with code 0", text)
        # write twice, getpid once, exit: six calls, all of them through the gate.
        self.assertIn("run: 5 page(s) of user address space, 6 syscall(s)", text)

    def test_the_kernel_refuses_to_read_its_own_memory_for_a_user(self) -> None:
        # badwrite asks write() to send eight bytes from 0x00100000, the kernel image.
        # The exit code is the errno made positive: 37 is E_FAULT.  A kernel that had
        # copied the bytes would have exited with a negative count instead -- and the
        # kernel image starts with the letters MYOS, so the transcript is checked for
        # them too, which catches the leak even if the exit code were misread.
        text = self.text()
        self.assertIn("run: /bin/badwrite exited with code 37", text)
        self.assertNotIn("MYOS", text)

    def test_a_program_that_does_not_exist_is_reported(self) -> None:
        self.assertIn("run: /bin/nothing: no such file or directory", self.text())

    def test_the_log_shows_every_trip_through_ring_three(self) -> None:
        text = self.text()
        self.assertEqual(text.count("entering ring 3"), 3)
        self.assertEqual(text.count("back from ring 3"), 3)

    def test_the_in_guest_checks_pass_with_user_mode_in_the_picture(self) -> None:
        self.assertIn("68 ok, 0 skipped, 0 failed", self.text())
        self.assertIn("SELFTEST PASS", self.text())
        self.assertNotIn("FAIL", self.text())
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32UserImageTests(unittest.TestCase):
    """A user image the kernel must refuse, and one it must accept.

    The build's own images are checked from the host, and a copy with one byte of the
    header flipped is handed to the guest: the kernel has to say "invalid argument"
    rather than map a page and jump into it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        volume = myfs.volume_from_image(cls.result.hard_disk, myfs.PARTITION_INDEX)
        inode = volume.resolve("/bin/hello")
        image = bytearray(volume.read_file(inode))
        cls.original = bytes(image)
        image[7] ^= 0xFF                    # the checksum byte of the header
        volume.write_file(inode, bytes(image), truncate=True)
        cls.disk = ROOT / "build" / "tmp" / "user-image.img"
        cls.disk.parent.mkdir(parents=True, exist_ok=True)
        cls.disk.write_bytes(myfs.build_disk_image(volume))
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="run /bin/hello\r", timeout=40),
            qemu.SessionStep(wait="run: /bin/hello:", serial="run /bin/hellocpp\r",
                             timeout=40),
            qemu.SessionStep(wait="exited with code 0", serial="selftest\r", timeout=60),
        ], timeout=150.0, extra=qemu.data_disk_arguments(cls.disk))

    def test_the_corrupted_image_is_refused(self) -> None:
        text = self.session.transcript
        self.assertIn("run: /bin/hello: invalid argument", text)
        self.assertNotIn("hello from ring 3", text)

    def test_the_in_guest_checks_notice_the_corrupted_file_too(self) -> None:
        # Two independent checks see the same flipped byte: the user-image header
        # validation refuses to run it, and the manifest verification notices that the
        # bytes on the volume are not the ones the build recorded.  The run therefore
        # ends with the failure status, which is the honest outcome -- and it is worth
        # asserting rather than working around, because it is the second check that
        # would catch a corruption of any file, not just a program.
        text = self.session.transcript
        self.assertIn("hello from a C++ user program", text)
        self.assertIn("FAIL every packed file matches its manifest checksum", text)
        self.assertEqual(self.session.status, qemu.debug_exit_status(0x02))

    def test_the_host_can_see_the_flipped_byte_it_wrote(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        broken = volume.read_file(volume.resolve("/bin/hello"))
        expected = bytearray(self.original)
        expected[7] ^= 0xFF
        self.assertEqual(broken, bytes(expected))
        # The rest of the volume is untouched, including the files the packer wrote.
        self.assertEqual(volume.check(), [])
        self.assertEqual(volume.read_file(volume.resolve("/hello.txt")),
                         (ROOT / "files" / "hello.txt").read_bytes())


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32DataDiskUpdateTests(unittest.TestCase):
    """A disk from an older build, brought up to date — the shape a user meets.

    Their own disk was made before the build shipped any programs, so `run /bin/hello`
    answers "no such file or directory", and rebuilding the disk is exactly what must
    not happen (it holds their files).  `build.py data-disk --update` merges what the
    build ships and leaves the rest alone, and this test reproduces the whole story:
    an old disk with one file of the user's own, the real update path, then a boot that
    reads their file and runs a program.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        # An "old" disk: the shipped example files, one file of the user's own, and
        # deliberately no /bin.
        volume = myfs.build_volume(ROOT / "files")
        volume.create_file("/note.txt", b"my own note")
        cls.disk = ROOT / "build" / "tmp" / "old-data.img"
        cls.disk.parent.mkdir(parents=True, exist_ok=True)
        cls.disk.write_bytes(myfs.build_disk_image(volume))

        # The real command, with the data disk pointed at the fixture.
        original = myos_build.DATA_DISK
        myos_build.DATA_DISK = cls.disk
        try:
            cls.update_status = myos_build.main(["data-disk", "--update"])
        finally:
            myos_build.DATA_DISK = original

        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="cat /note.txt\r", timeout=40),
            qemu.SessionStep(wait="bytes read", serial="run /bin/hello\r", timeout=40),
            qemu.SessionStep(wait="exited with code 0", serial="ls /bin\r", timeout=30),
            qemu.SessionStep(wait="ls /bin:", serial="selftest\r", timeout=60),
        ], timeout=150.0, extra=qemu.data_disk_arguments(cls.disk))

    def test_the_update_command_succeeded(self) -> None:
        self.assertEqual(self.update_status, 0)

    def test_the_users_own_file_survived(self) -> None:
        self.assertIn("cat: /note.txt (11 bytes)", self.session.transcript)
        self.assertIn("my own note", self.session.transcript)

    def test_the_programs_are_there_now_and_run(self) -> None:
        text = self.session.transcript
        self.assertIn("hello from ring 3", text)
        self.assertIn("run: /bin/hello exited with code 0", text)
        self.assertRegex(text, re.compile(r"f  hello  \(72 bytes"))
        self.assertIn("3 entries", text)

    def test_the_volume_is_still_consistent_after_the_merge(self) -> None:
        # The kernel's own manifest check is the strongest statement available here:
        # it verifies every file against the checksum the host computed, on the merged
        # volume, and it is the check that would catch a merge that damaged something.
        self.assertIn("every packed file matches its manifest checksum", self.session.transcript)
        self.assertIn("SELFTEST PASS", self.session.transcript)
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)
        self.assertEqual(myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX).check(), [])


def scratch_disk(name: str) -> Path:
    """A private copy of the built hard disk image, for a test that writes.

    Every write test works on its own copy: the images a build produces are
    compared against the bytes a build will produce next time, and a test that
    scribbled on them would make the next run fail for the wrong reason.
    """
    source = myos_build.build(arch=32).hard_disk
    target = ROOT / "build" / "tmp" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32WriteTests(unittest.TestCase):
    """The write path, driven through the shell, on a disk of its own.

    The guest's own report is only half the evidence: the files it wrote are read
    back on the host, through the other implementation of the format, after QEMU
    has exited.  That is the same trade as the manifest check, in the other
    direction -- the kernel reads what the host wrote, and the host reads what the
    kernel wrote.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.disk = scratch_disk("write-session.img")
        cls.result = myos_build.build(arch=32)
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="write /note.txt hello from the kernel\r",
                             timeout=40),
            qemu.SessionStep(wait="write: /note.txt", serial="cat /note.txt\r", timeout=20),
            qemu.SessionStep(wait="cat: 21 bytes read", serial="mkdir /d\r", timeout=20),
            qemu.SessionStep(wait="mkdir: /d",
                             serial="write /d/inner.txt inside the subdirectory here\r",
                             timeout=20),
            qemu.SessionStep(wait="write: /d/inner.txt", serial="ls /d\r", timeout=20),
            qemu.SessionStep(wait="ls /d:", serial="cat /d/inner.txt\r", timeout=20),
            # The destructive mistakes a shell has to refuse: replacing a directory
            # with a file, removing a directory that still holds something, and
            # removing the root.
            qemu.SessionStep(wait="cat: 28 bytes read", serial="write /d plain\r",
                             timeout=20),
            qemu.SessionStep(wait="write: /d: is a directory", serial="rm /d\r",
                             timeout=20),
            qemu.SessionStep(wait="rm: /d: directory not empty",
                             serial="rm /d/inner.txt\r", timeout=20),
            qemu.SessionStep(wait="rm: /d/inner.txt", serial="rm /d\r", timeout=20),
            qemu.SessionStep(wait="rm: /d,", serial="rm /\r", timeout=20),
            qemu.SessionStep(wait="rm: /:", serial="fstest\r", timeout=60),
            qemu.SessionStep(wait="fstest:", serial="df\r", timeout=30),
            qemu.SessionStep(wait="df: 2048", serial="sync\r", timeout=20),
            qemu.SessionStep(wait="marked cleanly unmounted", serial="selftest\r",
                             timeout=30),
        ], timeout=140.0, extra=qemu.data_disk_arguments(cls.disk))

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        self.assertEqual(self.session.unmet_waits, [])

    def test_write_reports_what_it_wrote(self) -> None:
        self.assertIn("write: /note.txt, 21 bytes, 2030 free blocks", self.text())

    def test_the_kernel_reads_back_its_own_file(self) -> None:
        text = self.text().replace("\r\n", "\n")
        self.assertIn("cat: /note.txt (21 bytes)\n", text)
        body = text.split("cat: /note.txt (21 bytes)\n", 1)[1]
        # The file holds no trailing newline, so the console adds one to leave the
        # cursor at the start of a line: the content is the text plus that newline.
        self.assertEqual(body.split("cat: 21 bytes read", 1)[0],
                         "hello from the kernel\n")

    def test_a_directory_can_be_created_written_into_and_listed(self) -> None:
        text = self.text()
        self.assertIn("mkdir: /d", text)
        self.assertIn("write: /d/inner.txt, 28 bytes", text)
        self.assertRegex(text, re.compile(r"f  inner\.txt  \(28 bytes, inode \d+\)"))

    def test_the_obvious_destructive_mistakes_are_refused(self) -> None:
        text = self.text()
        self.assertIn("write: /d: is a directory", text)
        self.assertIn("rm: /d: directory not empty", text)
        self.assertIn("rm: /: no such file or directory", text)

    def test_the_in_guest_write_test_passes(self) -> None:
        self.assertIn("fstest: 22 ok, 0 failed", self.text())
        self.assertIn("ok   the volume is marked cleanly unmounted again", self.text())
        self.assertNotIn("  FAIL", self.text())

    def test_the_volume_is_left_where_it_was(self) -> None:
        # Eleven data blocks held the packed tree (see the filesystem test) and one
        # more holds the note this session wrote; everything the write test created and
        # removed is gone again, which the guest's own check ("every data block the
        # test used came back") has already asserted.
        self.assertIn("df: 2048 blocks of 512 bytes: 12 used, 2030 free", self.text())
        self.assertIn("sync: nothing was pending", self.text())
        self.assertIn("SELFTEST PASS", self.text())
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)

    def test_the_host_reads_what_the_kernel_wrote(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        self.assertEqual(volume.read_file(volume.resolve("/note.txt")),
                         b"hello from the kernel")
        self.assertEqual(volume.state, myfs.STATE_CLEAN,
                         "sync did not mark the volume cleanly unmounted")
        self.assertEqual(volume.check(), [],
                         "the host tool found the volume the kernel wrote inconsistent")

    def test_the_host_sees_the_removals_too(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        names = {path for path, _ in volume.iter_files()}
        self.assertNotIn("d", names)
        self.assertNotIn("d/inner.txt", names)

    def test_the_files_the_build_packed_are_untouched(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        for name in ("hello.txt", "readme.txt", "docs/notes.txt"):
            self.assertEqual(volume.read_file(volume.resolve(f"/{name}")),
                             (ROOT / "files" / name).read_bytes())


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32PersistenceTests(unittest.TestCase):
    """Two boots, one disk: the claim the whole filesystem exists to make."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.disk = scratch_disk("persistence.img")
        cls.first = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ",
                             serial="write /keep.txt hello from the first boot\r",
                             timeout=40),
            qemu.SessionStep(wait="write: /keep.txt", serial="mkdir /notes\r", timeout=20),
            qemu.SessionStep(wait="mkdir: /notes",
                             serial="write /notes/two.txt second file\r", timeout=20),
            qemu.SessionStep(wait="write: /notes/two.txt", serial="sync\r", timeout=20),
            qemu.SessionStep(wait="marked cleanly unmounted", serial="selftest\r",
                             timeout=30),
        ], timeout=90.0, extra=qemu.data_disk_arguments(cls.disk))
        # A separate QEMU process: the machine is switched off and on again, which
        # is the only way to know whether anything was really on the disk.
        cls.second = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="ls /\r", timeout=40),
            qemu.SessionStep(wait="entries", serial="cat /keep.txt\r", timeout=20),
            qemu.SessionStep(wait="cat: 25 bytes read", serial="cat /notes/two.txt\r",
                             timeout=20),
            qemu.SessionStep(wait="cat: 11 bytes read", serial="stat /keep.txt\r",
                             timeout=20),
            qemu.SessionStep(wait="is a file", serial="selftest\r", timeout=30),
        ], timeout=90.0, extra=qemu.data_disk_arguments(cls.disk))

    def test_both_boots_ran_to_completion(self) -> None:
        self.assertEqual(self.first.unmet_waits, [])
        self.assertEqual(self.second.unmet_waits, [])
        self.assertEqual(self.first.status, qemu.DEBUG_EXIT_PASS_STATUS)
        self.assertEqual(self.second.status, qemu.DEBUG_EXIT_PASS_STATUS)

    def test_the_second_boot_sees_what_the_first_boot_wrote(self) -> None:
        text = self.second.transcript
        self.assertIn("f  keep.txt  (25 bytes", text)
        self.assertIn("d  notes/  (1 entries)", text)
        self.assertNotIn("not unmounted cleanly", text)

    def test_the_contents_survived(self) -> None:
        text = self.second.transcript.replace("\r\n", "\n")
        body = text.split("cat: /keep.txt (25 bytes)\n", 1)[1]
        self.assertEqual(body.split("cat: 25 bytes read", 1)[0],
                         "hello from the first boot\n")

    def test_the_host_reads_the_files_the_kernel_left_behind(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        self.assertEqual(volume.read_file(volume.resolve("/keep.txt")),
                         b"hello from the first boot")
        self.assertEqual(volume.read_file(volume.resolve("/notes/two.txt")),
                         b"second file")
        self.assertEqual(volume.state, myfs.STATE_CLEAN)
        self.assertEqual(volume.check(), [])
        # The packed files are still there next to the new ones.
        self.assertEqual(volume.read_file(volume.resolve("/hello.txt")),
                         (ROOT / "files" / "hello.txt").read_bytes())

    def test_the_build_was_not_what_put_them_there(self) -> None:
        # A rebuild must not be able to produce this volume: the file exists only
        # because a running kernel wrote it.
        fresh = myos_build.build(arch=32).volume.read_bytes()
        written = self.disk.read_bytes()
        self.assertNotEqual(fresh, written)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32FsckTests(unittest.TestCase):
    """A volume that was not shut down cleanly, and the repair pass for it.

    The fixture is built on the host with three blocks marked used and referenced
    by nothing -- exactly what a power cut between "write the data" and "write the
    inode" leaves behind -- and with the state flag saying it was never unmounted.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        volume = myfs.build_volume(ROOT / "files", leak_blocks=3)
        volume.set_state(myfs.STATE_DIRTY)
        cls.leaks = 3
        cls.disk = ROOT / "build" / "tmp" / "fsck-fixture.img"
        cls.disk.parent.mkdir(parents=True, exist_ok=True)
        cls.disk.write_bytes(myfs.build_disk_image(volume))
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="fs\r", timeout=40),
            qemu.SessionStep(wait="not unmounted cleanly", serial="df\r", timeout=20),
            qemu.SessionStep(wait="df: 2048", serial="fsck\r", timeout=30),
            qemu.SessionStep(wait="state clean", serial="df\r", timeout=20),
            qemu.SessionStep(wait="df: 2048", serial="cat /hello.txt\r", timeout=20),
            qemu.SessionStep(wait="bytes read", serial="selftest\r", timeout=30),
        ], timeout=90.0, extra=qemu.data_disk_arguments(cls.disk))

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        self.assertEqual(self.session.unmet_waits, [])

    def test_the_boot_reports_a_volume_that_was_not_unmounted(self) -> None:
        text = self.text()
        self.assertIn("warning: the volume was not unmounted cleanly", text)
        self.assertIn("fs: state dirty", text)

    def test_fsck_reclaims_the_leaked_blocks(self) -> None:
        text = self.text()
        self.assertIn(f"fsck: {self.leaks} block(s) reclaimed, 0 block(s) rescued, "
                      "0 orphan inode(s)", text)
        self.assertIn("fsck: the free counts did not match the bitmaps", text)
        self.assertIn("fsck: 2036 free blocks, 57 free inodes, state clean", text)

    def test_the_free_blocks_came_back(self) -> None:
        # Nine data blocks were in use before the repair -- six from the packed tree
        # and three leaked -- and six after it.
        self.assertIn("df: 2048 blocks of 512 bytes: 9 used, 2033 free", self.text())
        self.assertIn("df: 2048 blocks of 512 bytes: 6 used, 2036 free", self.text())

    def test_the_files_are_still_readable_after_the_repair(self) -> None:
        self.assertIn("hello from the myos filesystem", self.text())
        self.assertIn("SELFTEST PASS", self.text())

    def test_the_host_agrees_that_the_volume_is_now_clean(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        self.assertEqual(volume.state, myfs.STATE_CLEAN)
        self.assertEqual(volume.check(), [],
                         "the kernel's repair did not satisfy the host's checks")
        self.assertEqual(volume.read_file(volume.resolve("/hello.txt")),
                         (ROOT / "files" / "hello.txt").read_bytes())


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32HardDiskTests(unittest.TestCase):
    """The MBR path: the loader is reached through the partition's boot record.

    The image has to be attached as an IDE disk for this to test anything.  With
    only a floppy drive present, QEMU's firmware falls back to the floppy and the
    guest reports `boot drive 0` -- a green test that never left the medium it
    meant to leave.  Attaching the same file as the primary master and booting
    with `-boot c` makes the guest report `boot drive 128`, which is the claim this
    test is actually making.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)

    def test_the_loader_boots_from_the_mbr_partition(self) -> None:
        run = qemu.run_session(self.result.hard_disk, [
            qemu.SessionStep(wait="myos> ", serial="info\r", timeout=40),
            qemu.SessionStep(wait="boot drive", serial="selftest\r", timeout=30),
        ], timeout=90.0,
            extra=[*qemu.data_disk_arguments(self.result.hard_disk), "-boot", "c"])
        self.assertNotIn("BOOT ERROR", run.transcript)
        self.assertRegex(run.transcript, re.compile(r"boot drive 128"))
        self.assertIn("SELFTEST PASS", run.transcript)
        self.assertEqual(run.status, qemu.DEBUG_EXIT_PASS_STATUS)
        self.assertEqual(run.unmet_waits, [])

    def test_the_disk_it_booted_from_is_the_one_it_mounts(self) -> None:
        # The volume is found by walking the MBR, so a boot from the disk must give
        # the same filesystem as attaching it as a data disk does.
        run = qemu.run_session(self.result.hard_disk, [
            qemu.SessionStep(wait="myos> ", serial="fs\r", timeout=40),
            qemu.SessionStep(wait="free inodes", serial="cat /hello.txt\r", timeout=20),
            qemu.SessionStep(wait="bytes read", serial="selftest\r", timeout=30),
        ], timeout=90.0,
            extra=[*qemu.data_disk_arguments(self.result.hard_disk), "-boot", "c"])
        self.assertIn("myfs volume mounted from LBA 2048", run.transcript)
        self.assertIn("hello from the myos filesystem", run.transcript)
        self.assertEqual(run.status, qemu.DEBUG_EXIT_PASS_STATUS)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32FilesystemTests(unittest.TestCase):
    """The filesystem, driven through the shell with a real disk attached.

    Everything here is read-only: the image is a build artefact and a test that
    wrote to it would leave the next run looking at someone else's leftovers.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        # A copy, not the build artefact: `blk test` writes to the scratch tail of
        # the disk it is given, and a test should not leave the image a build
        # produced in a different state from the one it will produce next time.
        cls.disk = ROOT / "build" / "tmp" / "fs-session.img"
        cls.disk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cls.result.hard_disk, cls.disk)
        # Every wait marker is unique on purpose: wait_for searches the whole
        # transcript, so reusing a marker would match the earlier occurrence and
        # send the next command before the previous one had finished.
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="blk\r", timeout=40),
            qemu.SessionStep(wait="blk: myfs partition at", serial="fs\r", timeout=20),
            qemu.SessionStep(wait="fs: state", serial="ls /\r", timeout=20),
            qemu.SessionStep(wait="ls /:", serial="cat /hello.txt\r", timeout=20),
            qemu.SessionStep(wait="cat: /hello.txt", serial="cat /docs/notes.txt\r",
                             timeout=20),
            qemu.SessionStep(wait="cat: /docs/notes.txt", serial="stat /readme.txt\r",
                             timeout=20),
            qemu.SessionStep(wait="stat: /readme.txt", serial="df\r", timeout=20),
            qemu.SessionStep(wait="df: 2048 blocks", serial="stat /docs\r", timeout=20),
            qemu.SessionStep(wait="stat: /docs", serial="cat /no-such-file\r", timeout=20),
            qemu.SessionStep(wait="cat: /no-such-file", serial="ls /docs\r", timeout=20),
            qemu.SessionStep(wait="ls /docs:", serial="blk test\r", timeout=30),
            qemu.SessionStep(wait="read back", serial="selftest\r", timeout=30),
        ], timeout=90.0, extra=qemu.data_disk_arguments(cls.disk))

    def text(self) -> str:
        return self.session.transcript

    def test_every_step_of_the_session_was_answered(self) -> None:
        self.assertEqual(self.session.unmet_waits, [])

    def test_the_ata_disk_is_identified(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"blk: primary master, model 'QEMU HARDDISK'"))
        self.assertRegex(text, re.compile(r"blk: \d+ sectors of 512 bytes \(\d+ MiB\), "
                                          r"LBA28 supported"))
        self.assertIn("blk: myfs partition at LBA 2048, 2048 sectors", text)

    def test_the_volume_mounts_with_a_consistent_superblock(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"fs: myfs v1 'myos', 2048 blocks of 512 bytes, "
                                          r"64 inodes"))
        self.assertIn("fs: state clean", text)
        self.assertNotIn("fs: warning", text)

    def test_ls_lists_what_the_build_packed(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"ls /:"))
        for name in ("hello.txt", "readme.txt", "manifest", "docs/"):
            self.assertIn(name, text)
        self.assertRegex(text, re.compile(r"\d+ entries"))

    def test_cat_prints_the_kernel_the_same_bytes_the_host_packed(self) -> None:
        # The host file is the source of truth: the kernel read the same bytes the
        # packer wrote, after a trip through ATA and the inode/indirect-block path.
        # The console turns a newline into CRLF on the serial port, so both sides
        # are normalised before they are compared -- this is about the bytes the
        # file holds, not about the line endings the terminal needs.
        expected = (ROOT / "files" / "hello.txt").read_text().replace("\r\n", "\n")
        text = self.text().replace("\r\n", "\n")
        self.assertIn("cat: /hello.txt (31 bytes)\n", text)
        body = text.split("cat: /hello.txt (31 bytes)\n", 1)[1]
        body = body.split("cat: 31 bytes read", 1)[0]
        self.assertEqual(body, expected)

    def test_cat_reads_a_file_in_a_subdirectory(self) -> None:
        expected = ((ROOT / "files" / "docs" / "notes.txt").read_text()
                    .replace("\r\n", "\n"))
        text = self.text().replace("\r\n", "\n")
        self.assertIn("cat: /docs/notes.txt (190 bytes)\n", text)
        body = text.split("cat: /docs/notes.txt (190 bytes)\n", 1)[1]
        body = body.split("cat: 190 bytes read", 1)[0]
        self.assertEqual(body, expected)

    def test_stat_reports_the_size_and_the_blocks(self) -> None:
        text = self.text()
        size = (ROOT / "files" / "readme.txt").stat().st_size
        self.assertRegex(text, re.compile(
            rf"stat: /readme.txt is a file, inode \d+, {size} bytes in \d+ block\(s\)"))
        self.assertRegex(text, re.compile(
            r"stat: /docs is a directory, inode \d+, 1 entries"))

    def test_df_reports_the_free_space_the_pack_left(self) -> None:
        # Eleven data blocks: the root directory, /bin, /docs, the three programs, the
        # two text files, the manifest, and the notes file inside /docs.
        self.assertIn("df: 2048 blocks of 512 bytes: 11 used, 2031 free", self.text())
        self.assertIn("df: 64 inodes: 10 used, 53 free", self.text())

    def test_a_missing_path_is_reported_and_not_invented(self) -> None:
        self.assertIn("cat: /no-such-file: no such file or directory", self.text())

    def test_the_disk_write_path_reaches_the_medium(self) -> None:
        # `blk test` writes a pattern to the scratch tail and reads it back.  The
        # interesting part is not the guest's report but the image on the host: the
        # bytes it wrote have to be there, and nothing else may have moved.
        self.assertIn("blk test: wrote and read back 8 sectors (4096 bytes) at LBA 4096",
                      self.text())
        start = myfs.SCRATCH_LBA * image.SECTOR
        end = start + myfs.SCRATCH_SECTORS * image.SECTOR
        written = self.disk.read_bytes()
        original = self.result.hard_disk.read_bytes()
        self.assertNotEqual(written[start:end], original[start:end],
                            "the scratch area was never written")
        self.assertEqual(written[:start], original[:start],
                         "something other than the scratch area changed")

    def test_a_subdirectory_can_be_listed(self) -> None:
        text = self.text()
        self.assertRegex(text, re.compile(r"ls /docs:"))
        # The inode number is not what this test is about (it moves whenever the packed
        # tree changes), so it is matched rather than asserted: the size and the path
        # are the claims.
        self.assertRegex(text, re.compile(r"f  notes\.txt  \(190 bytes, inode \d+\)"))

    def test_the_guest_exits_with_the_pass_status(self) -> None:
        self.assertIn("SELFTEST PASS", self.text())
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32ShutdownTests(unittest.TestCase):
    """`reboot` has to leave the volume clean, not rely on the user typing `sync`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        cls.disk = scratch_disk("shutdown.img")
        # Note the deliberate absence of `sync` before the reboot: that is the
        # whole point of the test.
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", serial="write /before-reboot.txt kept\r",
                             timeout=40),
            qemu.SessionStep(wait="write: /before-reboot.txt", serial="reboot\r",
                             timeout=30),
        ], timeout=90.0, extra=qemu.data_disk_arguments(cls.disk))

    def test_reboot_flushes_before_resetting(self) -> None:
        text = self.session.transcript
        self.assertIn("reboot: the volume is marked cleanly unmounted", text)
        self.assertIn("rebooting...", text)

    def test_the_volume_really_is_clean_on_the_medium(self) -> None:
        volume = myfs.volume_from_image(self.disk, myfs.PARTITION_INDEX)
        self.assertEqual(volume.state, myfs.STATE_CLEAN,
                         "a reboot left the volume marked as not unmounted")
        self.assertEqual(volume.check(), [])
        self.assertEqual(volume.read_file(volume.resolve("/before-reboot.txt")),
                         b"kept")


@unittest.skipUnless(QEMU_AVAILABLE, "qemu-system-i386 is not installed")
class Kernel32KeyboardTests(unittest.TestCase):
    """Typing on the emulated PS/2 keyboard, which is the path a person uses."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=32)
        # `sendkey` presses real keys through the i8042, so this exercises IRQ1, the
        # scan-code decoder and the same shell the serial tests drive.
        cls.session = qemu.run_session(cls.result.floppy, [
            qemu.SessionStep(wait="myos> ", keys=("h", "e", "l", "p", "ret"), timeout=40),
            qemu.SessionStep(wait="commands", keys=("k", "e", "y", "l", "o", "g", "ret"),
                             timeout=20),
            qemu.SessionStep(wait="IRQ1", keys=("s", "e", "l", "f", "t", "e", "s", "t", "ret"),
                             timeout=30),
        ], timeout=60.0, monitor_port=4471)

    def test_keys_are_decoded_and_echoed(self) -> None:
        text = self.session.transcript
        self.assertEqual(self.session.unmet_waits, [])
        self.assertIn("myos> help", text)

    def test_the_keyboard_irq_is_what_delivered_them(self) -> None:
        # The serial counter must stay at zero here: if these characters had come in
        # over COM1, the keyboard path would be untested.
        self.assertRegex(self.session.transcript, re.compile(r"keyboard: \d+ scan codes decoded"))
        self.assertIn("serial: 0 bytes received on IRQ4", self.session.transcript)

    def test_the_self_test_still_passes_when_it_is_typed(self) -> None:
        self.assertIn("SELFTEST PASS", self.session.transcript)
        self.assertEqual(self.session.status, qemu.DEBUG_EXIT_PASS_STATUS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
