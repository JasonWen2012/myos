"""
Regression tests for the toolchain layer.

These pin the environment facts that cost real debugging time, so a change that
breaks them fails here instead of showing up as a mystifying boot failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cofllink, image, toolchain  # noqa: E402

# Scratch space lives inside the workspace rather than the system temp directory:
# a build/test sandbox may be limited to the workspace, and writing to %TEMP%
# then fails with EACCES for reasons that have nothing to do with the test.
SCRATCH_ROOT = ROOT / "build" / "test-scratch"


def make_scratch(name: str) -> Path:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_ROOT / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


class ToolchainDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tc = toolchain.discover()

    def test_nasm_is_found_and_required(self) -> None:
        self.assertTrue(self.tc.get("nasm").found, "nasm is required to build myos")
        self.assertTrue(self.tc.get("nasm").required)

    def test_report_mentions_every_tool(self) -> None:
        report = self.tc.report()
        for name in ("nasm", "g++", "qemu-system-i386"):
            self.assertIn(name, report)

    def test_missing_qemu_is_not_an_error(self) -> None:
        # The simulator backend means a missing QEMU must never fail the build.
        self.assertIsInstance(self.tc.qemu_available, bool)
        if not self.tc.qemu_available:
            self.assertIn("simulator", self.tc.report())


class NasmQuirksTests(unittest.TestCase):
    """The nasm behaviours that are surprising and therefore worth pinning."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tc = toolchain.discover()
        cls.tmp = make_scratch("nasm")

    def _assemble(self, source: str) -> subprocess.CompletedProcess:
        asm = self.tmp / "probe.asm"
        out = self.tmp / "probe.bin"
        asm.write_text(source, encoding="ascii")
        return subprocess.run(
            [str(self.tc.path("nasm")), "-f", "bin", "-o", str(out), str(asm)],
            capture_output=True, text=True,
        )

    def test_lowercase_directives_are_accepted(self) -> None:
        proc = self._assemble("[bits 16]\n[org 0x7c00]\n    jmp short $ \n"
                              "    times 510-($-$$) db 0\n    dw 0xAA55\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_uppercase_bits_directive_is_actually_accepted(self) -> None:
        # Worth pinning because the opposite is widely believed: nasm 3.02 accepts
        # `[BITS 16]` in this position.  The project still uses lowercase
        # everywhere for consistency, but a failure blamed on case here would be a
        # misdiagnosis.
        proc = self._assemble("[BITS 16]\nnop\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_win32_format_cannot_express_16_bit_relocations(self) -> None:
        # This is the constraint that shapes the whole 16-bit kernel: it must be a
        # single assembly unit, and every address is materialised into a 32-bit
        # register instead of a 16-bit immediate.
        asm = self.tmp / "rel16.asm"
        obj = self.tmp / "rel16.o"
        asm.write_text('[bits 16]\nsection .text\nmsg: db 0\nmov si, msg\n',
                       encoding="ascii")
        proc = subprocess.run(
            [str(self.tc.path("nasm")), "-f", "win32", "-o", str(obj), str(asm)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("relocation", proc.stderr)

    def test_flat_binary_format_handles_16_bit_relocations_fine(self) -> None:
        # The limitation belongs to COFF, not to nasm or to 16-bit code: the flat
        # binary format resolves these itself, which is why the boot sector and
        # stage2 use it.
        asm = self.tmp / "flat16.asm"
        obj = self.tmp / "flat16.bin"
        asm.write_text('[bits 16]\n[org 0x7c00]\nmsg: db 0\nmov si, msg\n',
                       encoding="ascii")
        proc = subprocess.run(
            [str(self.tc.path("nasm")), "-f", "bin", "-o", str(obj), str(asm)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_mov_to_32_bit_register_is_accepted(self) -> None:
        proc = self._assemble('[bits 16]\nsection .text\nmsg: db 0\n'
                              'mov edi, msg\n')
        self.assertEqual(proc.returncode, 0, proc.stderr)


class CoffLinkerParsingTests(unittest.TestCase):
    """The COFF parsing rules that produced silently wrong addresses."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tc = toolchain.discover()
        cls.tmp = make_scratch("coff")
        cls.obj = cls.tmp / "syms.o"
        asm = cls.tmp / "syms.asm"
        asm.write_text(
            "[bits 16]\n"
            "section .text\n"
            "global kmain\n"
            "kmain:\n"
            "    mov edi, first_data\n"
            "    mov esi, message\n"
            "    ret\n"
            "section .data\n"
            "    dd 0x11111111\n"          # keeps first_data away from .data+0
            "first_data:\n"
            "    dd 7\n"
            "message:\n"
            "    db 'hello', 0\n",
            encoding="ascii",
        )
        toolchain.assemble(asm, cls.obj, fmt="win32", tc=cls.tc)
        cls.parsed = cofllink.read_object(cls.obj)

    def test_sections_are_parsed(self) -> None:
        names = {s.name for s in self.parsed.sections}
        self.assertIn(".text", names)
        self.assertIn(".data", names)

    def test_relocations_resolve_by_physical_entry_index(self) -> None:
        # Relocations index the symbol table by physical entry number, which
        # includes auxiliary records.  Using the logical (aux-skipped) index made
        # every reference resolve to the wrong symbol -- `mov esi, console_data`
        # loaded kernel_main's address instead.
        for sec in self.parsed.sections:
            for (_off, entry_index, _type) in sec.relocations:
                symbol = self.parsed.symbol_at(entry_index)
                self.assertIsNotNone(
                    symbol,
                    f"relocation entry {entry_index} must map to a symbol",
                )

    def test_short_inline_symbol_names_are_decoded(self) -> None:
        names = {s.name for s in self.parsed.symbols}
        self.assertIn(".file", names)
        self.assertIn(".text", names)
        self.assertIn("kmain", names)

    def test_link_resolves_data_symbols_to_the_data_section(self) -> None:
        # console_data-style references must land in .data, not in .text: getting
        # this wrong added the code section's address and every data pointer was
        # off by the size of .data, which made the console read its state from
        # inside the code.
        result = cofllink.link([self.obj], base=0x8000, entry="kmain",
                               layout=cofllink.loader_layout())
        start, size = result.section_bounds["data"]
        code_start, _ = result.section_bounds["code"]
        self.assertTrue(start <= result.symbols["first_data"] < start + size,
                        "first_data must be inside .data")
        self.assertTrue(start <= result.symbols["message"] < start + size,
                        "message must be inside .data")
        self.assertLess(start, code_start, ".data must be placed before .text")
        self.assertNotEqual(result.symbols["first_data"], code_start)

    def test_unresolved_symbol_is_reported_by_name(self) -> None:
        # The call has to be a 32-bit-register form, because COFF cannot carry the
        # 16-bit relocation a plain `call` would need.  That is the same
        # constraint that shapes the kernel.
        asm = self.tmp / "unresolved.asm"
        obj = self.tmp / "unresolved.o"
        asm.write_text("[bits 16]\nextern missing_function\nsection .text\n"
                       "global kmain\nkmain:\n    call dword missing_function\n"
                       "    ret\n",
                       encoding="ascii")
        proc = subprocess.run(
            [str(self.tc.path("nasm")), "-f", "win32", "-o", str(obj), str(asm)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            self.skipTest(f"nasm cannot express this relocation form: {proc.stderr.strip()}")
        with self.assertRaises(cofllink.LinkError) as ctx:
            cofllink.link([obj], base=0x8000, entry="kmain",
                          layout=cofllink.loader_layout())
        self.assertIn("missing_function", str(ctx.exception))

    def test_missing_entry_symbol_is_reported(self) -> None:
        with self.assertRaises(cofllink.LinkError) as ctx:
            cofllink.link([self.obj], base=0x8000, entry="no_such_entry",
                          layout=cofllink.loader_layout())
        self.assertIn("no_such_entry", str(ctx.exception))


class ImageHeaderTests(unittest.TestCase):
    """The self-checking kernel image header.

    Layout: magic(4) arch(1) version(1) flags(1) checksum(1) entry(dword) size(dword).
    Entry and size are dwords so that one format covers both the 16-bit kernel and
    the much larger 32-bit one; the checksum sits at byte 7 for the same reason.
    """

    def _header(self, entry: int = 0x20, size: int = 0x100, arch: int = 1) -> bytearray:
        image = bytearray(0x100)
        image[0:4] = b"MYOS"
        image[4] = arch
        image[5] = 0x01
        image[8:12] = entry.to_bytes(4, "little")
        image[12:16] = size.to_bytes(4, "little")
        return image

    def test_patch_makes_the_header_self_checking(self) -> None:
        patched = cofllink.patch_image_header(bytes(self._header()), 0x20)
        self.assertEqual(sum(patched[:16]) & 0xFF, 0)
        cofllink.verify_header(patched, 16, b"MYOS", arch=1)

    def test_entry_and_size_are_dwords(self) -> None:
        patched = cofllink.patch_image_header(bytes(self._header()), 0x20)
        self.assertEqual(int.from_bytes(patched[8:12], "little"), 0x20)
        self.assertEqual(int.from_bytes(patched[12:16], "little"), len(patched))

    def test_a_32_bit_image_larger_than_64_kib_is_expressible(self) -> None:
        # The word-sized fields this header started with could record neither an
        # entry offset nor a size above 0xFFFF, which a C++ kernel passes at once.
        image = self._header(entry=0x12345, size=0x30000, arch=2)
        image.extend(bytes(0x30000 - len(image)))
        patched = cofllink.patch_image_header(bytes(image), 0x12345)
        cofllink.verify_header(patched, 16, b"MYOS", arch=2)
        self.assertEqual(int.from_bytes(patched[8:12], "little"), 0x12345)

    def test_bad_magic_is_rejected(self) -> None:
        broken = bytearray(cofllink.patch_image_header(bytes(self._header()), 0x20))
        broken[0] = 0x00
        with self.assertRaises(cofllink.LinkError):
            cofllink.verify_header(bytes(broken), 16, b"MYOS", arch=1)

    def test_corrupted_checksum_is_rejected(self) -> None:
        broken = bytearray(cofllink.patch_image_header(bytes(self._header()), 0x20))
        broken[5] ^= 0xFF
        with self.assertRaises(cofllink.LinkError):
            cofllink.verify_header(bytes(broken), 16, b"MYOS", arch=1)

    def test_a_nonzero_flags_byte_is_rejected(self) -> None:
        broken = bytearray(cofllink.patch_image_header(bytes(self._header()), 0x20))
        broken[6] = 1
        broken[7] = (-sum(broken[:16])) & 0xFF     # keep the checksum valid
        with self.assertRaises(cofllink.LinkError):
            cofllink.verify_header(bytes(broken), 16, b"MYOS", arch=1)

    def test_an_arch_mismatch_is_rejected(self) -> None:
        patched = cofllink.patch_image_header(bytes(self._header(arch=2)), 0x20)
        with self.assertRaises(cofllink.LinkError):
            cofllink.verify_header(patched, 16, b"MYOS", arch=1)

    def test_entry_offset_outside_the_image_is_rejected(self) -> None:
        with self.assertRaises(cofllink.LinkError):
            cofllink.patch_image_header(bytes(self._header()), 0x9999)


class LinkerLayoutTests(unittest.TestCase):
    """Rules about what a section contributes to the flat image."""

    def test_an_uninitialised_section_contributes_no_file_bytes(self) -> None:
        # An uninitialised section has SizeOfRawData = the bytes to reserve and
        # PointerToRawData = 0.  Slicing the object file from offset 0 for it copies
        # the COFF header into what should be zero-filled BSS: the kernel's static
        # counters started life holding 0x0005014C -- the i386 machine type 0x014C
        # followed by the section count 5.
        tc = toolchain.discover()
        tmp = make_scratch("bss")
        asm = tmp / "bss.asm"
        asm.write_text(
            "[bits 32]\n"
            "section .text\n"
            "global kmain\n"
            "kmain:\n"
            "    mov eax, reserved\n"
            "    ret\n"
            "section .bss\n"
            "global reserved\n"
            "reserved:\n"
            "    resb 64\n",
            encoding="ascii",
        )
        obj = tmp / "bss.o"
        toolchain.assemble(asm, obj, fmt="win32", tc=tc)

        parsed = cofllink.read_object(obj)
        bss = next(s for s in parsed.sections if s.name == ".bss")
        self.assertEqual(bss.raw, b"", ".bss must carry no file contents")
        self.assertEqual(bss.size, 64, ".bss must still reserve its size")

        link = cofllink.link([obj], base=0x100000, entry="kmain",
                             layout=cofllink.kernel_layout())
        start, size = link.section_bounds["bss"]
        self.assertEqual(size, 64)
        region = link.image[start - 0x100000:start - 0x100000 + size]
        self.assertEqual(region, b"\x00" * size)       # a COFF header would show up here

    def test_the_verbose_report_handles_a_discarded_region(self) -> None:
        # `python build.py kernel32 --verbose` crashed with KeyError: 'discard':
        # the report looked every region up in the bounds table, and a discarded
        # region deliberately has no bounds.  The flag is documented, so it gets a
        # test rather than a note in a comment.
        tc = toolchain.discover()
        tmp = make_scratch("verbose")
        asm = tmp / "verbose.asm"
        asm.write_text(
            "[bits 32]\n"
            "section .text\n"
            "global kmain\n"
            "kmain:\n"
            "    ret\n"
            "section .debug_info\n"
            "    db 1, 2, 3\n",
            encoding="ascii",
        )
        obj = tmp / "verbose.o"
        toolchain.assemble(asm, obj, fmt="win32", tc=tc)
        printed: list[str] = []
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            link = cofllink.link([obj], base=0x100000, entry="kmain",
                                 layout=cofllink.kernel_layout(), verbose=True)
        printed = captured.getvalue().splitlines()
        self.assertTrue(printed)
        self.assertIn("(discarded)", captured.getvalue())
        self.assertNotIn("discard", link.section_bounds)

    def test_section_symbols_from_several_objects_are_not_definitions(self) -> None:
        # Every object carries a `.text` section symbol.  Entering those in the
        # symbol table made a normal multi-object link fail with "symbol '.text' is
        # defined more than once with different addresses".
        tc = toolchain.discover()
        tmp = make_scratch("sections")
        objects = []
        for index in (1, 2):
            asm = tmp / f"part{index}.asm"
            asm.write_text(
                "[bits 32]\n"
                "section .text\n"
                f"global part{index}\n"
                f"part{index}:\n"
                "    ret\n",
                encoding="ascii",
            )
            obj = tmp / f"part{index}.o"
            toolchain.assemble(asm, obj, fmt="win32", tc=tc)
            objects.append(obj)
        main = tmp / "main.asm"
        main.write_text(
            "[bits 32]\n"
            "extern part1\n"
            "extern part2\n"
            "section .text\n"
            "global kmain\n"
            "kmain:\n"
            "    call part1\n"
            "    call part2\n"
            "    ret\n",
            encoding="ascii",
        )
        main_obj = tmp / "main.o"
        toolchain.assemble(main, main_obj, fmt="win32", tc=tc)

        link = cofllink.link([main_obj, *objects], base=0x100000, entry="kmain",
                             layout=cofllink.kernel_layout())
        self.assertNotIn(".text", link.symbols)
        self.assertIn("part1", link.symbols)
        self.assertIn("part2", link.symbols)

    def test_the_entry_may_carry_the_toolchains_underscore(self) -> None:
        # nasm's `global kmain` produces `kmain`; this target's g++ produces `_kmain`
        # for the same extern "C" name, and the entry lookup must accept either.
        tc = toolchain.discover()
        tmp = make_scratch("entry")
        asm = tmp / "entry.asm"
        asm.write_text(
            "[bits 32]\n"
            "section .text\n"
            "global _kmain\n"
            "_kmain:\n"
            "    ret\n",
            encoding="ascii",
        )
        obj = tmp / "entry.o"
        toolchain.assemble(asm, obj, fmt="win32", tc=tc)
        link = cofllink.link([obj], base=0x100000, entry="kmain",
                             layout=cofllink.kernel_layout())
        self.assertEqual(link.entry, link.symbols["_kmain"])


class ImagePackingTests(unittest.TestCase):
    @staticmethod
    def _valid_boot_sector() -> bytes:
        sector = bytearray(512)
        sector[510:512] = b"\x55\xaa"
        return bytes(sector)

    def test_boot_sector_validation(self) -> None:
        self.assertIsNone(image.validate_boot_sector(self._valid_boot_sector()))
        with self.assertRaises(image.ImageError):
            image.validate_boot_sector(bytes(480))

    def test_boot_sector_without_signature_is_rejected(self) -> None:
        with self.assertRaises(image.ImageError):
            image.validate_boot_sector(bytes(512))

    def test_floppy_geometry_is_1440k(self) -> None:
        geometry = image.floppy_geometry()
        self.assertEqual(geometry.size_bytes, 1440 * 1024)
        self.assertEqual(geometry.sectors_per_track, 18)
        self.assertEqual(geometry.heads, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
