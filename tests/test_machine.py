"""
Tests for the machine's hardware model: keyboard delivery, idle detection.

These sit between the CPU tests and the boot tests.  The CPU tests can show that
an opcode is right; only these show that a keystroke reaches a kernel the way the
hardware would deliver it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emulator.cpu16 import IF  # noqa: E402
from emulator.machine import INT_KEYBOARD, Machine  # noqa: E402


def tiny_guest() -> Machine:
    """A machine with no disk, running a four-instruction loop at 0:0x2000.

    `sti` turns interrupts on so IRQ1 can be delivered, and the loop after it is
    the instruction the interrupt has to come back to.
    """
    machine = Machine()
    code = bytes([
        0xFB,              # sti
        0xEB, 0xFE,        # .loop: jmp .loop
    ])
    machine.cpu.mem[0x2000:0x2000 + len(code)] = code
    machine.cpu.mem[0x3000:0x3002] = b"\x00\x00"     # room for the interrupt frame
    machine.cpu.set_cs_ip(0, 0x2000)
    machine.cpu.sregs[2] = 0                          # SS
    machine.cpu.regs[4] = 0x3000                      # SP
    machine.cpu.set_flag(IF, False)
    return machine


class KeyboardDeliveryTests(unittest.TestCase):
    def test_a_keystroke_with_no_handler_is_acknowledged_by_the_firmware(self) -> None:
        # The guest owns no INT 09h vector here.  On a real machine the firmware's
        # handler would run; in this emulator that handler is Python and writes no
        # stub into the guest IVT, so raising IRQ1 would jump the guest to IVT
        # entry 9 -- which is 0:0, i.e. straight into the interrupt table.  A
        # real-mode kernel that did `sti` without owning vector 9 crashed exactly
        # that way, so the machine models the firmware's work instead.
        machine = tiny_guest()
        machine.type_text("a")
        latched = len(machine.pending_keys)

        self.assertEqual(latched, 2, "a keystroke latches its make and break codes")
        for _ in range(20):
            machine.step()

        self.assertEqual(machine.firmware_irq_count, latched,
                         "each latched byte must be acknowledged exactly once")
        self.assertFalse(machine.pending_keys, "the latch must drain")
        self.assertEqual(machine.cpu.sregs[1], 0)
        self.assertLess(machine.cpu.ip16, 0x2100,
                        "the guest must stay in its own code, not run the IVT")
        self.assertEqual(int.from_bytes(machine.cpu.mem[0x24:0x26], "little"), 0)

    def test_a_guest_handler_is_interrupted_and_can_read_the_port(self) -> None:
        # A kernel that does own IRQ1 must be able to read the scancode from port
        # 0x60; that read is also what clears the latch, so without it the same
        # byte would raise the interrupt again on the very next instruction.
        machine = tiny_guest()
        stub = bytes([
            0xE4, 0x60,        # in al, 0x60
            0xA2, 0x00, 0x05,  # mov [0x0500], al
            0xCF,              # iret
        ])
        machine.cpu.mem[0x1000:0x1000 + len(stub)] = stub
        machine.cpu.mem[0x24:0x26] = (0x1000).to_bytes(2, "little")   # IVT[9] offset
        machine.cpu.mem[0x26:0x28] = (0x0000).to_bytes(2, "little")   # IVT[9] segment
        machine.press_key("h")                     # make code 0x23, break 0xA3

        for _ in range(20):
            machine.step()
            if machine.cpu.mem[0x500]:
                break

        self.assertEqual(machine.cpu.mem[0x500], 0x23,
                         "the guest handler must see the make code on port 0x60")
        self.assertEqual(machine.firmware_irq_count, 0,
                         "with a guest handler the firmware must not also take it")

        for _ in range(20):
            machine.step()

        self.assertEqual(machine.cpu.mem[0x500], 0xA3,
                         "the break code raises its own interrupt")
        self.assertFalse(machine.pending_keys)
        self.assertLess(machine.cpu.ip16, 0x2100, "the handler must return to the guest")


class InputIdleTests(unittest.TestCase):
    def test_typing_uppercase_sends_shift(self) -> None:
        machine = Machine()
        machine.type_text("Ab")
        delivered = [ascii_code for _scan, ascii_code in machine.int_key_queue]
        self.assertIn(ord("A"), delivered,
                      "an uppercase letter needs Shift, not the lowercase key")

    def test_idle_is_reported_while_bytes_are_still_latched(self) -> None:
        # A kernel that runs with interrupts off never acknowledges the 8042
        # latch, so idle detection has to key off the queue INT 16h serves.  If it
        # waited for the latch to drain instead, every test that types a line
        # would sit out its whole step budget instead of reporting a settled shell.
        machine = Machine()
        poll = bytes([
            0xB4, 0x00,        # .poll: mov ah, 0
            0xCD, 0x16,        # int 0x16
            0xEB, 0xFA,        # jmp .poll
        ])
        machine.cpu.mem[0x2000:0x2000 + len(poll)] = poll
        machine.cpu.set_cs_ip(0, 0x2000)
        machine.cpu.sregs[2] = 0
        machine.cpu.regs[4] = 0x3000
        machine.type_text("a")                     # latched, and never acknowledged
        machine.int_key_queue.clear()              # the guest already read it

        reason = machine.run_until_idle(max_steps=20_000, idle_polls=1_000)

        self.assertEqual(reason, "waiting for input")
        self.assertTrue(machine.pending_keys,
                        "interrupts are off, so the latch really is still full")


if __name__ == "__main__":
    unittest.main(verbosity=2)
