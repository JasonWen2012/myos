"""
Tests for the interactive launcher's input path.

`run.py --interactive` feeds real terminal keystrokes into the emulated keyboard,
so it has one job that no other test covers: turning whatever the terminal hands
over into the keys the kernel's BIOS queue understands.  A mistake there is
invisible to every other test, because they call `type_text`/`press_key` directly.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build as myos_build  # noqa: E402
import run as myos_run  # noqa: E402
from emulator.machine import Machine  # noqa: E402


def booted_machine() -> Machine:
    myos_build.build(arch=16)
    machine = Machine()
    machine.load_image(myos_build.IMAGES_DIR / "myos16.img")
    machine.boot()
    machine.run_until_idle(max_steps=400_000, idle_polls=60_000)
    return machine


def queued_ascii(machine: Machine) -> list[int]:
    return [ascii_code for _scan, ascii_code in machine.int_key_queue]


class FeedTests(unittest.TestCase):
    def test_carriage_return_and_newline_both_mean_enter(self) -> None:
        # A raw terminal sends CR, but a pipe delivers LF, because Python
        # translates newlines while reading text.  Treating only CR as Enter fed
        # every character of a line except the one that ends it, so a scripted
        # session typed at the shell and never ran anything.
        for newline in ("\r", "\n"):
            with self.subTest(newline=repr(newline)):
                machine = Machine()
                self.assertTrue(myos_run._feed(machine, newline))
                self.assertIn(0x0D, queued_ascii(machine))

    def test_escape_and_ctrl_keys_ask_to_quit(self) -> None:
        for quit_key in ("\x1b", "\x03", "\x04"):
            with self.subTest(key=repr(quit_key)):
                self.assertFalse(myos_run._feed(Machine(), quit_key))

    def test_arrow_keys_become_named_keys(self) -> None:
        machine = Machine()
        self.assertTrue(myos_run._feed(machine, "\x1b[A"))
        self.assertTrue(machine.int_key_queue, "the escape sequence must be consumed")
        self.assertNotIn(0x1B, queued_ascii(machine),
                         "ESC must not arrive as a plain character")

    def test_backspace_and_tab_are_translated(self) -> None:
        machine = Machine()
        myos_run._feed(machine, "\x7f\t")
        delivered = queued_ascii(machine)
        self.assertIn(0x08, delivered)
        self.assertIn(0x09, delivered)


class InteractiveSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = booted_machine()

    def test_a_line_typed_through_feed_reaches_the_shell(self) -> None:
        # This is the whole path the launcher drives: feed a line, let the kernel
        # run, read the screen.  It failed silently while LF was dropped, and the
        # only symptom was a shell that echoed input and never answered it.
        self.assertTrue(myos_run._feed(self.machine, "echo hello world\r"))
        self.assertEqual(
            self.machine.run_until_idle(max_steps=400_000, idle_polls=60_000),
            "waiting for input",
        )
        self.assertIn("hello world", self.machine.screen_text())

    def test_uppercase_input_arrives_as_uppercase(self) -> None:
        myos_run._feed(self.machine, "ECHO Loud\r")
        self.machine.run_until_idle(max_steps=400_000, idle_polls=60_000)
        self.assertIn("Loud", self.machine.screen_text())


class BackendSelectionTests(unittest.TestCase):
    """Which execution engine an image gets, decided from the image itself.

    The in-tree emulator is 16-bit real mode only, so handing it a protected-mode
    image does not fail cleanly -- it executes the header as code.  The choice is
    therefore made from the kernel header on the disk, not from a flag the user can
    get wrong.
    """

    def test_the_architecture_byte_decides_the_backend(self) -> None:
        myos_build.build(arch=16)
        myos_build.build(arch=32)
        self.assertEqual(myos_run.image_architecture(myos_build.IMAGES_DIR / "myos16.img"), 1)
        self.assertEqual(myos_run.image_architecture(myos_build.IMAGES_DIR / "myos32.img"), 2)

    def test_something_that_is_not_a_myos_image_is_rejected(self) -> None:
        self.assertIsNone(myos_run.image_architecture(ROOT / "README.md"))

    def test_the_default_image_follows_the_architecture(self) -> None:
        self.assertEqual(myos_run.default_image(16).name, "myos16.img")
        self.assertEqual(myos_run.default_image(32).name, "myos32.img")

    def test_the_emulator_refuses_a_32_bit_image(self) -> None:
        myos_build.build(arch=32)
        code = myos_run.main(["--backend", "sim", "--no-build",
                              str(myos_build.IMAGES_DIR / "myos32.img")])
        self.assertEqual(code, 2, "asking the 16-bit emulator for a 32-bit kernel "
                                  "must be a clear refusal, not a crashed emulator")


if __name__ == "__main__":
    unittest.main(verbosity=2)
