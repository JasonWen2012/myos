"""
Regression tests for the 16-bit interpreter.

Each test assembles a handful of instruction bytes directly and checks the CPU
state afterwards, so a change to decoding or flags fails here rather than in a
mystifying boot failure.  The cases that are called out were all real bugs found
while bringing the kernel up.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emulator.cpu16 import (  # noqa: E402
    CPU, CpuHalt, NotImplementedOpcode, EmulatorError,
    CF, ZF, SF, OF, AF, PF,
)


def run(bytes_: bytes, steps: int = 1, cs: int = 0, ip: int = 0) -> CPU:
    """Load a code snippet at 0:0 and execute exactly `steps` instructions.

    Tests that mean to run a setup instruction *and* the instruction under test
    pass steps=2.  Guessing the count from the snippet length does not work: a
    one-byte instruction (xor ax,ax, hlt, ret) would then be executed several
    times over, which produced failures that had nothing to do with the CPU.
    """
    cpu = CPU()
    cpu.mem[0:len(bytes_)] = bytes_
    cpu.reset(cs, ip)
    for _ in range(steps):
        cpu.step()
        if cpu.halted:
            break
    return cpu


class ByteRegisterEncodingTests(unittest.TestCase):
    """AL/CL/DL/BL and AH/CH/DH/BH versus AX/CX/DX/BX/SP/BP/SI/DI."""

    def test_high_byte_registers_are_not_the_stack_pointer(self) -> None:
        # `mov ah, 0x0E` (B4 0E) must write the high byte of AX.  Treating r/m
        # 4..7 as SP/BP/SI/DI instead of AH/CH/DH/BH put the value into SP and
        # left AH zero, so every BIOS teletype call printed nothing.
        cpu = run(bytes.fromhex("b40e"))
        self.assertEqual(cpu.get_reg(4, 8), 0x0E, "AH must hold 0x0E")
        self.assertEqual(cpu.regs[0], 0x0E00, "AH is the high byte of AX")
        self.assertEqual(cpu.regs[4], 0, "SP must be untouched")

    def test_ah_does_not_alias_sp(self) -> None:
        cpu = CPU()
        cpu.regs[4] = 0x1234                    # SP
        cpu.set_reg(4, 8, 0xFF)                 # AH, not SP's low byte
        self.assertEqual(cpu.regs[4], 0x1234, "SP must be unchanged")
        self.assertEqual(cpu.get_reg(4, 8), 0xFF)

    def test_al_is_the_low_byte_of_ax(self) -> None:
        cpu = CPU()
        cpu.regs[0] = 0xABCD
        self.assertEqual(cpu.get_reg(0, 8), 0xCD)
        cpu.set_reg(0, 8, 0x11)
        self.assertEqual(cpu.regs[0], 0xAB11)

    def test_ch_dh_bh_alias_words_1_2_3(self) -> None:
        # The high-byte encodings 4..7 are AH CH DH BH, i.e. the high bytes of
        # word registers 0..3.  (Getting this wrong put the value into SP.)
        for high_index, word in ((4, 0), (5, 1), (6, 2), (7, 3)):
            cpu = CPU()
            cpu.regs[word] = 0x0000
            cpu.set_reg(high_index, 8, 0x5A)
            self.assertEqual(cpu.regs[word], 0x5A00,
                             f"register {high_index} must alias word {word}'s high byte")


class FarTransferTests(unittest.TestCase):
    """Far jumps and returns, including the CS:IP update ordering trap."""

    def test_far_jump_16_bit_offset(self) -> None:
        # EA <off16> <seg16>.  Reading a 32-bit offset here consumed two bytes of
        # the segment and jumped into the interrupt vector table.
        cpu = run(bytes.fromhex("ea007e0000"))
        self.assertEqual((cpu.sregs[1], cpu.ip16), (0x0000, 0x7E00))

    def test_far_jump_32_bit_offset_with_operand_size_prefix(self) -> None:
        # 66 EA <off32> <seg16>
        cpu = run(bytes.fromhex("66ea0000010000"))
        self.assertEqual((cpu.sregs[1], cpu.ip16), (0x0000, 0x0000))

    def test_far_jump_sets_the_fetch_pointer(self) -> None:
        # _csip must follow CS: a far jump whose _csip still pointed into the old
        # segment fetched from the wrong place while IP looked correct.
        cpu = CPU()
        cpu.mem[0:5] = bytes.fromhex("ea007e0000")
        cpu.mem[0x7E00] = 0xF4                  # hlt
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(cpu._csip, 0x7E00)
        cpu.step()
        self.assertTrue(cpu.halted, "execution must continue at the jumped-to address")

    def test_far_return_pops_ip_then_cs(self) -> None:
        # CB: pop IP, then pop CS.
        cpu = CPU()
        cpu.mem[0:3] = bytes.fromhex("cb") + b"\x00\x00"
        cpu.reset(0, 0)
        cpu.push(0x0000, 16)                    # segment
        cpu.push(0x1234, 16)                    # offset
        cpu.step()
        self.assertEqual((cpu.sregs[1], cpu.ip16), (0x0000, 0x1234))
        self.assertEqual(cpu._csip, 0x1234)

    def test_iret_restores_cs_ip_and_flags(self) -> None:
        cpu = CPU()
        cpu.reset(0, 0)
        cpu.push(0x0202, 16)                    # flags
        cpu.push(0x0040, 16)                    # CS
        cpu.push(0x0100, 16)                    # IP
        cpu.do_iret()
        self.assertEqual((cpu.sregs[1], cpu.ip16), (0x0040, 0x0100))
        self.assertEqual(cpu._csip, 0x0400 + 0x0100)


class ArithmeticTests(unittest.TestCase):
    def test_add_sets_carry_and_zero(self) -> None:
        # mov ax,0xFFFF then add ax,1 -- two instructions.
        cpu = run(bytes.fromhex("b8ffff83c001"), steps=2)
        self.assertEqual(cpu.regs[0], 0x0000)
        self.assertEqual(cpu.flag(CF), 1)
        self.assertEqual(cpu.flag(ZF), 1)

    def test_sub_sets_sign_and_overflow(self) -> None:
        cpu = run(bytes.fromhex("b80080"))          # mov ax,0x8000
        cpu.mem[cpu._csip] = 0x2D                   # sub ax, imm16
        cpu.mem[cpu._csip + 1] = 0x01
        cpu.mem[cpu._csip + 2] = 0x00
        cpu.step()
        self.assertEqual(cpu.regs[0], 0x7FFF)
        self.assertEqual(cpu.flag(OF), 1)
        self.assertEqual(cpu.flag(SF), 0)

    def test_group1_immediate_selects_operation_from_reg_field(self) -> None:
        # 0x80 is one opcode for add/or/adc/sbb/and/sub/xor/cmp; the operation
        # comes from the ModRM reg field.  Binding a handler per reg value and
        # asserting a match rejected `cmp` (reg 7), which the kernel emits for
        # every loop bound.
        cpu = run(bytes.fromhex("b805003c07"), steps=2)     # mov ax,5; cmp al,7
        self.assertEqual(cpu.regs[0], 0x0005, "cmp must not write its result")
        self.assertEqual(cpu.flag(CF), 1, "5 < 7 sets carry")
        self.assertEqual(cpu.flag(SF), 1)

    def test_inc_preserves_carry(self) -> None:
        cpu = CPU()
        cpu.mem[0:4] = bytes.fromhex("f9b8ffff")     # stc; mov ax,0xFFFF
        cpu.reset(0, 0)
        cpu.step()                                    # stc
        cpu.step()                                    # mov ax,0xFFFF
        cpu.mem[cpu._csip] = 0x40                     # inc ax
        cpu.step()
        self.assertEqual(cpu.regs[0], 0x0000)
        self.assertEqual(cpu.flag(CF), 1, "inc must not touch the carry flag")
        self.assertEqual(cpu.flag(ZF), 1)

    def test_xor_same_register_zeroes_it(self) -> None:
        cpu = run(bytes.fromhex("31c0"), steps=1)
        self.assertEqual(cpu.regs[0], 0)
        self.assertEqual(cpu.flag(ZF), 1)

    def test_mul_unsigned_16_bit(self) -> None:
        cpu = CPU()
        cpu.regs[0] = 0x1000
        cpu.regs[3] = 0x0010
        cpu.mem[0:2] = bytes.fromhex("f7e3")          # mul bx
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(cpu.regs[0], 0x0000)
        self.assertEqual(cpu.regs[2], 0x0001)


class StringOperationTests(unittest.TestCase):
    def test_rep_stosb_stores_bytes(self) -> None:
        # The "b" suffix is not decoration: AA stores AL and advances DI by one,
        # whatever the default operand size is.  An earlier version of the
        # emulator used the 16-bit default for every string opcode, so this
        # stored AX instead -- and more visibly, made `lodsb` skip every other
        # character of every string the bootloader printed.
        cpu = CPU()
        cpu.regs[0] = 0x0042                             # AX = 0x0042, AL = 'B'
        cpu.regs[1] = 3                                  # CX = 3
        cpu.regs[7] = 0x100                              # DI
        cpu.mem[0:2] = bytes.fromhex("f3aa")             # rep stosb
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(bytes(cpu.mem[0x100:0x106]), b"BBB\x00\x00\x00")
        self.assertEqual(cpu.regs[1], 0, "CX must reach zero")
        self.assertEqual(cpu.regs[7], 0x103, "DI advances one byte per store")

    def test_rep_stosw_stores_words(self) -> None:
        # AB is the word form: two bytes per iteration, DI advancing by two.
        cpu = CPU()
        cpu.regs[0] = 0x0041
        cpu.regs[1] = 2
        cpu.regs[7] = 0x200
        cpu.mem[0:2] = bytes.fromhex("f3ab")             # rep stosw
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(bytes(cpu.mem[0x200:0x204]), b"A\x00A\x00")
        self.assertEqual(cpu.regs[7], 0x204)

    def test_lodsb_reads_one_byte_and_advances_si_by_one(self) -> None:
        # The classic `lodsb; test al, al; jz done; ...` string loop depends on
        # exactly this.  Stepping SI by two printed every other character.
        cpu = CPU()
        cpu.mem[0x200:0x204] = b"abc\x00"
        cpu.regs[6] = 0x200                              # SI
        cpu.mem[0:1] = bytes.fromhex("ac")               # lodsb
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(cpu.regs[0] & 0xFF, ord("a"))
        self.assertEqual(cpu.regs[6], 0x201)

    def test_lodsb_survives_the_operand_size_prefix(self) -> None:
        # 0x66 is ignored by the byte form rather than promoting it to a word.
        cpu = CPU()
        cpu.mem[0x200:0x204] = b"abc\x00"
        cpu.regs[6] = 0x200
        cpu.mem[0:2] = bytes.fromhex("66ac")             # lodsb with 0x66
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(cpu.regs[0] & 0xFF, ord("a"))
        self.assertEqual(cpu.regs[6], 0x201)

    def test_rep_movsb_copies_bytes_and_honours_the_direction_flag(self) -> None:
        cpu = CPU()
        cpu.mem[0x200:0x203] = b"abc"
        cpu.regs[1] = 3
        cpu.regs[6] = 0x202                              # SI
        cpu.regs[7] = 0x302                              # DI
        cpu.mem[0:3] = bytes.fromhex("fdf3a4")           # std; rep movsb
        cpu.reset(0, 0)
        cpu.step()                                       # std
        cpu.step()                                       # rep movsb
        self.assertEqual(bytes(cpu.mem[0x300:0x303]), b"abc")
        self.assertEqual(cpu.regs[6], 0x1FF, "SI walks down, one byte per step")
        self.assertEqual(cpu.regs[7], 0x2FF, "DI walks down, one byte per step")

    def test_rep_movsb_moves_exactly_cx_bytes(self) -> None:
        # A word-stepping bug copied twice the requested length, which is how the
        # boot sector's relocation ran 512 bytes past its own sector.
        cpu = CPU()
        cpu.mem[0x200:0x210] = bytes(range(16))
        cpu.regs[1] = 4
        cpu.regs[6] = 0x200
        cpu.regs[7] = 0x300
        cpu.mem[0:2] = bytes.fromhex("f3a4")             # rep movsb
        cpu.reset(0, 0)
        cpu.step()
        self.assertEqual(bytes(cpu.mem[0x300:0x304]), bytes(range(4)))
        self.assertEqual(bytes(cpu.mem[0x304:0x310]), b"\x00" * 12,
                         "nothing beyond CX bytes may be written")
        self.assertEqual(cpu.regs[6], 0x204)
        self.assertEqual(cpu.regs[7], 0x304)


class HaltAndFaultTests(unittest.TestCase):
    def test_hlt_halts(self) -> None:
        cpu = run(bytes.fromhex("f4"))
        self.assertTrue(cpu.halted)

    def test_unknown_opcode_raises_with_location(self) -> None:
        with self.assertRaises(NotImplementedOpcode) as ctx:
            run(bytes.fromhex("0f00"))
        self.assertIn("0F 00", str(ctx.exception))

    def test_divide_by_zero_raises(self) -> None:
        cpu = CPU()
        cpu.regs[0] = 10
        cpu.regs[3] = 0
        cpu.mem[0:2] = bytes.fromhex("f7f3")            # div bx
        cpu.reset(0, 0)
        from emulator.cpu16 import DivideError
        with self.assertRaises(DivideError):
            cpu.step()

    def test_memory_bounds_are_checked(self) -> None:
        cpu = CPU()
        cpu.reset(0, 0)
        with self.assertRaises(EmulatorError):
            cpu.rb(0x100000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
