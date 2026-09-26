"""
End-to-end boot tests.

These are the tests that actually prove the operating system starts: build the
images, run them in the emulator, and assert on what appears on screen.  A unit
test can show that a decoder is right; only this can show that the boot sector,
the second stage, the linker and the kernel agree with each other.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build as myos_build  # noqa: E402
from emulator.machine import Machine  # noqa: E402


def boot_image(path: Path, max_steps: int = 400_000) -> Machine:
    """Boot an image and run until the machine settles.

    run_until_idle rather than run: a kernel that starts a shell never halts, it
    polls INT 16h waiting for a key, and that counts as settled.
    """
    machine = Machine()
    machine.load_image(path)
    machine.boot()
    machine.run_until_idle(max_steps=max_steps, idle_polls=60_000)
    return machine


class BuildOutputTests(unittest.TestCase):
    """The build's own invariants, checked on the real artifacts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = myos_build.build(arch=16)

    def test_boot_sector_is_exactly_one_sector(self) -> None:
        data = self.result.boot.read_bytes()
        self.assertEqual(len(data), 512)

    def test_boot_sector_has_the_signature(self) -> None:
        data = self.result.boot.read_bytes()
        self.assertEqual(data[510:512], b"\x55\xaa")

    def test_boot_sector_hands_off_with_a_far_jump(self) -> None:
        # `jmp seg:label` makes nasm resolve the offset from the output file start,
        # which produced a jump into the interrupt vector table.  The build checks
        # the emitted bytes, so this pins it.
        data = self.result.boot.read_bytes()
        self.assertIn(bytes.fromhex("ea00070000"), data,
                      "the handoff must be a far jump to 0000:0700")

    def test_second_stage_fits_its_reserved_sectors(self) -> None:
        self.assertLessEqual(self.result.stage2.stat().st_size,
                             myos_build.STAGE2_MAX_SECTORS * 512)

    def test_kernel_fits_the_loader_budget(self) -> None:
        self.assertLessEqual(self.result.kernel_bytes,
                             myos_build.MAX_KERNEL_SECTORS * 512)

    def test_kernel_image_header_is_self_checking(self) -> None:
        kernel = self.result.kernel.read_bytes()
        self.assertEqual(kernel[:4], b"MYOS")
        self.assertEqual(kernel[4], 1, "architecture byte must say 16-bit")
        self.assertEqual(kernel[6], 0, "the flags byte must be zero")
        self.assertEqual(sum(kernel[:16]) & 0xFF, 0, "header checksum must verify")
        self.assertEqual(int.from_bytes(kernel[12:16], "little"), len(kernel))
        entry = int.from_bytes(kernel[8:12], "little")
        self.assertGreaterEqual(entry, 16, "the entry cannot point into the header")
        self.assertLess(entry, len(kernel))

    def test_floppy_image_size(self) -> None:
        self.assertEqual(self.result.floppy.stat().st_size, 1440 * 1024)

    def test_hard_disk_image_has_an_active_partition(self) -> None:
        from tools import image
        data = self.result.hard_disk.read_bytes()
        self.assertEqual(data[510:512], b"\x55\xaa")
        entries = image.mbr_partition_entries(data)
        self.assertEqual(sum(1 for e in entries if e["active"]), 1,
                         "exactly one partition must be marked bootable")


class BootSequenceTests(unittest.TestCase):
    """The whole chain, verified through what the user would see."""

    @classmethod
    def setUpClass(cls) -> None:
        myos_build.build(arch=16)
        cls.floppy = myos_build.IMAGES_DIR / "myos16.img"
        cls.machine = boot_image(cls.floppy)
        cls.text = cls.machine.screen_text()

    def test_the_second_stage_drew_its_banner(self) -> None:
        # The banner means the boot sector loaded stage2 and stage2 drew to the
        # video buffer before touching the disk again.
        self.assertIn("myos", self.text.lower())

    def test_the_kernel_printed_its_banner(self) -> None:
        self.assertIn("myos 16-bit kernel", self.text)

    def test_the_kernel_reports_the_image_size_from_its_own_header(self) -> None:
        # The number comes out of the header the loader validated, so seeing the
        # right size proves loader and kernel agree about the image layout.
        expected = f"image {self.machine and myos_build.build(arch=16).kernel_bytes} bytes"
        self.assertIn(expected, self.text)

    def test_the_kernel_reports_startup(self) -> None:
        self.assertIn("kernel started successfully", self.text)

    def test_the_kernel_starts_the_shell(self) -> None:
        # The kernel no longer halts after printing: it hands off to the shell,
        # which is the thing the user actually interacts with.
        self.assertIn("Type `help` for a list of commands.", self.text)
        self.assertIn("myos>", self.text)

    def test_the_kernel_comes_to_rest_waiting_for_input(self) -> None:
        # Either it idled in HLT (interrupts off) or the shell is polling INT 16h
        # with nothing queued.  Both mean the machine is settled and not crashed;
        # an emulator fault would have raised instead.
        reason = self.machine.halt_reason or ""
        self.assertTrue(
            self.machine.cpu.halted or "waiting for input" in reason or "hlt" in reason,
            f"unexpected stop reason: {reason!r}",
        )

    def test_the_console_blanked_the_screen_to_the_default_attribute(self) -> None:
        # console_init fills the screen with spaces in ATTR_DEFAULT.  Reading the
        # whole buffer (not just the first row) is the point: the cursor may have
        # moved on and later writes may have used other attributes.
        attrs = {self.machine.cpu.mem[0xB8001 + i * 2] for i in range(80 * 25)}
        self.assertIn(0x07, attrs, "the default attribute must be present")
        self.assertNotIn(0x00, attrs,
                         "no cell may be left with attribute 0 (black on black)")


class ShellTests(unittest.TestCase):
    """Typing at the prompt, command by command.

    Everything here goes through the real keyboard path: the harness latches scan
    codes, the kernel reads them with INT 16h, echoes them and dispatches.  The
    regressions these tests exist for were all invisible to a build that only
    checked the banner, so they assert on the lines the user would see.
    """

    def type_line(self, machine: Machine, text: str) -> str:
        machine.type_text(text)
        machine.press_key("enter")
        reason = machine.run_until_idle(max_steps=400_000, idle_polls=60_000)
        self.assertEqual(
            reason, "waiting for input",
            f"the shell did not settle after {text!r}: {reason!r}",
        )
        return machine.screen_text()

    def boot(self) -> Machine:
        myos_build.build(arch=16)
        return boot_image(myos_build.IMAGES_DIR / "myos16.img")

    def test_a_multi_character_command_is_dispatched_whole(self) -> None:
        # The line editor holds the data-block address in EBX while it echoes each
        # key, so a console primitive that clobbered BL corrupted the line after
        # the first character.  `help` came back as `unknown command: hl`.
        machine = self.boot()
        text = self.type_line(machine, "help")
        self.assertNotIn("unknown command", text)
        self.assertIn("  echo <text>   print the text back", text)

    def test_every_command_in_the_table_reaches_its_own_handler(self) -> None:
        # Handler addresses are stored relative to the table base.  The dispatcher
        # once added the *matched entry* instead, which is the same address only
        # for the first entry: `help` worked and every later command jumped a few
        # bytes inside its own handler.  One distinguishing string per command
        # catches that class of error.
        cases = {
            "help": "commands",
            "echo hello world": "hello world",
            "info": "16-bit real mode",
            "mem": "KiB conventional",
            "ticks": "ticks ",
            "fact 5": "120",
            "keylog": "int 09h handler",
        }
        machine = self.boot()
        for command, expected in cases.items():
            with self.subTest(command=command):
                text = self.type_line(machine, command)
                self.assertNotIn("unknown command", text)
                self.assertIn(expected, text)

    def test_command_matching_ignores_case(self) -> None:
        machine = self.boot()
        self.assertNotIn("unknown command", self.type_line(machine, "ECHO Mixed Case"))
        self.assertIn("Mixed Case", machine.screen_text())

    def test_an_unknown_command_is_reported_not_ignored(self) -> None:
        machine = self.boot()
        text = self.type_line(machine, "bogus")
        self.assertIn("unknown command: bogus", text)

    def test_backspace_edits_the_line_before_dispatch(self) -> None:
        # Backspace has to fix the line *and* the screen: a console that only
        # stepped the cursor back left the deleted character visible, so the line
        # the user could see no longer matched the one being dispatched.
        machine = self.boot()
        machine.type_text("helX")
        machine.press_key("backspace")
        machine.type_text("p")
        machine.press_key("enter")
        self.assertEqual(
            machine.run_until_idle(max_steps=400_000, idle_polls=60_000),
            "waiting for input",
        )
        text = machine.screen_text()
        self.assertNotIn("unknown command", text)
        self.assertIn("commands", text)
        self.assertNotIn("X", text, "the deleted character must be rubbed out")

    def test_an_overlong_line_does_not_run_into_later_state(self) -> None:
        # The line buffer is fixed size and sits next to the command token buffer,
        # so an overflowing line would corrupt the token and the block after it.
        machine = self.boot()
        self.type_line(machine, "echo " + "x" * 200)
        # The shell must still be healthy afterwards.
        self.assertIn("commands", self.type_line(machine, "help"))

    def test_clear_blanks_the_screen_and_keeps_the_shell_alive(self) -> None:
        machine = self.boot()
        text = self.type_line(machine, "clear")
        self.assertNotIn("myos 16-bit kernel", text,
                         "clear must wipe the earlier output")
        self.assertIn("commands", self.type_line(machine, "help"))

    def test_a_blank_line_prints_a_fresh_prompt(self) -> None:
        machine = self.boot()
        self.type_line(machine, "")
        self.assertIn("myos>", machine.screen_text())

    def test_the_kernel_keeps_interrupts_disabled(self) -> None:
        # The kernel installs no interrupt vector of its own, so enabling
        # interrupts lets the first keystroke jump through a 0:0 IVT entry into
        # the interrupt table and the BIOS data area.  Turning IF on here has to
        # go together with owning vector 09h and the timer.
        from emulator.cpu16 import IF

        machine = self.boot()
        self.assertFalse(machine.cpu.flag(IF),
                         "a kernel with no interrupt vectors must keep IF clear")
        # And the keyboard still works with interrupts off, because INT 16h reads
        # the firmware's queue directly.
        self.assertIn("commands", self.type_line(machine, "help"))


class HardDiskBootTests(unittest.TestCase):
    def test_the_same_loader_boots_from_a_partitioned_image(self) -> None:
        myos_build.build(arch=16)
        machine = boot_image(myos_build.IMAGES_DIR / "myos16-hd.img")
        self.assertIn("myos 16-bit kernel", machine.screen_text(),
                      "the loader must find the active partition and boot it")


class ErrorPathTests(unittest.TestCase):
    """A damaged image must not be executed as if it were fine.

    These also cover the loader's *diagnostics*, which are not decoration: with no
    `org` in either stage and a string instruction that stepped two bytes at a
    time, the loader could not print a single legible word, so every way it could
    fail looked exactly like a hang.
    """

    def build_parts(self) -> tuple[bytes, bytes, bytes]:
        myos_build.build(arch=16)
        stage1 = (myos_build.BUILD_DIR / "boot16.bin").read_bytes()
        stage2 = (myos_build.BUILD_DIR / "stage2.bin").read_bytes()
        kernel = (myos_build.BUILD_DIR / "kernel16.bin").read_bytes()
        return stage1, stage2, kernel

    def test_a_kernel_that_cannot_be_read_reports_the_bios_error(self) -> None:
        # A medium with room for stage2 but nothing at the kernel's LBA.  The
        # loader asks for the kernel, the read walks off the end of the medium,
        # and both the code and the CHS it tried have to reach the screen.  The
        # code 09 here is the loader's own "past the end of the medium" marker,
        # which it stores after the retries and the controller reset are spent.
        import re

        from emulator.bios import DiskGeometry

        stage1, stage2, _kernel = self.build_parts()
        geometry = DiskGeometry(cylinders=1, heads=2, sectors_per_track=18,
                                data=bytearray(512 * 36))
        image = bytearray(512 * 36)
        image[0:512] = stage1
        image[512:512 + len(stage2)] = stage2

        machine = Machine()
        machine.load_image(bytes(image), geometry=geometry)
        machine.boot()
        machine.run(max_steps=400_000)
        text = machine.screen_text()

        self.assertRegex(
            text, re.compile(r"BOOT ERROR: disk read failed, code [0-9A-F]{2} "
                             r"at CHS [0-9A-F]{6}"))
        self.assertTrue(machine.cpu.halted, "a failed load must stop, not wander")

    def test_the_first_stage_reports_a_stage2_read_failure(self) -> None:
        # A medium too small to hold stage2 at all.  This is the first stage's own
        # error path, and the only case where its message survives: once stage2
        # runs, it paints the whole screen.
        import re

        from emulator.bios import DiskGeometry

        stage1, _stage2, _kernel = self.build_parts()
        geometry = DiskGeometry(cylinders=1, heads=1, sectors_per_track=4,
                                data=bytearray(512 * 4))
        image = bytearray(512 * 4)
        image[0:512] = stage1

        machine = Machine()
        machine.load_image(bytes(image), geometry=geometry)
        machine.boot()
        machine.run(max_steps=400_000)
        text = machine.screen_text()

        self.assertIn("myos boot", text)
        self.assertRegex(text, re.compile(r"BOOT ERROR: cannot load stage2, code [0-9A-F]{2}"))
        self.assertIn("System halted.", text)
        self.assertNotIn("loading kernel", text, "stage2 must never have run")

    def test_a_kernel_with_a_broken_header_is_reported(self) -> None:
        from tools import image

        stage1, stage2, kernel = self.build_parts()
        kernel = bytearray(kernel)
        kernel[0] = 0x00                        # break the "MYOS" magic

        img = image.build_floppy_image(stage1)
        img[512:512 + len(stage2)] = stage2
        off = myos_build.KERNEL_LBA * 512
        img[off:off + len(kernel)] = kernel

        machine = Machine()
        machine.load_image(bytes(img))
        machine.boot()
        machine.run(max_steps=400_000)
        text = machine.screen_text()

        # The message names the architecture byte it read, and that byte is
        # printed by the same hex routine that used to corrupt its own value.
        self.assertIn("BOOT ERROR: bad kernel image, arch 01", text)
        self.assertIn("System halted.", text)
        self.assertNotIn("myos 16-bit kernel", text,
                         "a corrupt image must never reach the kernel")


class BootDiagnosticsTests(unittest.TestCase):
    """What the two loader stages put on the screen before the kernel runs."""

    def test_both_stages_print_legibly_before_the_kernel_starts(self) -> None:
        # The kernel blanks the screen as its first act, so the loader's own
        # output can only be seen by stopping at the handover -- which is the
        # point of it: when the kernel never starts, that text is the only
        # evidence there is, and for a long time it was invisible.
        myos_build.build(arch=16)
        kernel = (myos_build.BUILD_DIR / "kernel16.bin").read_bytes()
        entry = myos_build.KERNEL_LOAD_LIN + int.from_bytes(kernel[8:12], "little")

        machine = Machine()
        machine.load_image(myos_build.IMAGES_DIR / "myos16.img")
        machine.boot()
        for _ in range(400_000):
            if machine.cpu.sregs[1] == 0 and machine.cpu.ip16 == entry:
                break
            machine.step()
        else:
            self.fail("the loader never handed control to the kernel")

        text = machine.screen_text()
        # The first stage's "myos boot" is deliberately absent here: stage2 paints
        # the whole screen, so that message is only evidence when stage2 fails to
        # load (see test_the_first_stage_reports_a_stage2_read_failure).
        self.assertIn("myos", text, "stage2 must draw its banner")
        self.assertIn("a small operating system", text)
        # The status line starts as "loading kernel..." and is replaced in place on
        # success, so at the handover it must read as the success pair with no
        # leftover tail from the longer string.
        self.assertNotIn("loading kernel", text)
        self.assertIn("kernel loaded", text)
        self.assertIn("starting kernel...", text)
        self.assertNotIn("loadedl", text, "the status line must be fully overwritten")

    def test_the_boot_chain_leaves_the_interrupt_vector_table_alone(self) -> None:
        # Without the org directives both stages assembled absolute addresses from
        # the start of the file, so stage2's variables lived at linear 0x303-0x318
        # -- inside the IVT -- and it read its strings from there as well.  Booting
        # still worked, because the same wrong address was used to write and to
        # read, which is exactly what made it so hard to see.
        myos_build.build(arch=16)
        machine = boot_image(myos_build.IMAGES_DIR / "myos16.img")
        used = [v for v in range(256)
                if machine.cpu.rw(v * 4) or machine.cpu.rw(v * 4 + 2)]
        self.assertEqual(used, [], "the loader must not write interrupt vectors")


if __name__ == "__main__":
    unittest.main(verbosity=2)
