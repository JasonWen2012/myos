"""
myos 16-bit real-mode x86 interpreter.

Design notes
------------
* Registers are kept as four 16-bit backing words so that AL/AH/AX/EAX aliases
  work exactly like the real chip (AL is the low byte of AX, and AX is the low
  word of EAX).  The high halves (AH/CH/DH/BH) alias word registers 4..7.
* Memory is one flat 1 MiB bytearray.  A segment:offset pair resolves to
  (seg << 4) + offset with the 16-bit offset wrapping inside the segment, which
  is what the real chip does and what matters for the video buffer when the
  programmer sets DS=0xB800.
* Instructions are decoded in one pass.  A small Decode object carries the
  prefixes (segment override, operand/address size, REP) and is rebuilt for
  every instruction, so prefixes can never leak into the next instruction.
* Opcode handlers live in ops16.py and are registered into a 256-entry table by
  install().  Dispatch is O(1) and every handler is reachable from a test
  through step().
* Anything not implemented raises NotImplementedOpcode carrying the opcode and
  the CS:IP where it happened.  A silently skipped instruction would make a
  broken OS impossible to debug, so no handler is ever left as a no-op.

The interpreter is deliberately scoped to the subset of the 8086/80386
real-mode instructions the myos kernel uses; tests/test_cpu16.py pins the
semantics of each one that is registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

MEM_SIZE = 0x100000
MASK16 = 0xFFFF
MASK20 = 0xFFFFF

# FLAGS bit positions
CF = 0x0001
PF = 0x0004
AF = 0x0010
ZF = 0x0040
SF = 0x0080
TF = 0x0100
IF = 0x0200
DF = 0x0400
OF = 0x0800

PARITY_TABLE = [bin(i).count("1") % 2 == 0 for i in range(256)]

# (word register index, is high byte) for the eight 8-bit register encodings.
_BYTE_REGISTER_MAP = ((0, False), (1, False), (2, False), (3, False),
                      (0, True), (1, True), (2, True), (3, True))


class EmulatorError(Exception):
    """Base class for all emulator faults."""


class NotImplementedOpcode(EmulatorError):
    """Raised when execution reaches an instruction the interpreter lacks."""

    def __init__(self, opcode: int, cs: int, ip: int, note: str = "") -> None:
        self.opcode = opcode
        self.cs = cs
        self.ip = ip
        self.note = note
        where = f"{cs:04X}:{ip:04X} opcode {opcode:#04x}"
        if note:
            where += f" ({note})"
        super().__init__(f"unimplemented opcode at {where}")


class DivideError(EmulatorError):
    def __init__(self, cs: int, ip: int, detail: str = "") -> None:
        self.cs = cs
        self.ip = ip
        super().__init__(f"divide error (INT 0) at {cs:04X}:{ip:04X} {detail}".rstrip())


class CpuHalt(Exception):
    """Raised by step()/run() when the CPU is idling in HLT."""


class ShutdownRequest(Exception):
    """Raised by a BIOS handler to stop emulation (reboot / no boot device)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class Decode:
    """Per-instruction prefix state and decode options."""

    seg: Optional[int] = None       # segment override register index
    opsize: int = 16                # 16 or 32
    addrsize: int = 16              # 16 or 32
    rep: Optional[int] = None       # 0xF3 -> rep/repe, 0xF2 -> repne


@dataclass
class Operand:
    """A decoded ModRM operand: either a register or a memory location."""

    is_mem: bool
    reg: int = 0                    # register index (0..7) for register operands
    addr: int = 0                   # linear address for memory operands
    seg: int = 0                    # segment register index for memory operands
    size: int = 16                  # operand size in bits
    width: int = 2                  # 1, 2 or 4 bytes
    is_sreg: bool = False           # decoded as a segment register (mov sreg)


@dataclass
class State:
    """Observed side effects of one instruction, used for tests and tracing."""

    cs: int = 0
    ip: int = 0
    opcode: int = 0
    fetched: bytes = b""
    written: list = field(default_factory=list)   # (addr, value, width)
    halted: bool = False


class CPU:
    """A 16-bit real-mode x86 CPU."""

    def __init__(self, mem: Optional[bytearray] = None) -> None:
        self.mem = mem if mem is not None else bytearray(MEM_SIZE)
        # Register file order is the ModRM encoding order: AX CX DX BX SP BP SI DI
        self.regs = [0] * 8
        self.sregs = [0] * 4          # ES CS SS DS
        self.ip16 = 0
        self.flags = 0x0002           # bit 1 is always set
        self.halted = False
        self.int_hooks: dict[int, Callable[[int, "CPU"], None]] = {}
        self.instructions = 0
        self.trace: list[tuple[int, int, int]] = []
        self.trace_limit = 32
        self.state = State()
        self._csbase = 0
        self._csip = 0
        # Optional callbacks the harness installs.
        self.can_wake: Optional[Callable[[], bool]] = None
        self.on_out: Optional[Callable[[int, int, int], None]] = None
        self.on_in: Optional[Callable[[int, int], Optional[int]]] = None
        self.ops = _ensure_ops()
        self.ops0f = _OPS0F
        self._opcounts: dict[tuple[int, int], int] = {}

    # ------------------------------------------------------------------ setup

    @classmethod
    def from_image(cls, image: bytes) -> "CPU":
        cpu = cls()
        cpu.mem[0:len(image)] = bytes(image)[:MEM_SIZE]
        return cpu

    def load(self, image: bytes, lin: int = 0) -> None:
        self.mem[lin:lin + len(image)] = bytes(image)

    def reset(self, cs: int = 0, ip: int = 0) -> None:
        self.halted = False
        self.sregs[1] = cs & MASK16
        self._csbase = (cs << 4) & MASK20
        self.ip16 = ip & MASK16
        self._csip = self._csbase + self.ip16

    # -------------------------------------------------------------- registers

    def get_reg(self, idx: int, size: int) -> int:
        """Read register idx for size 8/16/32.

        ModRM register numbers 0..7 name two different sets of eight registers:
        for 8-bit operands they are AL CL DL BL AH CH DH BH (the high four being
        bytes 1..0 of word registers 0..3), and for 16/32-bit operands they are
        AX CX DX BX SP BP SI DI.  Getting this wrong silently corrupts SP when a
        program does `mov ah, ...`.
        """
        if size == 8:
            word, high = _BYTE_REGISTER_MAP[idx & 7]
            return (self.regs[word] >> 8) & 0xFF if high else self.regs[word] & 0xFF
        if size in (16, 32):
            return self.regs[idx & 7]
        raise ValueError(f"bad register size {size}")

    def set_reg(self, idx: int, size: int, value: int) -> None:
        if size == 8:
            word, high = _BYTE_REGISTER_MAP[idx & 7]
            if high:
                self.regs[word] = (self.regs[word] & 0x00FF) | ((value & 0xFF) << 8)
            else:
                self.regs[word] = (self.regs[word] & 0xFF00) | (value & 0xFF)
            return
        if size in (16, 32):
            self.regs[idx & 7] = value & MASK16
            return
        raise ValueError(f"bad register size {size}")

    def seg_base(self, idx: int) -> int:
        return (self.sregs[idx] << 4) & MASK20

    def set_sreg(self, idx: int, value: int) -> None:
        self.sregs[idx] = value & MASK16
        if idx == 1:
            self._csbase = (value << 4) & MASK20
            self._csip = self._csbase + self.ip16
    def get_seg(self, idx: int) -> int:
        return self.sregs[idx]

    # ------------------------------------------------------------------ flags

    def flag(self, mask: int) -> int:
        return 1 if (self.flags & mask) else 0

    def set_flag(self, mask: int, value) -> None:
        if value:
            self.flags |= mask
        else:
            self.flags &= ~mask

    def clear_cf_of(self) -> None:
        self.flags &= ~(CF | OF)

    # ---------------------------------------------------------------- memory

    def a(self, seg: int, off: int) -> int:
        return ((seg << 4) + (off & MASK16)) & MASK20

    def _check(self, lin: int, width: int) -> None:
        if lin < 0 or lin + width > MEM_SIZE:
            raise EmulatorError(f"memory access out of range: linear {lin:#07x} width {width}")

    def rb(self, lin: int) -> int:
        self._check(lin, 1)
        return self.mem[lin]

    def rw(self, lin: int) -> int:
        self._check(lin, 2)
        return self.mem[lin] | (self.mem[lin + 1] << 8)

    def rd(self, lin: int) -> int:
        self._check(lin, 4)
        return (self.mem[lin] | (self.mem[lin + 1] << 8)
                | (self.mem[lin + 2] << 16) | (self.mem[lin + 3] << 24))

    def wb(self, lin: int, value: int) -> None:
        self._check(lin, 1)
        self.mem[lin] = value & 0xFF

    def ww(self, lin: int, value: int) -> None:
        self._check(lin, 2)
        value &= MASK16
        self.mem[lin] = value & 0xFF
        self.mem[lin + 1] = (value >> 8) & 0xFF

    def wd(self, lin: int, value: int) -> None:
        self._check(lin, 4)
        value &= 0xFFFFFFFF
        for i in range(4):
            self.mem[lin + i] = (value >> (8 * i)) & 0xFF

    def rd_mem(self, lin: int, width: int) -> int:
        if width == 1:
            return self.rb(lin)
        if width == 2:
            return self.rw(lin)
        if width == 4:
            return self.rd(lin)
        raise ValueError(width)

    def wr_mem(self, lin: int, width: int, value: int) -> None:
        if width == 1:
            self.wb(lin, value)
        elif width == 2:
            self.ww(lin, value)
        elif width == 4:
            self.wd(lin, value)
        else:
            raise ValueError(width)

    # --------------------------------------------------------------- stack

    def push(self, value: int, size: int = 16) -> None:
        self.regs[4] = (self.regs[4] - (size // 8)) & MASK16
        self.wr_mem(self.a(self.sregs[2], self.regs[4]), size // 8, value)

    def pop(self, size: int = 16) -> int:
        value = self.rd_mem(self.a(self.sregs[2], self.regs[4]), size // 8)
        self.regs[4] = (self.regs[4] + (size // 8)) & MASK16
        return value

    # -------------------------------------------------------------- fetching

    def fetch8(self) -> int:
        b = self.mem[self._csip & MASK20]
        self._csip = (self._csip + 1) & MASK20
        self.ip16 = (self._csip - self._csbase) & MASK16
        return b

    def fetch_bytes(self, n: int) -> bytes:
        return bytes(self.fetch8() for _ in range(n))

    def fetch16(self) -> int:
        return self.fetch8() | (self.fetch8() << 8)

    def fetch32(self) -> int:
        return self.fetch16() | (self.fetch16() << 16)

    def fetch_s8(self) -> int:
        v = self.fetch8()
        return v - 0x100 if v & 0x80 else v

    def fetch_s16(self) -> int:
        v = self.fetch16()
        return v - 0x10000 if v & 0x8000 else v

    def fetch_i(self, size: int) -> int:
        if size == 8:
            return self.fetch8()
        if size == 16:
            return self.fetch16()
        return self.fetch32()

    def fetch_si(self, size: int) -> int:
        if size == 8:
            return self.fetch_s8()
        if size == 16:
            return self.fetch_s16()
        return self.fetch32()

    def jump_rel(self, size: int) -> None:
        """Advance IP by a signed displacement relative to the next instruction."""
        disp = self.fetch_si(size)
        self._csip = (self._csip + disp) & MASK20
        self.ip16 = (self._csip - self._csbase) & MASK16

    def set_ip(self, value: int) -> None:
        self.ip16 = value & MASK16
        self._csip = self._csbase + self.ip16

    def set_cs_ip(self, cs: int, ip: int) -> None:
        """Change CS and IP together.

        Order matters and getting it wrong is subtle: set_ip() derives the fetch
        pointer from the *current* _csbase, so calling set_ip() before updating
        CS leaves _csip pointing into the old segment.  The CPU then fetches from
        the wrong place while ip16 looks correct, which is why a bad far return
        appears as a jump to a random address with a plausible-looking IP.
        Always use this helper when both change.
        """
        self.sregs[1] = cs & MASK16
        self._csbase = (cs << 4) & MASK20
        self.ip16 = ip & MASK16
        self._csip = self._csbase + self.ip16

    # ---------------------------------------------------------- ModRM decode

    def decode_modrm(self, d: Decode, size: int) -> tuple[Operand, int]:
        """Decode a ModRM byte into (operand, reg_field).

        The reg field is returned raw so callers can treat it either as a
        register operand or as an opcode extension.
        """
        modrm = self.fetch8()
        mod = modrm >> 6
        reg = (modrm >> 3) & 7
        rm = modrm & 7
        width = size // 8

        if mod == 3:
            return Operand(is_mem=False, reg=rm, size=size, width=width), reg

        if d.addrsize == 16:
            if rm == 0:
                base = self.regs[3] + self.regs[6]
            elif rm == 1:
                base = self.regs[3] + self.regs[7]
            elif rm == 2:
                base = self.regs[5] + self.regs[6]
            elif rm == 3:
                base = self.regs[5] + self.regs[7]
            elif rm == 4:
                base = self.regs[6]
            elif rm == 5:
                base = self.regs[7]
            elif rm == 6:
                base = self.fetch16() if mod == 0 else self.regs[4]
            else:
                base = self.regs[3]
            if mod == 1:
                base += self.fetch_s8()
            elif mod == 2:
                base += self.fetch16()
            if d.seg is not None:
                seg = d.seg
            elif rm in (2, 3) or (rm == 6 and mod != 0):
                seg = 2                                  # SS: BP-relative
            else:
                seg = 3                                  # DS otherwise
            offset = base & MASK16
        else:
            sib = self.fetch8() if rm == 4 else None
            base = 0
            if sib is not None:
                scale = 1 << ((sib >> 6) & 3)
                index = (sib >> 3) & 7
                sbase = sib & 7
                if index != 4:                           # ESP cannot be an index
                    base += self.regs[index] * scale
                if sbase == 5 and (sib >> 6) == 0:
                    base += self.fetch32()
                else:
                    base += self.regs[sbase]
            elif rm == 5 and mod == 0:
                base = self.fetch32()
            else:
                base = self.regs[rm]
            if mod == 1:
                base += self.fetch_s8()
            elif mod == 2:
                base += self.fetch32()
            seg = d.seg if d.seg is not None else 3
            offset = base

        return Operand(is_mem=True, addr=self.a(self.sregs[seg], offset),
                       seg=seg, size=size, width=width), reg

    # --------------------------------------------------------- operand access

    def op_read(self, op: Operand) -> int:
        if op.is_mem:
            return self.rd_mem(op.addr, op.width)
        return self.get_reg(op.reg, op.size)

    def op_write(self, op: Operand, value: int) -> None:
        value &= (1 << op.size) - 1
        if op.is_mem:
            self.wr_mem(op.addr, op.width, value)
            self.state.written.append((op.addr, value, op.width))
        else:
            self.set_reg(op.reg, op.size, value)

    def op_read_signed(self, op: Operand) -> int:
        v = self.op_read(op)
        return v - (1 << op.size) if v & (1 << (op.size - 1)) else v

    # ------------------------------------------------------------ flag helpers

    def _flags_add(self, a: int, b: int, size: int, carry: int = 0) -> int:
        mask = (1 << size) - 1
        total = a + b + carry
        res = total & mask
        self.set_flag(CF, total > mask)
        self.set_flag(ZF, res == 0)
        self.set_flag(SF, bool(res & (1 << (size - 1))))
        self.set_flag(AF, ((a ^ b ^ res) & 0x10) != 0)
        self.set_flag(OF, ((a ^ res) & (b ^ res) & (1 << (size - 1))) != 0)
        self.set_flag(PF, PARITY_TABLE[res & 0xFF])
        return res

    def _flags_sub(self, a: int, b: int, size: int, borrow: int = 0) -> int:
        mask = (1 << size) - 1
        total = a - b - borrow
        res = total & mask
        self.set_flag(CF, total < 0)
        self.set_flag(ZF, res == 0)
        self.set_flag(SF, bool(res & (1 << (size - 1))))
        self.set_flag(AF, ((a ^ b ^ res) & 0x10) != 0)
        self.set_flag(OF, ((a ^ b) & (a ^ res) & (1 << (size - 1))) != 0)
        self.set_flag(PF, PARITY_TABLE[res & 0xFF])
        return res

    def _flags_logic(self, res: int, size: int) -> int:
        res &= (1 << size) - 1
        self.set_flag(CF, False)
        self.set_flag(OF, False)
        self.set_flag(AF, False)
        self.set_flag(ZF, res == 0)
        self.set_flag(SF, bool(res & (1 << (size - 1))))
        self.set_flag(PF, PARITY_TABLE[res & 0xFF])
        return res

    # ----------------------------------------------------------- interrupts

    def raise_int(self, vector: int) -> None:
        """Enter an interrupt: BIOS hook when registered, else the real IVT."""
        hook = self.int_hooks.get(vector & 0xFF)
        if hook is not None:
            hook(vector & 0xFF, self)
            return
        base = (vector & 0xFF) * 4
        off = self.rw(base)
        seg = self.rw(base + 2)
        self.push(self.flags, 16)
        self.push(self.sregs[1], 16)
        self.push(self.ip16, 16)
        self.set_flag(IF, False)
        self.set_flag(TF, False)
        self.sregs[1] = seg
        self._csbase = (seg << 4) & MASK20
        self.ip16 = off
        self._csip = self._csbase + off

    def do_iret(self) -> None:
        ip = self.pop(16)
        cs = self.pop(16)
        flags = self.pop(16)
        self.set_cs_ip(cs, ip)
        self.flags = (flags | 0x0002) & MASK16

    def port_in(self, port: int, size: int) -> int:
        """Read a port.  A machine may claim the port through `on_in`.

        The callback returns None for ports it does not model, so the open-bus
        value below is the default rather than the only answer.
        """
        if self.on_in is not None:
            value = self.on_in(port, size)
            if value is not None:
                return value & (0xFF if size == 8 else MASK16)
        return 0xFF if size == 8 else MASK16

    def port_out(self, port: int, size: int, value: int) -> None:
        if self.on_out is not None:
            self.on_out(port, size, value)

    # -------------------------------------------------------------- stepping

    def step(self) -> None:
        if self.halted:
            raise CpuHalt("already halted")
        self.instructions += 1
        st = State(cs=self.sregs[1], ip=self.ip16)
        self.state = st
        d = Decode()
        fetched = bytearray()
        opcode = self.fetch8()
        fetched.append(opcode)
        while opcode in _PREFIXES:
            _PREFIXES[opcode](self, d)
            opcode = self.fetch8()
            fetched.append(opcode)
        st.opcode = opcode

        if opcode == 0x0F:
            second = self.fetch8()
            fetched.append(second)
            handler = self.ops0f[second]
            if handler is None:
                st.fetched = bytes(fetched)
                raise NotImplementedOpcode(second, st.cs, st.ip, note=f"0F {second:02X}")
            handler(second, self, d)
        else:
            handler = self.ops[opcode]
            if handler is None:
                st.fetched = bytes(fetched)
                raise NotImplementedOpcode(opcode, st.cs, st.ip)
            handler(opcode, self, d)

        st.fetched = bytes(fetched)
        key = (st.cs, st.ip)
        self._opcounts[key] = self._opcounts.get(key, 0) + 1
        if self.trace_limit:
            self.trace.append((st.cs, st.ip, opcode))
            if len(self.trace) > self.trace_limit:
                del self.trace[:len(self.trace) - self.trace_limit]

    def hlt_loop_detected(self) -> bool:
        """True when CS:IP has repeated with no progress and nothing can wake us.

        Runs of at most 65536 executions are ignored: a tight but finite wait
        loop (a delay calibrated by timing, for instance) must not be mistaken
        for a dead halt.
        """
        if not self.trace:
            return False
        cs, ip, _ = self.trace[-1]
        if (cs, ip) != (self.sregs[1], self.ip16):
            return False
        return 65536 < self._opcounts.get((cs, ip), 0) < 0xFFFFFF

    def run(self, max_steps: int = 50_000_000) -> str:
        """Run until halt/shutdown/budget.  Returns a human-readable reason."""
        steps = 0
        try:
            while steps < max_steps:
                self.step()
                steps += 1
                if self.halted:
                    return "hlt with interrupts disabled"
                if self.hlt_loop_detected() and not self._can_wake():
                    return "halted: idle loop with no pending input"
        except CpuHalt as exc:
            return str(exc)
        except ShutdownRequest as exc:
            return f"shutdown: {exc.reason}"
        return f"step budget exhausted ({max_steps} instructions)"

    def _can_wake(self) -> bool:
        if self.can_wake is None:
            return False
        return bool(self.can_wake())

    def opcode_histogram(self, top: int = 10) -> list[tuple[str, int]]:
        counts: dict[tuple[int, int], int] = {}
        for (cs, ip), n in self._opcounts.items():
            counts[(cs, ip)] = n
        out = sorted(counts.items(), key=lambda kv: -kv[1])[:top]
        return [(f"{cs:04X}:{ip:04X}", n) for (cs, ip), n in out]


# --------------------------------------------------------------------- prefixes

def _p_seg(idx: int):
    def apply(cpu: CPU, d: Decode) -> None:
        d.seg = idx
    return apply


def _p_opsize(cpu: CPU, d: Decode) -> None:
    d.opsize = 32 if d.opsize == 16 else 16


def _p_addrsize(cpu: CPU, d: Decode) -> None:
    d.addrsize = 32 if d.addrsize == 16 else 16


def _p_rep(cpu: CPU, d: Decode) -> None:
    d.rep = 0xF3


def _p_repne(cpu: CPU, d: Decode) -> None:
    d.rep = 0xF2


_PREFIXES: dict[int, Callable[[CPU, Decode], None]] = {
    0x26: _p_seg(0), 0x2E: _p_seg(1), 0x36: _p_seg(2), 0x3E: _p_seg(3),
    0x64: _p_seg(0), 0x65: _p_seg(0),
    0x66: _p_opsize,
    0x67: _p_addrsize,
    0xF0: lambda cpu, d: None,
    0xF2: _p_repne,
    0xF3: _p_rep,
}

# Populated on first CPU construction by ops16.build_tables(); imported lazily
# so that cpu16 and ops16 do not form an import cycle.
_OPS: list = [None] * 256
_OPS0F: list = [None] * 256


def install(ops: dict[int, Callable], ops0f: dict[int, Callable]) -> None:
    """Register the opcode handler tables produced by ops16.build_tables()."""
    for opcode, handler in ops.items():
        _OPS[opcode] = handler
    for opcode, handler in ops0f.items():
        _OPS0F[opcode] = handler


def _ensure_ops() -> list:
    if _OPS[0x90] is None:
        from .ops16 import build_tables
        install(*build_tables())
    return _OPS


__all__ = [
    "CPU", "Decode", "Operand", "State", "CpuHalt", "DivideError",
    "EmulatorError", "NotImplementedOpcode", "ShutdownRequest", "install",
    "MEM_SIZE", "MASK16", "MASK20",
    "CF", "PF", "AF", "ZF", "SF", "TF", "IF", "DF", "OF", "PARITY_TABLE",
]
