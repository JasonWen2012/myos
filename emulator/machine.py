"""
The emulated machine: memory, video buffer, keyboard, disk drives and the
instruction loop that ties the CPU to the BIOS.

A Machine owns one CPU and one BIOS.  Keyboard input is queued rather than
sampled, and is delivered two ways so both styles of guest work:

  * the BIOS key queue (INT 16h) for boot sectors and simple programs, and
  * the 8042 data port plus IRQ1 for kernels that install their own handler.

Keys are queued as make/break scancode pairs, exactly like the real controller,
so a kernel that only reacts to make codes and a kernel that tracks modifiers
both behave correctly.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Iterable, Optional

from .bios import (
    BIOS, DiskGeometry, floppy_1440, hard_disk,
    VIDEO_BASE, VIDEO_COLS, VIDEO_ROWS, BDA_TICKS,
)
from .cpu16 import CPU, CpuHalt, EmulatorError, ShutdownRequest, IF

KBD_DATA_PORT = 0x60
KBD_STATUS_PORT = 0x64
INT_KEYBOARD = 0x09

# Set-1 scan codes: (make, break) for every character the harness can type.
_SCANCODES: dict[str, tuple[int, int]] = {
    "esc": (0x01, 0x81), "1": (0x02, 0x82), "2": (0x03, 0x83), "3": (0x04, 0x84),
    "4": (0x05, 0x85), "5": (0x06, 0x86), "6": (0x07, 0x87), "7": (0x08, 0x88),
    "8": (0x09, 0x89), "9": (0x0A, 0x8A), "0": (0x0B, 0x8B), "-": (0x0C, 0x8C),
    "=": (0x0D, 0x8D), "backspace": (0x0E, 0x8E), "tab": (0x0F, 0x8F),
    "q": (0x10, 0x90), "w": (0x11, 0x91), "e": (0x12, 0x92), "r": (0x13, 0x93),
    "t": (0x14, 0x94), "y": (0x15, 0x95), "u": (0x16, 0x96), "i": (0x17, 0x97),
    "o": (0x18, 0x98), "p": (0x19, 0x99), "[": (0x1A, 0x9A), "]": (0x1B, 0x9B),
    "enter": (0x1C, 0x9C), "ctrl": (0x1D, 0x9D),
    "a": (0x1E, 0x9E), "s": (0x1F, 0x9F), "d": (0x20, 0xA0), "f": (0x21, 0xA1),
    "g": (0x22, 0xA2), "h": (0x23, 0xA3), "j": (0x24, 0xA4), "k": (0x25, 0xA5),
    "l": (0x26, 0xA6), ";": (0x27, 0xA7), "'": (0x28, 0xA8), "`": (0x29, 0xA9),
    "shift": (0x2A, 0xAA), "\\": (0x2B, 0xAB),
    "z": (0x2C, 0xAC), "x": (0x2D, 0xAD), "c": (0x2E, 0xAE), "v": (0x2F, 0xAF),
    "b": (0x30, 0xB0), "n": (0x31, 0xB1), "m": (0x32, 0xB2), ",": (0x33, 0xB3),
    ".": (0x34, 0xB4), "/": (0x35, 0xB5), "alt": (0x38, 0xB8), "space": (0x39, 0xB9),
    "caps": (0x3A, 0xBA),
    "f1": (0x3B, 0xBB), "f2": (0x3C, 0xBC), "f3": (0x3D, 0xBD), "f4": (0x3E, 0xBE),
    "f5": (0x3F, 0xBF), "f6": (0x40, 0xC0), "f7": (0x41, 0xC1), "f8": (0x42, 0xC2),
    "f9": (0x43, 0xC3), "f10": (0x44, 0xC4),
    "home": (0x47, 0xC7), "up": (0x48, 0xC8), "pageup": (0x49, 0xC9),
    "left": (0x4B, 0xCB), "right": (0x4D, 0xCD), "end": (0x4F, 0xCF),
    "down": (0x50, 0xD0), "pagedown": (0x51, 0xD1), "insert": (0x52, 0xD2),
    "delete": (0x53, 0xD3),
}

_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}

_ASCII_FOR_SCAN = {
    0x1C: 0x0D, 0x0E: 0x08, 0x0F: 0x09, 0x39: 0x20, 0x01: 0x1B,
}


def scancode_for(char: str) -> tuple[int, int, bool]:
    """Return (make, break, needs_shift) for one character."""
    if char == "\n":
        char = "enter"
    if char == " ":
        char = "space"
    if char in _SHIFTED:                          # a symbol Shift produces, e.g. '!'
        make, brk = _SCANCODES[_SHIFTED[char]]
        return make, brk, True
    lower = char.lower()
    if lower in _SCANCODES:
        make, brk = _SCANCODES[lower]
        # An uppercase letter is the same key as its lowercase form; the firmware
        # only reports the capital while Shift is held.  Returning "no shift"
        # here (which the earlier ordering did, because the lowercase key exists)
        # typed 'e' for 'E', so a case-insensitivity test could pass without the
        # harness ever having sent a capital.
        return make, brk, char.isalpha() and char.isupper()
    raise KeyError(f"no scancode for {char!r}")


def ascii_for(make: int, shifted: bool) -> int:
    """Best-effort ASCII for a make code, used only for the BIOS key queue."""
    if make in _ASCII_FOR_SCAN:
        return _ASCII_FOR_SCAN[make]
    for name, (code, _brk) in _SCANCODES.items():
        if code == make and len(name) == 1:
            ch = name
            if shifted:
                for up, base in _SHIFTED.items():
                    if base == ch:
                        return ord(up)
                return ord(ch.upper())
            return ord(ch)
    return 0


class Machine:
    """One emulated PC."""

    def __init__(self, mem: Optional[bytearray] = None) -> None:
        self.cpu = CPU(mem)
        self.bios = BIOS(self)
        self.image_name = "<none>"
        self.pending_keys: deque[tuple[int, int]] = deque()
        self.int_key_queue: deque[tuple[int, int]] = deque()
        self.serial_input: deque[str] = deque()
        self.instructions_per_tick = 5000
        self.intr_after_instructions = 5000
        self._last_tick_at = 0
        self.ticks = 0
        self.halt_reason: Optional[str] = None
        self.boot_drive = 0
        # Bytes the emulated 8042 has latched and not yet handed over, and a count
        # of the ones the firmware handler acknowledged itself (see
        # deliver_keyboard_interrupt).
        self.firmware_irq_count = 0
        # Set by the BIOS when a guest asks for a key and none is queued; the run
        # loop uses it to tell "waiting for the user" from "spinning".
        self.input_wait_seen = False
        self.input_wait_at_instruction = 0
        self.cpu.can_wake = self._can_wake
        self.cpu.on_out = self._on_out
        self.cpu.on_in = self._on_in

    # ------------------------------------------------------------------ loading

    def load_image(self, image: bytes | bytearray | str | Path,
                   drive: int = 0, geometry: Optional[DiskGeometry] = None) -> DiskGeometry:
        """Mount a raw disk image (bytes or a path) as the given BIOS drive."""
        if isinstance(image, (str, Path)):
            data = Path(image).read_bytes()
            self.image_name = str(image)
        else:
            data = bytes(image)
        geo = geometry if geometry is not None else _geometry_for(data)
        geo.data[0:len(data)] = data[:len(geo.data)]
        self.bios.attach(drive, geo)
        self.boot_drive = drive
        self.bios.set_boot_drive(drive)
        return geo

    def boot(self, drive: int = 0, vector: int = 0x19) -> None:
        """Power on: reset the CPU and start executing the boot sector.

        The emulator loads the boot sector the way real firmware does: sector 0
        of a floppy image, or the active partition's first sector for a hard
        disk image, both at 0000:7C00 with DL set to the drive.  This keeps a
        single code path for the floppy and partitioned-image cases.
        """
        self.cpu.reset(0, 0)
        self.cpu.halted = False
        segment, offset = self._load_boot_sector(drive)
        self.cpu.sregs[1] = segment
        self.cpu._csbase = (segment << 4) & 0xFFFFF
        self.cpu.set_ip(offset)
        self.cpu.set_reg(2, 16, drive)          # DL = boot drive
        self.cpu.set_flag(IF, True)

    def _load_boot_sector(self, drive: int) -> tuple[int, int]:
        geo = self.bios.drives.get(drive)
        if geo is None:
            raise EmulatorError(f"no disk attached as drive {drive:#x}")
        if len(geo.data) < 512:
            raise EmulatorError("disk image is smaller than one sector")
        sector0 = bytes(geo.data[0:512])
        lba = 0
        if sector0[510:512] == b"\x55\xaa":
            for i in range(4):                  # find the active partition
                entry = 0x1BE + i * 16
                if sector0[entry + 4] == 0x80:
                    lba = int.from_bytes(sector0[entry + 8:entry + 12], "little")
                    break
        if lba == 0:
            self.cpu.mem[0x7C00:0x7E00] = sector0
        else:
            offset = lba * 512
            if offset + 512 > len(geo.data):
                raise EmulatorError(f"active partition LBA {lba} is outside the image")
            self.cpu.mem[0x7C00:0x7E00] = geo.data[offset:offset + 512]
        return 0x0000, 0x7C00

    # ------------------------------------------------------------- keyboard

    def type_text(self, text: str) -> None:
        """Queue a string as keystrokes, with correct shift handling."""
        for char in text:
            try:
                make, brk, shift = scancode_for(char)
            except KeyError:
                raise KeyError(f"cannot type {char!r}: no scancode mapping")
            if shift:
                self._enqueue(_SCANCODES["shift"][0], ascii_for(_SCANCODES["shift"][0], False))
            self._enqueue(make, ascii_for(make, shift))
            self.pending_keys.append((brk, 0))
            self.int_key_queue.append((make, ascii_for(make, shift)))
            if shift:
                self.pending_keys.append((_SCANCODES["shift"][1], 0))
                self.int_key_queue.append((_SCANCODES["shift"][1], 0))

    def press_key(self, name: str, shift: bool = False) -> None:
        """Queue a single named key such as "enter", "backspace" or "f1"."""
        key = name.lower()
        if key not in _SCANCODES:
            raise KeyError(f"unknown key name {name!r}")
        make, brk = _SCANCODES[key]
        if shift:
            self._enqueue(_SCANCODES["shift"][0], 0)
        self._enqueue(make, ascii_for(make, shift))
        self.pending_keys.append((brk, 0))
        self.int_key_queue.append((make, ascii_for(make, shift)))
        if shift:
            self.pending_keys.append((_SCANCODES["shift"][1], 0))

    def _enqueue(self, scan: int, ascii_code: int) -> None:
        self.pending_keys.append((scan, ascii_code))

    def _can_wake(self) -> bool:
        return bool(self.pending_keys)

    def note_input_wait(self) -> None:
        """Called by the BIOS when a guest blocks waiting for a keystroke.

        The run loop uses this to recognise an idle shell: a kernel polling INT
        16h with nothing queued is waiting for the user, not spinning, so a test
        can stop there and type the next line.  Without it the loop would sit
        until its step budget ran out, because the machine's workload never goes
        fully quiescent while the shell is polling.
        """
        self.input_wait_seen = True
        self.input_wait_at_instruction = self.cpu.instructions

    def _on_out(self, port: int, size: int, value: int) -> None:
        if port in (0x20, 0xA0):                 # PIC command port: EOI
            pass
        elif port == KBD_DATA_PORT:
            pass

    def _on_in(self, port: int, size: int) -> Optional[int]:
        """The 8042 side of the keyboard, for a guest that owns IRQ1.

        A kernel handler for INT 09h reads the scancode from port 0x60 and checks
        the status port to see whether more bytes are waiting.  Returning None
        means "not a port this machine models", and the CPU falls back to its
        default open-bus value.

        Note who consumes the byte.  A guest handler reading 0x60 takes it out of
        the latched queue, which is what stops IRQ1 from being raised again on the
        very next instruction; when the guest has no handler at all, the firmware
        path in deliver_keyboard_interrupt does that instead.  Taking the byte in
        both places would lose half the input, and taking it in neither would leave
        the machine convinced a keystroke is permanently pending.
        """
        if port == KBD_DATA_PORT:
            if not self.pending_keys:
                return 0x00
            scan, _ascii = self.pending_keys.popleft()
            return scan
        if port == KBD_STATUS_PORT:
            return 0x01 if self.pending_keys else 0x00   # bit 0: output buffer full
        return None

    def deliver_keyboard_interrupt(self) -> None:
        """Model IRQ1 for one latched scancode.

        Two cases, and they are genuinely different:

        * the guest installed its own INT 09h vector.  Raise the interrupt and let
          the kernel's handler read the byte from port 0x60 (see
          ``CPU.raise_int``), exactly once per latched byte.
        * it did not, so on a real machine the *firmware's* handler would run.
          That handler lives in Python here and never writes a stub into the guest
          IVT, so raising the interrupt would send the guest to IVT entry 9, which
          is still 0:0 -- the CPU would execute the interrupt table and the BIOS
          data area.  That is not a hypothetical: a real-mode kernel that did `sti`
          without owning vector 9 crashed exactly this way.  The firmware's effect
          is therefore modelled directly, which is to acknowledge the byte from the
          controller; the translated key is already in the queue INT 16h serves.
        """
        if not (self.pending_keys and self.cpu.flag(IF)):
            return
        base = INT_KEYBOARD * 4
        if self.cpu.rw(base) or self.cpu.rw(base + 2):
            self.cpu.raise_int(INT_KEYBOARD)
            return
        self.pending_keys.popleft()                  # the firmware handler's work
        self.firmware_irq_count += 1

    # ---------------------------------------------------------------- running

    def _update_clock(self) -> None:
        if self.cpu.instructions - self._last_tick_at >= self.instructions_per_tick:
            elapsed = (self.cpu.instructions - self._last_tick_at) // self.instructions_per_tick
            self._last_tick_at += elapsed * self.instructions_per_tick
            self.ticks += elapsed
            self.bios._poke16(BDA_TICKS, self.ticks & 0xFFFF)
            self.bios._poke16(BDA_TICKS + 2, (self.ticks >> 16) & 0xFFFF)

    def step(self) -> None:
        self.cpu.step()
        self._update_clock()
        self.deliver_keyboard_interrupt()

    def run(self, max_steps: int = 50_000_000) -> str:
        """Run until the CPU idles, requests shutdown, or the budget runs out."""
        for _ in range(max_steps):
            try:
                self.step()
            except CpuHalt as exc:
                self.halt_reason = str(exc) or "hlt"
                return self.halt_reason
            except ShutdownRequest as exc:
                self.halt_reason = f"shutdown: {exc.reason}"
                return self.halt_reason
            if self.cpu.halted:
                self.halt_reason = "hlt with interrupts disabled"
                return self.halt_reason
        self.halt_reason = f"step budget exhausted ({max_steps} instructions)"
        return self.halt_reason

    def run_until_idle(self, max_steps: int = 50_000_000,
                       idle_polls: int = 200_000) -> str:
        """Run until the machine settles, the CPU halts, or it waits for a key.

        An OS that idles in an STI/HLT loop never trips CpuHalt, so tests need a
        quiescence detector.  A shell that is polling INT 16h with an empty queue
        is also "settled" -- it is waiting for the user -- and is reported as
        `waiting for input` so a test can type the next line.
        """
        last = self.state_digest()
        steps = 0
        self.input_wait_seen = False
        while steps < max_steps:
            chunk = min(idle_polls, max_steps - steps)
            for _ in range(chunk):
                try:
                    self.step()
                except CpuHalt as exc:
                    self.halt_reason = str(exc) or "hlt"
                    return self.halt_reason
                except ShutdownRequest as exc:
                    self.halt_reason = f"shutdown: {exc.reason}"
                    return self.halt_reason
                if self.cpu.halted:
                    self.halt_reason = "hlt with interrupts disabled"
                    return self.halt_reason
                # "Nothing typed that the guest has not already read" is the BIOS
                # key queue, not the 8042 latch: a kernel running with IF=0 never
                # acknowledges the latch (no IRQ1 is delivered), so waiting on it
                # would sit out the whole step budget after every keystroke
                # instead of reporting a shell that is waiting for the user.
                if self.input_wait_seen and not self.int_key_queue:
                    self.halt_reason = "waiting for input"
                    return self.halt_reason
            steps += chunk
            current = self.state_digest()
            if current == last:
                self.halt_reason = "idle"
                return self.halt_reason
            last = current
        self.halt_reason = f"step budget exhausted ({max_steps} instructions)"
        return self.halt_reason

    def state_digest(self) -> bytes:
        """Hash of everything a test would care about being stable."""
        import hashlib
        h = hashlib.sha256()
        h.update(bytes(self.cpu.mem[VIDEO_BASE:VIDEO_BASE + VIDEO_COLS * VIDEO_ROWS * 2]))
        h.update(bytes(self.cpu.mem[0x400:0x500]))
        h.update(str(self.cpu.sregs).encode())
        h.update(str(self.cpu.ip16).encode())
        h.update(str(len(self.pending_keys)).encode())
        h.update(str(self.cpu.instructions).encode())
        return h.digest()

    # ------------------------------------------------------------------ video

    def screen_chars(self) -> list[str]:
        """25 lines of 80 characters as currently displayed."""
        mem = self.cpu.mem
        lines = []
        for row in range(VIDEO_ROWS):
            base = VIDEO_BASE + row * VIDEO_COLS * 2
            chars = []
            for col in range(VIDEO_COLS):
                code = mem[base + col * 2]
                if code == 0:
                    code = 0x20
                chars.append(chr(code) if 32 <= code < 127 else " ")
            lines.append("".join(chars))
        return lines

    def screen_text(self, rstrip: bool = True) -> str:
        lines = self.screen_chars()
        if rstrip:
            lines = [line.rstrip() for line in lines]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def screen_attrs(self) -> list[int]:
        mem = self.cpu.mem
        return [mem[VIDEO_BASE + i * 2 + 1] for i in range(VIDEO_COLS * VIDEO_ROWS)]

    def cursor(self) -> tuple[int, int]:
        return self.bios.cursor

    # ------------------------------------------------------------------ report

    def describe(self) -> str:
        row, col = self.cursor()
        return (f"image={self.image_name} instructions={self.cpu.instructions} "
                f"cs:ip={self.cpu.sregs[1]:04X}:{self.cpu.ip16:04X} "
                f"cursor={row},{col} reason={self.halt_reason}")

    def register_dump(self) -> dict[str, int]:
        names = ("AX", "CX", "DX", "BX", "SP", "BP", "SI", "DI")
        dump = {n: self.cpu.regs[i] for i, n in enumerate(names)}
        dump["CS"] = self.cpu.sregs[1]
        dump["DS"] = self.cpu.sregs[3]
        dump["ES"] = self.cpu.sregs[0]
        dump["SS"] = self.cpu.sregs[2]
        dump["IP"] = self.cpu.ip16
        dump["FLAGS"] = self.cpu.flags
        return dump


def _geometry_for(data: bytes) -> DiskGeometry:
    """Pick a sane CHS geometry for a raw image based on its size."""
    size = len(data)
    if size <= 1474560:
        return floppy_1440()
    sectors = (size + 511) // 512
    spt, heads = 63, 16
    cyl = max(1, (sectors + spt * heads - 1) // (spt * heads))
    return hard_disk(cyl, heads, spt)
