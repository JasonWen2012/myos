"""
BIOS services for the myos 16-bit emulator.

Implements the interrupt handlers a real-mode boot sector relies on, plus the
8042 keyboard controller behind them:

  INT 10h  video    -- set mode, cursor shape/position, scroll, write char,
                       teletype output, get mode, write string (AH=13h)
  INT 11h  equipment word          INT 12h  memory size
  INT 13h  disk     -- reset, CHS read/write, get drive parameters, EDD probe
  INT 14h  serial   -- enough to let a guest emit bytes to the host log
  INT 15h  misc     -- A20 gate, extended memory size
  INT 16h  keyboard -- read/peek/flags driven by the injected scancode queue
  INT 1Ah  time     -- tick counter
  INT 18h/19h       -- no boot device / reboot (raised as ShutdownRequest)

Failures are reported the way the real BIOS reports them (CF set plus an error
code in AH) rather than raised as exceptions, because the guest is expected to
have a failure path -- and for the boot sector that failure path is itself
under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .cpu16 import CPU, ShutdownRequest, CF, ZF

VIDEO_BASE = 0xB8000
VIDEO_COLS = 80
VIDEO_ROWS = 25
VIDEO_SIZE = VIDEO_COLS * VIDEO_ROWS * 2

REG_AX, REG_CX, REG_DX, REG_BX, REG_SP, REG_BP, REG_SI, REG_DI = range(8)
SEG_ES, SEG_CS, SEG_SS, SEG_DS = 0, 1, 2, 3

BDA_SEG = 0x0040                  # BIOS data area lives at 0040:xxxx, so the
BDA_BASE = BDA_SEG << 4           # linear base is 0x400 and every offset below
BDA_EQUIPMENT = 0x10              # is an offset within that segment.
BDA_MEMORY_KB = 0x13
BDA_KBD_FLAGS = 0x17
BDA_TICKS = 0x6C
BDA_VIDEO_MODE = 0x49
BDA_VIDEO_COLS = 0x4A
BDA_VIDEO_PAGE_OFF = 0x4E
BDA_CURSOR_POS = 0x50
BDA_CURSOR_SHAPE = 0x60
BDA_BOOT_DRIVE = 0x75

DEFAULT_EQUIPMENT = 0x0021        # one floppy + 80x25 colour card
DEFAULT_MEMORY_KB = 640


@dataclass
class DiskGeometry:
    """CHS geometry plus the backing bytes for one emulated drive."""

    cylinders: int
    heads: int
    sectors_per_track: int
    data: bytearray = field(default_factory=bytearray)

    @property
    def total_sectors(self) -> int:
        return self.cylinders * self.heads * self.sectors_per_track

    def sector_offset(self, c: int, h: int, s: int) -> int:
        """Byte offset of a 1-based CHS sector."""
        lba = (c * self.heads + h) * self.sectors_per_track + (s - 1)
        return lba * 512


def floppy_1440(data: bytes | bytearray | None = None) -> DiskGeometry:
    """3.5" 1.44 MiB floppy: 80 cylinders, 2 heads, 18 sectors per track."""
    geo = DiskGeometry(80, 2, 18, bytearray(1440 * 1024))
    if data:
        geo.data[0:len(data)] = bytes(data)
    return geo


def hard_disk(cylinders: int = 16, heads: int = 16, spt: int = 63,
              data: bytes | bytearray | None = None) -> DiskGeometry:
    geo = DiskGeometry(cylinders, heads, spt, bytearray(cylinders * heads * spt * 512))
    if data:
        geo.data[0:len(data)] = bytes(data)
    return geo


class BIOS:
    """Routes guest INT instructions to the emulated BIOS services."""

    def __init__(self, machine) -> None:
        self.machine = machine
        self.cpu: CPU = machine.cpu
        self.drives: dict[int, DiskGeometry] = {}
        self.video_mode = 3
        self.crt_mode = 0x29             # 80x25 colour, 8x16 glyphs
        self.serial_out: list[str] = []
        self.a20_enabled = False
        self.teletype_count = 0
        self._install()

    # ------------------------------------------------------------------ setup

    def _install(self) -> None:
        self.cpu.int_hooks.update({
            0x10: self.int10_video,
            0x11: self.int11_equipment,
            0x12: self.int12_memory,
            0x13: self.int13_disk,
            0x14: self.int14_serial,
            0x15: self.int15_misc,
            0x16: self.int16_keyboard,
            0x1A: self.int1a_time,
            0x18: self.int18_no_boot,
            0x19: self.int19_reboot,
        })
        self._poke16(BDA_EQUIPMENT, DEFAULT_EQUIPMENT)
        self._poke16(BDA_MEMORY_KB, DEFAULT_MEMORY_KB)
        self._poke16(BDA_VIDEO_COLS, VIDEO_COLS)
        self._poke16(BDA_VIDEO_PAGE_OFF, 0)
        self._poke8(BDA_VIDEO_MODE, self.video_mode)
        self._poke16(BDA_CURSOR_SHAPE, 0x0607)
        self.cursor = (0, 0)

    # ------------------------------------------------------------- BDA helpers

    def _poke8(self, off: int, value: int) -> None:
        self.cpu.mem[BDA_BASE + off] = value & 0xFF

    def _poke16(self, off: int, value: int) -> None:
        lin = BDA_BASE + off
        self.cpu.mem[lin] = value & 0xFF
        self.cpu.mem[lin + 1] = (value >> 8) & 0xFF

    def _peek8(self, off: int) -> int:
        return self.cpu.mem[BDA_BASE + off]

    def _peek16(self, off: int) -> int:
        lin = BDA_BASE + off
        return self.cpu.mem[lin] | (self.cpu.mem[lin + 1] << 8)

    # -------------------------------------------------------- register helpers

    @property
    def al(self) -> int:
        return self.cpu.get_reg(REG_AX, 8)

    @al.setter
    def al(self, value: int) -> None:
        self.cpu.set_reg(REG_AX, 8, value)

    @property
    def ah(self) -> int:
        return self.cpu.get_reg(4, 8)

    @ah.setter
    def ah(self, value: int) -> None:
        self.cpu.set_reg(4, 8, value)

    @property
    def ax(self) -> int:
        return self.cpu.get_reg(REG_AX, 16)

    @ax.setter
    def ax(self, value: int) -> None:
        self.cpu.set_reg(REG_AX, 16, value)

    def _carry(self, value: bool) -> None:
        self.cpu.set_flag(CF, value)

    def _wr(self, idx: int, value: int, size: int = 16) -> None:
        self.cpu.set_reg(idx, size, value)

    def _rd(self, idx: int, size: int = 16) -> int:
        return self.cpu.get_reg(idx, size)

    # ---------------------------------------------------------------- video

    @property
    def cursor(self) -> tuple[int, int]:
        """(row, col) for page 0, decoded from the BIOS data area."""
        return divmod(self._peek16(BDA_CURSOR_POS), VIDEO_COLS)

    @cursor.setter
    def cursor(self, rc: tuple[int, int]) -> None:
        row, col = rc
        row = max(0, min(VIDEO_ROWS - 1, row))
        col = max(0, min(VIDEO_COLS - 1, col))
        self._poke16(BDA_CURSOR_POS, row * VIDEO_COLS + col)

    @staticmethod
    def _cell(row: int, col: int) -> int:
        return VIDEO_BASE + (row * VIDEO_COLS + col) * 2

    def _write_cell(self, row: int, col: int, ch: int, attr: int) -> None:
        if 0 <= row < VIDEO_ROWS and 0 <= col < VIDEO_COLS:
            lin = self._cell(row, col)
            self.cpu.mem[lin] = ch & 0xFF
            self.cpu.mem[lin + 1] = attr & 0xFF

    def _scroll_up(self, lines: int, attr: int, top: int, left: int,
                   bottom: int, right: int) -> None:
        if lines <= 0:
            lines = 1
        mem = self.cpu.mem
        for row in range(top, bottom + 1):
            src = row + lines
            for col in range(left, right + 1):
                dst = self._cell(row, col)
                if src <= bottom:
                    s = self._cell(src, col)
                    mem[dst] = mem[s]
                    mem[dst + 1] = mem[s + 1]
                else:
                    mem[dst] = 0x20
                    mem[dst + 1] = attr

    def _scroll_down(self, lines: int, attr: int, top: int, left: int,
                     bottom: int, right: int) -> None:
        if lines <= 0:
            lines = 1
        mem = self.cpu.mem
        for row in range(bottom, top - 1, -1):
            src = row - lines
            for col in range(left, right + 1):
                dst = self._cell(row, col)
                if src >= top:
                    s = self._cell(src, col)
                    mem[dst] = mem[s]
                    mem[dst + 1] = mem[s + 1]
                else:
                    mem[dst] = 0x20
                    mem[dst + 1] = attr

    def clear_screen(self, attr: int = 0x07) -> None:
        mem = self.cpu.mem
        for i in range(0, VIDEO_SIZE, 2):
            mem[VIDEO_BASE + i] = 0x20
            mem[VIDEO_BASE + i + 1] = attr
        self.cursor = (0, 0)

    def teletype(self, ch: int) -> None:
        """INT 10h AH=0Eh semantics; also the kernel's early console path."""
        self.teletype_count += 1
        row, col = self.cursor
        if ch == 0x0A:
            row += 1
        elif ch == 0x0D:
            col = 0
        elif ch == 0x08:
            if col > 0:
                col -= 1
        else:
            self._write_cell(row, col, ch, 0x07)
            col += 1
        if col >= VIDEO_COLS:
            col = 0
            row += 1
        if row >= VIDEO_ROWS:
            self._scroll_up(1, 0x07, 0, 0, VIDEO_ROWS - 1, VIDEO_COLS - 1)
            row = VIDEO_ROWS - 1
        self.cursor = (row, col)

    def int10_video(self, vector: int, cpu: CPU) -> None:
        sub = self.ah
        if sub == 0x00:                                   # set video mode
            self.video_mode = self.al & 0x7F
            self._poke8(BDA_VIDEO_MODE, self.video_mode)
            self.crt_mode = 0x29 if self.video_mode == 3 else 0x00
            self.cursor = (0, 0)
            if self.video_mode in (0, 1, 2, 3, 7):
                self.clear_screen(0x07)
        elif sub == 0x01:                                 # set cursor shape
            self._poke16(BDA_CURSOR_SHAPE, self._rd(REG_CX))
        elif sub == 0x02:                                 # set cursor position
            dx = self._rd(REG_DX)
            self.cursor = ((dx >> 8) & 0xFF, dx & 0xFF)
        elif sub == 0x03:                                 # get cursor position
            row, col = self.cursor
            self._wr(REG_DX, (row << 8) | col)
            self._wr(REG_CX, self._peek16(BDA_CURSOR_SHAPE))
        elif sub in (0x06, 0x07):                         # scroll window
            attr = (self._rd(REG_BX) >> 8) & 0xFF
            cx = self._rd(REG_CX)
            dx = self._rd(REG_DX)
            args = (self.al, attr, (cx >> 8) & 0xFF, cx & 0xFF,
                    (dx >> 8) & 0xFF, dx & 0xFF)
            if sub == 0x06:
                self._scroll_up(*args)
            else:
                self._scroll_down(*args)
        elif sub == 0x09:                                 # write char + attr
            self._write_repeated(self.al, self._rd(REG_BX, 8), self._rd(REG_CX) or 1)
        elif sub == 0x0A:                                 # write char only
            self._write_repeated_attr_only(self.al, self._rd(REG_CX) or 1)
        elif sub == 0x0E:                                 # teletype output
            self.teletype(self.al)
        elif sub == 0x0F:                                 # get video mode
            self.al = self.video_mode
            self.ah = VIDEO_COLS
            self._wr(REG_BX, 0)
        elif sub == 0x13:                                 # write string
            self._write_string(cpu)

    def _write_repeated(self, ch: int, attr: int, count: int) -> None:
        row, col = self.cursor
        for i in range(count):
            self._write_cell(row, col + i, ch, attr)

    def _write_repeated_attr_only(self, attr: int, count: int) -> None:
        row, col = self.cursor
        for i in range(count):
            if 0 <= row < VIDEO_ROWS and 0 <= col + i < VIDEO_COLS:
                self.cpu.mem[self._cell(row, col + i) + 1] = attr & 0xFF

    def _write_string(self, cpu: CPU) -> None:
        """INT 10h AH=13h -- string at ES:BP, CX bytes.

        AL bit 0: the string carries a leading attribute byte per character.
        AL bit 1: update the cursor after writing.
        """
        mode = self.al
        attr = (self._rd(REG_BX) >> 8) & 0xFF
        length = self._rd(REG_CX)
        dx = self._rd(REG_DX)
        es = cpu.sregs[SEG_ES]
        base = cpu.a(es, self._rd(REG_BP))
        row, col = (dx >> 8) & 0xFF, dx & 0xFF
        if mode & 0x02:
            row, col = self.cursor
        i = 0
        while i < length:
            ch = self.cpu.mem[base + i]
            if mode & 0x01:
                attr = self.cpu.mem[base + i + 1]
                i += 2
            else:
                i += 1
            self._write_cell(row, col, ch, attr)
            col += 1
            if col >= VIDEO_COLS:
                col = 0
                row += 1
        if mode & 0x02:
            self.cursor = (row, col)

    # ------------------------------------------------------------ simple ints

    def int11_equipment(self, vector: int, cpu: CPU) -> None:
        self.ax = self._peek16(BDA_EQUIPMENT)

    def int12_memory(self, vector: int, cpu: CPU) -> None:
        self.ax = self._peek16(BDA_MEMORY_KB)

    def int14_serial(self, vector: int, cpu: CPU) -> None:
        sub = self.ah
        if sub == 0x01:
            self.serial_out.append(chr(self.al & 0xFF))
            self.ah = 0x00
        elif sub == 0x03:
            self.ah = 0x60
            self.al = 0x00
        else:
            self.ah = 0x00

    def int15_misc(self, vector: int, cpu: CPU) -> None:
        sub = self.ah
        if sub == 0x24:                          # A20 gate control
            self.a20_enabled = (self.al & 1) == 1
            self.ah = 0x00
            self._carry(False)
        elif sub == 0x88:                        # extended memory in KB
            self.ax = 15 * 1024
            self._carry(False)
        else:
            self.ah = 0x86
            self._carry(True)

    def int1a_time(self, vector: int, cpu: CPU) -> None:
        sub = self.ah
        ticks = self.machine.ticks
        if sub == 0x00:
            self._wr(REG_CX, (ticks >> 16) & 0xFFFF)
            self._wr(REG_DX, ticks & 0xFFFF)
            self.al = 0
            self._carry(False)
        elif sub == 0x01:
            self._wr(REG_CX, 0)
            self._wr(REG_DX, 0)
            self._carry(False)
        elif sub == 0x04:
            self._wr(REG_CX, 0x2026)
            self._wr(REG_DX, 0x0101)
            self._carry(False)

    def int18_no_boot(self, vector: int, cpu: CPU) -> None:
        raise ShutdownRequest("BIOS found no bootable device (INT 18h)")

    def int19_reboot(self, vector: int, cpu: CPU) -> None:
        raise ShutdownRequest("reboot requested (INT 19h)")

    # -------------------------------------------------------------- keyboard

    def int16_keyboard(self, vector: int, cpu: CPU) -> None:
        sub = self.ah
        queue = self.machine.int_key_queue
        if sub in (0x00, 0x10):
            if not queue:
                # Nothing typed.  A real BIOS would block here until a key
                # arrives; the emulator reports "no key" and lets the run loop
                # decide, because blocking would also block the harness that is
                # supposed to supply the keystroke.
                self.ax = 0x0000
                self.cpu.set_flag(ZF, True)
                self.machine.note_input_wait()
                return
            scan, ascii_code = queue.popleft()
            self.al = ascii_code
            self.ah = scan
        elif sub in (0x01, 0x11):
            if queue:
                scan, ascii_code = queue[0]
                self.cpu.set_flag(ZF, False)
                self.al = ascii_code
                self.ah = scan
            else:
                self.cpu.set_flag(ZF, True)
        elif sub == 0x02:
            self.al = self._peek8(BDA_KBD_FLAGS)

    def set_kbd_flag(self, mask: int, value: bool) -> None:
        """Update the BIOS keyboard shift-flag byte at 0x417."""
        flags = self._peek8(BDA_KBD_FLAGS)
        self._poke8(BDA_KBD_FLAGS, (flags | mask) if value else (flags & ~mask & 0xFF))

    # ------------------------------------------------------------------- disk

    def int13_disk(self, vector: int, cpu: CPU) -> None:
        sub = self.ah
        drive = self._rd(REG_DX, 8)
        if sub == 0x00:                                  # reset controller
            self.ah = 0x00
            self._carry(False)
            return
        geo = self.drives.get(drive)
        if sub == 0x08:                                  # drive parameters
            if geo is None:
                self._disk_fail(0x01)
                return
            self._wr(REG_CX, ((geo.cylinders & 0xFF) << 8) | (geo.sectors_per_track & 0x3F))
            self._wr(REG_DX, ((geo.heads - 1) << 8) | (drive & 0x0F))
            self._wr(REG_BX, 0)
            self.ah = 0x00
            self._carry(False)
            return
        if sub == 0x41:                                  # EDD installation check
            if geo is None:
                self._disk_fail(0x01)
                return
            self._wr(REG_BX, 0xAA55)
            self.ah = 0x30
            self._wr(REG_CX, 0x0001)
            self._carry(False)
            return
        if sub == 0x42:                                  # EDD extended read
            if geo is None:
                self._disk_fail(0x01)
                return
            dap = cpu.a(cpu.sregs[SEG_DS], self._rd(REG_SI))
            if cpu.mem[dap] != 0x10:
                self._disk_fail(0x01)
                return
            count = cpu.mem[dap + 2]
            dest_off = self._peek16(dap + 4)
            dest_seg = self._peek16(dap + 6)
            lba = (cpu.mem[dap + 8] | (cpu.mem[dap + 9] << 8)
                   | (cpu.mem[dap + 10] << 16) | (cpu.mem[dap + 11] << 24))
            self._read_lba(geo, lba, count, cpu.a(dest_seg, dest_off))
            return
        if sub in (0x02, 0x03):                          # CHS read / write
            self._rw_sectors(cpu, geo, write=(sub == 0x03))
            return
        self._disk_fail(0x01)

    def _disk_fail(self, code: int) -> None:
        self.ah = code
        self._carry(True)

    def _rw_sectors(self, cpu: CPU, geo: Optional[DiskGeometry], write: bool) -> None:
        if geo is None:
            self._disk_fail(0x01)
            return
        count = self.al
        cx = self._rd(REG_CX)
        cyl = (cx >> 8) & 0xFF
        sector = cx & 0x3F
        head = (self._rd(REG_DX) >> 8) & 0xFF
        if count == 0 or count > 128 or sector == 0 or sector > geo.sectors_per_track:
            self._disk_fail(0x04)
            return
        if cyl >= geo.cylinders or head >= geo.heads:
            self._disk_fail(0x04)
            return
        offset = geo.sector_offset(cyl, head, sector)
        length = count * 512
        if offset + length > len(geo.data):
            self._disk_fail(0x04)
            return
        base = cpu.a(cpu.sregs[SEG_ES], self._rd(REG_BX))
        if write:
            geo.data[offset:offset + length] = cpu.mem[base:base + length]
        else:
            cpu.mem[base:base + length] = geo.data[offset:offset + length]
        self.ah = 0x00
        self._carry(False)

    def _read_lba(self, geo: DiskGeometry, lba: int, count: int, dest: int) -> None:
        offset = lba * 512
        length = count * 512
        if lba < 0 or count == 0 or offset + length > len(geo.data):
            self._disk_fail(0x04)
            return
        self.cpu.mem[dest:dest + length] = geo.data[offset:offset + length]
        self.ah = 0x00
        self._carry(False)

    # ------------------------------------------------------------------- misc

    def attach(self, drive: int, geo: DiskGeometry) -> None:
        self.drives[drive] = geo

    def set_boot_drive(self, drive: int) -> None:
        self._poke8(BDA_BOOT_DRIVE, drive)
