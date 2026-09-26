"""
Opcode handlers for the myos 16-bit real-mode interpreter.

Each handler has the signature handler(opcode, cpu, decode).  build_tables()
returns the 256-entry one-byte table plus the 0F-prefixed table.

Handlers are grouped by opcode family and the register / immediate / r-m forms
of one arithmetic operation share a single implementation, so flag semantics are
written once and tested once.  Scope is the instruction subset the myos boot
sector and 16-bit kernel actually use; everything else stays unregistered so the
interpreter reports it loudly instead of silently skipping it.

Register encoding order (ModRM reg field): AX CX DX BX SP BP SI DI.
"""

from __future__ import annotations

from typing import Callable, Dict

from .cpu16 import (
    CPU, Decode, Operand, DivideError, PARITY_TABLE,
    CF, PF, AF, ZF, SF, OF, IF, DF,
)

Handler = Callable[[int, CPU, Decode], None]

OPS: Dict[int, Handler] = {}
OPS0F: Dict[int, Handler] = {}

SEG_ES, SEG_CS, SEG_SS, SEG_DS = 0, 1, 2, 3
REG_AX, REG_CX, REG_DX, REG_BX, REG_SP, REG_BP, REG_SI, REG_DI = range(8)

_ARITH_KINDS = ("add", "or", "adc", "sbb", "and", "sub", "xor", "cmp")


def op(*opcodes: int) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        for o in opcodes:
            OPS[o] = fn
        return fn
    return deco


def op0f(*opcodes: int) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        for o in opcodes:
            OPS0F[o] = fn
        return fn
    return deco


def _operand_from_reg(reg: int, size: int) -> Operand:
    return Operand(is_mem=False, reg=reg, size=size, width=size // 8)


# --------------------------------------------------------- arithmetic family

def _apply(kind: str, cpu: CPU, a: int, b: int, size: int) -> int:
    """Apply one arithmetic operation and set flags; returns the result."""
    if kind == "add":
        return cpu._flags_add(a, b, size)
    if kind == "or":
        return cpu._flags_logic(a | b, size)
    if kind == "adc":
        return cpu._flags_add(a, b, size, carry=cpu.flag(CF))
    if kind == "sbb":
        return cpu._flags_sub(a, b, size, borrow=cpu.flag(CF))
    if kind == "and":
        return cpu._flags_logic(a & b, size)
    if kind == "sub":
        return cpu._flags_sub(a, b, size)
    if kind == "xor":
        return cpu._flags_logic(a ^ b, size)
    if kind == "cmp":
        cpu._flags_sub(a, b, size)
        return a
    raise AssertionError(kind)


def _make_arith_rm_reg(opcode: int) -> Handler:
    """r/m,reg and reg,r/m forms for one opcode in 0x00..0x3D.

    Bit 0 of the opcode selects the byte form, bit 1 the direction.
    """
    kind = _ARITH_KINDS[(opcode >> 3) & 7]
    force8 = (opcode & 1) == 0
    to_rm = (opcode & 3) in (0, 1)

    def handler(op: int, cpu: CPU, d: Decode) -> None:
        sz = 8 if force8 else d.opsize
        rm, reg = cpu.decode_modrm(d, sz)
        regop = _operand_from_reg(reg, sz)
        if to_rm:
            res = _apply(kind, cpu, cpu.op_read(rm), cpu.op_read(regop), sz)
            if kind != "cmp":
                cpu.op_write(rm, res)
        else:
            res = _apply(kind, cpu, cpu.op_read(regop), cpu.op_read(rm), sz)
            if kind != "cmp":
                cpu.op_write(regop, res)
    return handler


def _make_arith_acc_imm(opcode: int) -> Handler:
    """AL/AX accumulator immediate form (0x04, 0x05, 0x0C, ...)."""
    kind = _ARITH_KINDS[(opcode >> 3) & 7]
    force8 = (opcode & 1) == 0

    def handler(op: int, cpu: CPU, d: Decode) -> None:
        sz = 8 if force8 else d.opsize
        imm = cpu.fetch_i(sz)
        res = _apply(kind, cpu, cpu.get_reg(REG_AX, sz), imm, sz)
        if kind != "cmp":
            cpu.set_reg(REG_AX, sz, res)
    return handler


def _register_arith_family() -> None:
    """Opcodes 0x00..0x3D: eight operations x six addrmodes."""
    for kind_index in range(8):
        base = kind_index * 8
        OPS[base + 0] = _make_arith_rm_reg(base + 0)     # r/m8, r8
        OPS[base + 1] = _make_arith_rm_reg(base + 1)     # r/m16, r16
        OPS[base + 2] = _make_arith_rm_reg(base + 2)     # r8, r/m8
        OPS[base + 3] = _make_arith_rm_reg(base + 3)     # r16, r/m16
        OPS[base + 4] = _make_arith_acc_imm(base + 4)    # AL, imm8
        OPS[base + 5] = _make_arith_acc_imm(base + 5)    # AX, imm16
    # 0x06/0x07, 0x0E, 0x16/0x17 and 0x1E/0x1F are segment push/pop and fall
    # through to the dedicated handlers registered below.


def _group1_common(opcode: int, cpu: CPU, d: Decode, size: int, immediate: str) -> None:
    """Shared body of 0x80/0x81/0x82/0x83.

    The operation is selected by the ModRM reg field, NOT by the opcode: one
    opcode value covers all eight operations (add, or, adc, sbb, and, sub, xor,
    cmp).  Binding a separate handler per reg value and then asserting that the
    register matched is wrong -- it rejects cmp (reg 7), which is one of the most
    common instructions a compiler emits for a loop bound.
    """
    rm, reg = cpu.decode_modrm(d, size)
    kind = _ARITH_KINDS[reg]
    if immediate == "imm8":
        imm = cpu.fetch8()
    elif immediate == "imm8sx":
        imm = cpu.fetch_s8() & ((1 << size) - 1)
    else:
        imm = cpu.fetch_i(size)
    res = _apply(kind, cpu, cpu.op_read(rm), imm, size)
    if kind != "cmp":
        cpu.op_write(rm, res)


def _register_group1() -> None:
    """0x80/0x82 imm8, 0x81 imm16, 0x83 sign-extended imm8.

    These are registered with setdefault because 0x84..0x8F are occupied by test,
    xchg and mov, which register themselves at module import time.
    """
    def make(size_mode: int, immediate: str) -> Handler:
        def handler(opcode: int, cpu: CPU, d: Decode) -> None:
            size = 8 if size_mode == 8 else d.opsize
            _group1_common(opcode, cpu, d, size, immediate)
        return handler

    OPS.setdefault(0x80, make(8, "imm8"))
    OPS.setdefault(0x82, make(8, "imm8"))
    OPS.setdefault(0x81, make(0, "imm16"))
    OPS.setdefault(0x83, make(0, "imm8sx"))


# --------------------------------------------------------------- mov and lea

def _register_mov() -> None:
    def mov_rm_reg8(opcode: int, cpu: CPU, d: Decode) -> None:
        rm, reg = cpu.decode_modrm(d, 8)
        cpu.op_write(rm, cpu.get_reg(reg, 8))

    def mov_reg_rm8(opcode: int, cpu: CPU, d: Decode) -> None:
        rm, reg = cpu.decode_modrm(d, 8)
        cpu.set_reg(reg, 8, cpu.op_read(rm))

    def mov_rm_reg(opcode: int, cpu: CPU, d: Decode) -> None:
        sz = d.opsize
        rm, reg = cpu.decode_modrm(d, sz)
        cpu.op_write(rm, cpu.get_reg(reg, sz))

    def mov_reg_rm(opcode: int, cpu: CPU, d: Decode) -> None:
        sz = d.opsize
        rm, reg = cpu.decode_modrm(d, sz)
        cpu.set_reg(reg, sz, cpu.op_read(rm))

    OPS[0x88] = mov_rm_reg8
    OPS[0x8A] = mov_reg_rm8
    OPS[0x89] = mov_rm_reg
    OPS[0x8B] = mov_reg_rm
    OPS[0x8C] = _mov_from_sreg
    OPS[0x8E] = _mov_to_sreg
    OPS[0x8D] = _lea
    OPS[0xC6] = _mov_rm_imm8
    OPS[0xC7] = _mov_rm_imm

    @op(0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7)
    def mov_imm8(opcode: int, cpu: CPU, d: Decode) -> None:
        cpu.set_reg(opcode - 0xB0, 8, cpu.fetch8())

    @op(0xB8, 0xB9, 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF)
    def mov_imm(opcode: int, cpu: CPU, d: Decode) -> None:
        cpu.set_reg(opcode - 0xB8, d.opsize, cpu.fetch_i(d.opsize))

    @op(0xA0, 0xA1, 0xA2, 0xA3)
    def mov_acc_mem(opcode: int, cpu: CPU, d: Decode) -> None:
        seg = d.seg if d.seg is not None else SEG_DS
        lin = cpu.a(cpu.sregs[seg], cpu.fetch16())
        if opcode == 0xA0:
            cpu.set_reg(REG_AX, 8, cpu.rd_mem(lin, 1))
        elif opcode == 0xA1:
            cpu.set_reg(REG_AX, d.opsize, cpu.rd_mem(lin, d.opsize // 8))
        elif opcode == 0xA2:
            cpu.wr_mem(lin, 1, cpu.get_reg(REG_AX, 8))
        else:
            cpu.wr_mem(lin, d.opsize // 8, cpu.get_reg(REG_AX, d.opsize))


def _mov_from_sreg(opcode: int, cpu: CPU, d: Decode) -> None:
    rm, reg = cpu.decode_modrm(d, d.opsize)
    cpu.op_write(rm, cpu.get_seg(reg & 3))


def _mov_to_sreg(opcode: int, cpu: CPU, d: Decode) -> None:
    rm, reg = cpu.decode_modrm(d, d.opsize)
    if (reg & 7) > 3:
        raise AssertionError(f"mov to invalid segment register {reg}")
    cpu.set_sreg(reg, cpu.op_read(rm))


def _mov_rm_imm8(opcode: int, cpu: CPU, d: Decode) -> None:
    rm, _ = cpu.decode_modrm(d, 8)
    cpu.op_write(rm, cpu.fetch8())


def _mov_rm_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    rm, _ = cpu.decode_modrm(d, d.opsize)
    cpu.op_write(rm, cpu.fetch_i(d.opsize))


def _lea(opcode: int, cpu: CPU, d: Decode) -> None:
    """Load the effective *offset* of a memory operand (never its linear address)."""
    saved_ip = cpu._csip
    _probe, reg = cpu.decode_modrm(d, d.opsize)
    consumed = cpu._csip - saved_ip
    cpu._csip = saved_ip
    cpu.ip16 = (cpu._csip - cpu._csbase) & 0xFFFF
    target, reg2 = cpu.decode_modrm(d, d.opsize)
    cpu._csip = saved_ip + consumed
    cpu.ip16 = (cpu._csip - cpu._csbase) & 0xFFFF
    if not target.is_mem:
        raise AssertionError("lea with a register operand")
    offset = (target.addr - ((cpu.sregs[target.seg] << 4) & 0xFFFFF)) & 0xFFFF
    cpu.set_reg(reg2, d.opsize, offset)


# ------------------------------------------------------------------- inc/dec

def _preserve_cf(cpu: CPU, fn: Callable[[], None]) -> None:
    cf = cpu.flags & CF
    fn()
    cpu.flags = (cpu.flags & ~CF) | cf


def _register_incdec() -> None:
    def make_rm(size: int, delta: int) -> Handler:
        def handler(opcode: int, cpu: CPU, d: Decode) -> None:
            rm, _ = cpu.decode_modrm(d, size)
            if delta > 0:
                _preserve_cf(cpu, lambda: cpu.op_write(rm, cpu._flags_add(cpu.op_read(rm), 1, size)))
            else:
                _preserve_cf(cpu, lambda: cpu.op_write(rm, cpu._flags_sub(cpu.op_read(rm), 1, size)))
        return handler

    def fe_group(opcode: int, cpu: CPU, d: Decode) -> None:
        rm, reg = cpu.decode_modrm(d, 8)
        if reg == 0:
            _preserve_cf(cpu, lambda: cpu.op_write(rm, cpu._flags_add(cpu.op_read(rm), 1, 8)))
        elif reg == 1:
            _preserve_cf(cpu, lambda: cpu.op_write(rm, cpu._flags_sub(cpu.op_read(rm), 1, 8)))
        else:
            raise AssertionError(f"0xFE /{reg} is undefined")

    OPS[0xFE] = fe_group
    OPS[0xFF] = _ff_group

    @op(0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47)
    def inc_reg(opcode: int, cpu: CPU, d: Decode) -> None:
        reg = opcode - 0x40
        sz = d.opsize
        _preserve_cf(cpu, lambda: cpu.set_reg(
            reg, sz, cpu._flags_add(cpu.get_reg(reg, sz), 1, sz)))

    @op(0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F)
    def dec_reg(opcode: int, cpu: CPU, d: Decode) -> None:
        reg = opcode - 0x48
        sz = d.opsize
        _preserve_cf(cpu, lambda: cpu.set_reg(
            reg, sz, cpu._flags_sub(cpu.get_reg(reg, sz), 1, sz)))


def _ff_group(opcode: int, cpu: CPU, d: Decode) -> None:
    """0xFF /0 inc, /1 dec, /2 call near, /3 call far, /4 jmp near, /5 jmp far,
    /6 push, /7 undefined."""
    sz = d.opsize
    rm, reg = cpu.decode_modrm(d, sz)
    if reg == 0:
        _preserve_cf(cpu, lambda: cpu.op_write(rm, cpu._flags_add(cpu.op_read(rm), 1, sz)))
    elif reg == 1:
        _preserve_cf(cpu, lambda: cpu.op_write(rm, cpu._flags_sub(cpu.op_read(rm), 1, sz)))
    elif reg == 2:
        cpu.push(cpu.ip16, 16)
        cpu.set_ip(cpu.op_read(rm))
    elif reg == 3:
        target = cpu.op_read(rm)
        cpu.push(cpu.sregs[SEG_CS], 16)
        cpu.push(cpu.ip16, 16)
        cpu.sregs[SEG_CS] = (target >> 16) & 0xFFFF
        cpu._csbase = (cpu.sregs[SEG_CS] << 4) & 0xFFFFF
        cpu.set_ip(target & 0xFFFF)
    elif reg == 4:
        cpu.set_ip(cpu.op_read(rm))
    elif reg == 5:
        target = cpu.op_read(rm)
        cpu.sregs[SEG_CS] = (target >> 16) & 0xFFFF
        cpu._csbase = (cpu.sregs[SEG_CS] << 4) & 0xFFFFF
        cpu.set_ip(target & 0xFFFF)
    elif reg == 6:
        cpu.push(cpu.op_read(rm), sz)
    else:
        raise AssertionError(f"0xFF /{reg} is undefined")


# ------------------------------------------------------------------- returns

@op(0xC3)
def ret(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_ip(cpu.pop(16))


@op(0xC2)
def ret_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    n = cpu.fetch16()
    cpu.set_ip(cpu.pop(16))
    cpu.regs[REG_SP] = (cpu.regs[REG_SP] + n) & 0xFFFF


@op(0xCB)
def retf(opcode: int, cpu: CPU, d: Decode) -> None:
    ip = cpu.pop(16)
    cs = cpu.pop(16)
    cpu.set_cs_ip(cs, ip)


@op(0xCA)
def retf_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    n = cpu.fetch16()
    ip = cpu.pop(16)
    cs = cpu.pop(16)
    cpu.set_cs_ip(cs, ip)
    cpu.regs[REG_SP] = (cpu.regs[REG_SP] + n) & 0xFFFF


# ------------------------------------------------------------------- shifts

_SHIFT_KINDS = {0: "rol", 1: "ror", 2: "rcl", 3: "rcr", 4: "shl", 5: "shr",
                6: "shl", 7: "sar"}


def _do_shift(cpu: CPU, kind: str, value: int, count: int, size: int) -> int:
    count &= 0x1F
    if count == 0:
        return value
    bits = size
    mask = (1 << bits) - 1
    sign = 1 << (bits - 1)
    if kind in ("shl", "sal"):
        res = (value << count) & mask
        if count <= bits:
            cpu.set_flag(CF, bool((value >> (bits - count)) & 1))
            cpu.set_flag(OF, bool((res ^ value) & sign))
    elif kind == "shr":
        cpu.set_flag(CF, bool((value >> (count - 1)) & 1))
        res = (value & mask) >> count
        if count == 1:
            cpu.set_flag(OF, bool(value & sign))
    elif kind == "sar":
        sv = value - (1 << bits) if value & sign else value
        cpu.set_flag(CF, bool((sv >> (count - 1)) & 1))
        res = (sv >> count) & mask
        cpu.set_flag(OF, False)
    elif kind == "rol":
        n = count % bits
        res = value if n == 0 else (((value << n) | (value >> (bits - n))) & mask)
        if n:
            cpu.set_flag(CF, bool(res & 1))
            cpu.set_flag(OF, bool((res ^ (1 << (bits - 1))) & sign))
    elif kind == "ror":
        n = count % bits
        res = value if n == 0 else (((value >> n) | (value << (bits - n))) & mask)
        if n:
            cpu.set_flag(CF, bool(res & sign))
            cpu.set_flag(OF, bool(((res ^ (1 << (bits - 1))) & mask) & sign))
    elif kind in ("rcl", "rcr"):
        res = value
        for _ in range(count):
            cf_in = cpu.flag(CF)
            if kind == "rcl":
                new_cf = bool(value & sign)
                res = ((res << 1) | cf_in) & mask
            else:
                new_cf = bool(res & 1)
                res = (res >> 1) | (cf_in << (bits - 1))
            cpu.set_flag(CF, new_cf)
    else:
        raise AssertionError(kind)

    if count:
        cpu.set_flag(ZF, res == 0)
        cpu.set_flag(SF, bool(res & sign))
        cpu.set_flag(PF, PARITY_TABLE[res & 0xFF])
        if kind not in ("rol", "ror", "rcl", "rcr"):
            cpu.set_flag(AF, False)
    return res


def _register_shifts() -> None:
    def make(opcode: int, size: int, count_from_cl: bool, imm8: bool) -> Handler:
        def handler(opcode: int, cpu: CPU, d: Decode) -> None:
            rm, reg = cpu.decode_modrm(d, size)
            if imm8:
                count = cpu.fetch8()
            elif count_from_cl:
                count = cpu.get_reg(REG_CX, 8)
            else:
                count = 1
            kind = _SHIFT_KINDS[reg]
            cpu.op_write(rm, _do_shift(cpu, kind, cpu.op_read(rm), count, size))
        return handler

    OPS[0xD0] = make(0xD0, 8, False, False)
    OPS[0xD1] = make(0xD1, 16, False, False)
    OPS[0xD2] = make(0xD2, 8, True, False)
    OPS[0xD3] = make(0xD3, 16, True, False)
    OPS[0xC0] = make(0xC0, 8, False, True)
    OPS[0xC1] = make(0xC1, 16, False, True)


# -------------------------------------------------------- control / misc

@op(0x90)
def nop(opcode: int, cpu: CPU, d: Decode) -> None:
    return


@op(0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97)
def xchg_acc_reg(opcode: int, cpu: CPU, d: Decode) -> None:
    reg = opcode - 0x90
    sz = d.opsize
    tmp = cpu.get_reg(REG_AX, sz)
    cpu.set_reg(REG_AX, sz, cpu.get_reg(reg, sz))
    cpu.set_reg(reg, sz, tmp)


@op(0x86, 0x87)
def xchg_rm_reg(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = 8 if opcode == 0x86 else d.opsize
    rm, reg = cpu.decode_modrm(d, sz)
    regop = _operand_from_reg(reg, sz)
    a = cpu.op_read(rm)
    b = cpu.op_read(regop)
    cpu.op_write(rm, b)
    cpu.op_write(regop, a)


@op(0x84, 0x85)
def test_rm_reg(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = 8 if opcode == 0x84 else d.opsize
    rm, reg = cpu.decode_modrm(d, sz)
    cpu._flags_logic(cpu.op_read(rm) & cpu.get_reg(reg, sz), sz)


@op(0xA8, 0xA9)
def test_acc_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    """`test al, imm8` / `test ax, imm16`.

    The accumulator forms are separate opcodes from the 0x84/0x85 ModRM forms and
    were simply missing.  They matter because `test al, 0x20` is the idiomatic way
    to poll a status port, so a driver hits this before anything else does.
    """
    sz = 8 if opcode == 0xA8 else d.opsize
    value = cpu.fetch_i(sz)
    cpu._flags_logic(cpu.get_reg(REG_AX, sz) & value, sz)


@op(0x98)
def cbw(opcode: int, cpu: CPU, d: Decode) -> None:
    al = cpu.get_reg(REG_AX, 8)
    cpu.set_reg(REG_AX, 16, (al - 0x100) & 0xFFFF if al & 0x80 else al)


@op(0x99)
def cwd(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.regs[REG_DX] = 0xFFFF if cpu.get_reg(REG_AX, 16) & 0x8000 else 0


@op(0x9C)
def pushf(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.push(cpu.flags, 16)


@op(0x9D)
def popf(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.flags = (cpu.pop(16) | 0x0002) & 0xFFFF


@op(0x9E)
def sahf(opcode: int, cpu: CPU, d: Decode) -> None:
    ah = cpu.get_reg(4, 8)
    cpu.flags = (cpu.flags & 0xFF00) | (ah & 0xD5) | 0x02


@op(0x9F)
def lahf(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_reg(4, 8, cpu.flags & 0xFF)


@op(0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57)
def push_reg(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.push(cpu.get_reg(opcode - 0x50, d.opsize), d.opsize)


@op(0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F)
def pop_reg(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_reg(opcode - 0x58, d.opsize, cpu.pop(d.opsize))


@op(0x06, 0x0E, 0x16, 0x1E)
def push_sreg(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.push(cpu.sregs[(opcode >> 3) & 3], 16)


@op(0x07, 0x17, 0x1F)
def pop_sreg(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_sreg((opcode >> 3) & 3, cpu.pop(16))


@op(0x60)
def pusha(opcode: int, cpu: CPU, d: Decode) -> None:
    sp = cpu.regs[REG_SP]
    for r in range(8):
        cpu.push(cpu.regs[r] if r != REG_SP else sp, 16)


@op(0x61)
def popa(opcode: int, cpu: CPU, d: Decode) -> None:
    for r in (7, 6, 5, 4, 3, 2, 1, 0):
        value = cpu.pop(16)
        if r != REG_SP:
            cpu.regs[r] = value


@op(0x68, 0x6A)
def push_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    value = cpu.fetch_s8() & 0xFFFF if opcode == 0x6A else cpu.fetch_i(d.opsize) & 0xFFFF
    cpu.push(value, d.opsize)


@op(0x69, 0x6B)
def imul_rm_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = d.opsize
    rm, reg = cpu.decode_modrm(d, sz)
    imm = cpu.fetch_i(sz) if opcode == 0x69 else cpu.fetch_s8() & ((1 << sz) - 1)
    b = imm - (1 << sz) if imm & (1 << (sz - 1)) else imm
    res = cpu.op_read_signed(rm) * b
    fits = -(1 << (sz - 1)) <= res < (1 << (sz - 1))
    cpu.set_flag(CF, not fits)
    cpu.set_flag(OF, not fits)
    cpu.set_reg(reg, sz, res)


@op(0x8F)
def pop_rm(opcode: int, cpu: CPU, d: Decode) -> None:
    rm, _ = cpu.decode_modrm(d, d.opsize)
    cpu.op_write(rm, cpu.pop(d.opsize))


# ------------------------------------------------------------ group 3 / 0F

@op(0xF6, 0xF7)
def group3(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = 8 if opcode == 0xF6 else d.opsize
    rm, reg = cpu.decode_modrm(d, sz)
    value = cpu.op_read(rm)
    if reg in (0, 1):
        imm = cpu.fetch8() if sz == 8 else cpu.fetch_i(sz)
        cpu._flags_logic(value & imm, sz)
        return
    if reg == 2:
        cpu.op_write(rm, ~value)
        return
    if reg == 3:
        cpu.op_write(rm, cpu._flags_sub(0, value, sz))
        return
    if reg == 4:                                  # mul
        if sz == 8:
            res = cpu.get_reg(REG_AX, 8) * value
            cpu.set_reg(REG_AX, 16, res & 0xFFFF)
            overflow = res > 0xFF
        else:
            res = cpu.get_reg(REG_AX, 16) * value
            cpu.regs[REG_AX] = res & 0xFFFF
            cpu.regs[REG_DX] = (res >> 16) & 0xFFFF
            overflow = res > 0xFFFF
        cpu.set_flag(CF, overflow)
        cpu.set_flag(OF, overflow)
        return
    if reg == 5:                                  # imul, one operand
        if sz == 8:
            a = cpu.get_reg(REG_AX, 8)
            a = a - 0x100 if a & 0x80 else a
            b = value - 0x100 if value & 0x80 else value
            res = a * b
            cpu.set_reg(REG_AX, 16, res & 0xFFFF)
            fits = -0x80 <= res <= 0x7F
        else:
            a = cpu.get_reg(REG_AX, 16)
            a = a - 0x10000 if a & 0x8000 else a
            b = value - 0x10000 if value & 0x8000 else value
            res = a * b
            cpu.regs[REG_AX] = res & 0xFFFF
            cpu.regs[REG_DX] = (res >> 16) & 0xFFFF
            fits = -0x8000 <= res <= 0x7FFF
        cpu.set_flag(CF, not fits)
        cpu.set_flag(OF, not fits)
        return
    if reg == 6:                                  # div
        if value == 0:
            raise DivideError(cpu.sregs[SEG_CS], cpu.ip16, "division by zero")
        if sz == 8:
            q, r = divmod(cpu.get_reg(REG_AX, 16), value)
            if q > 0xFF:
                raise DivideError(cpu.sregs[SEG_CS], cpu.ip16, "quotient overflow")
            cpu.set_reg(REG_AX, 8, q)
            cpu.set_reg(4, 8, r)
        else:
            num = cpu.get_reg(REG_AX, 16) | (cpu.regs[REG_DX] << 16)
            q, r = divmod(num, value)
            if q > 0xFFFF:
                raise DivideError(cpu.sregs[SEG_CS], cpu.ip16, "quotient overflow")
            cpu.regs[REG_AX] = q
            cpu.regs[REG_DX] = r
        return
    if reg == 7:                                  # idiv
        if value == 0:
            raise DivideError(cpu.sregs[SEG_CS], cpu.ip16, "division by zero")
        if sz == 8:
            num = cpu.get_reg(REG_AX, 16)
            num = num - 0x10000 if num & 0x8000 else num
            b = value - 0x100 if value & 0x80 else value
            q = int(num / b)
            r = num - q * b
            if not (-0x80 <= q <= 0x7F):
                raise DivideError(cpu.sregs[SEG_CS], cpu.ip16, "quotient overflow")
            cpu.set_reg(REG_AX, 8, q & 0xFF)
            cpu.set_reg(4, 8, r & 0xFF)
        else:
            num = cpu.get_reg(REG_AX, 16) | (cpu.regs[REG_DX] << 16)
            num = num - 0x100000000 if num & 0x80000000 else num
            b = value - 0x10000 if value & 0x8000 else value
            q = int(num / b)
            r = num - q * b
            if not (-0x8000 <= q <= 0x7FFF):
                raise DivideError(cpu.sregs[SEG_CS], cpu.ip16, "quotient overflow")
            cpu.regs[REG_AX] = q & 0xFFFF
            cpu.regs[REG_DX] = r & 0xFFFF
        return
    raise AssertionError(f"group-3 /{reg} undefined")


@op0f(0xB6, 0xB7, 0xBE, 0xBF)
def movzx_movsx(opcode: int, cpu: CPU, d: Decode) -> None:
    """0F B6/B7 movzx r32, r/m8/r/m16  and  0F BE/BF movsx r32, r/m8/r/m16.

    The source size comes from the opcode; the destination size comes from the
    operand-size prefix.  The kernel needs the 32-bit destination forms because
    it reaches data through 32-bit base registers, so `movzx ebx, bx` is common.
    """
    src_size = 8 if opcode in (0xB6, 0xBE) else 16
    signed = opcode in (0xBE, 0xBF)
    rm, reg = cpu.decode_modrm(d, src_size)
    value = cpu.op_read(rm)
    if signed and value & (1 << (src_size - 1)):
        value -= 1 << src_size
    cpu.set_reg(reg, d.opsize, value & ((1 << d.opsize) - 1))


@op0f(0xAF)
def imul_rm(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = d.opsize
    rm, reg = cpu.decode_modrm(d, sz)
    a = cpu.get_reg(reg, sz)
    a = a - (1 << sz) if a & (1 << (sz - 1)) else a
    res = a * cpu.op_read_signed(rm)
    fits = -(1 << (sz - 1)) <= res < (1 << (sz - 1))
    cpu.set_flag(CF, not fits)
    cpu.set_flag(OF, not fits)
    cpu.set_reg(reg, sz, res)


# --------------------------------------------------------------- jumps/loops

def _jump_relative(cpu: CPU, disp: int) -> None:
    cpu._csip = (cpu._csip + disp) & 0xFFFFF
    cpu.ip16 = (cpu._csip - cpu._csbase) & 0xFFFF


def _register_jcc() -> None:
    conds = {
        0x70: lambda c: c.flag(OF) == 1, 0x71: lambda c: c.flag(OF) == 0,
        0x72: lambda c: c.flag(CF) == 1, 0x73: lambda c: c.flag(CF) == 0,
        0x74: lambda c: c.flag(ZF) == 1, 0x75: lambda c: c.flag(ZF) == 0,
        0x76: lambda c: c.flag(CF) == 1 or c.flag(ZF) == 1,
        0x77: lambda c: c.flag(CF) == 0 and c.flag(ZF) == 0,
        0x78: lambda c: c.flag(SF) == 1, 0x79: lambda c: c.flag(SF) == 0,
        0x7A: lambda c: c.flag(PF) == 1, 0x7B: lambda c: c.flag(PF) == 0,
        0x7C: lambda c: c.flag(SF) != c.flag(OF),
        0x7D: lambda c: c.flag(SF) == c.flag(OF),
        0x7E: lambda c: c.flag(ZF) == 1 or c.flag(SF) != c.flag(OF),
        0x7F: lambda c: c.flag(ZF) == 0 and c.flag(SF) == c.flag(OF),
    }
    for opcode, cond in conds.items():
        def make(cond: Callable[[CPU], bool]) -> Handler:
            def handler(op: int, cpu: CPU, d: Decode) -> None:
                disp = cpu.fetch_s8()
                if cond(cpu):
                    _jump_relative(cpu, disp)
            return handler
        OPS[opcode] = make(cond)

    conds16 = {
        0x80: conds[0x70], 0x81: conds[0x71], 0x82: conds[0x72], 0x83: conds[0x73],
        0x84: conds[0x74], 0x85: conds[0x75], 0x86: conds[0x76], 0x87: conds[0x77],
        0x88: conds[0x78], 0x89: conds[0x79], 0x8A: conds[0x7A], 0x8B: conds[0x7B],
        0x8C: conds[0x7C], 0x8D: conds[0x7D], 0x8E: conds[0x7E], 0x8F: conds[0x7F],
    }
    for opcode, cond in conds16.items():
        def make(cond: Callable[[CPU], bool]) -> Handler:
            def handler(op: int, cpu: CPU, d: Decode) -> None:
                disp = cpu.fetch_s16()
                if cond(cpu):
                    _jump_relative(cpu, disp)
            return handler
        OPS0F[opcode] = make(cond)


@op(0xEB)
def jmp_short(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.jump_rel(8)


@op(0xE9)
def jmp_near(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.jump_rel(16)


@op(0xEA)
def jmp_far(opcode: int, cpu: CPU, d: Decode) -> None:
    # Encoding: EA <offset> <segment16>.  The offset is 16 bits by default and
    # 32 bits with the 0x66 operand-size prefix.  Reading it as 32 bits
    # unconditionally consumes two bytes of the segment and jumps to an address
    # assembled from unrelated instruction bytes, which is exactly the kind of
    # failure that looks like a broken disk read.
    off = cpu.fetch32() if d.opsize == 32 else cpu.fetch16()
    seg = cpu.fetch16()
    cpu.set_cs_ip(seg, off)


@op(0xE8)
def call_near(opcode: int, cpu: CPU, d: Decode) -> None:
    disp = cpu.fetch_s16()
    cpu.push(cpu.ip16, 16)
    _jump_relative(cpu, disp)


@op(0x9A)
def call_far(opcode: int, cpu: CPU, d: Decode) -> None:
    # Same encoding rules as 0xEA: offset is 16 bits, or 32 with the 0x66 prefix.
    off = cpu.fetch32() if d.opsize == 32 else cpu.fetch16()
    seg = cpu.fetch16()
    cpu.push(cpu.sregs[SEG_CS], 16)
    cpu.push(cpu.ip16, 16)
    cpu.set_cs_ip(seg, off)


def _loop_common(cpu: CPU, disp: int, dec_cx: bool, cond: Callable[[CPU], bool]) -> None:
    if dec_cx:
        cpu.regs[REG_CX] = (cpu.regs[REG_CX] - 1) & 0xFFFF
    if (not dec_cx or cpu.regs[REG_CX] != 0) and cond(cpu):
        _jump_relative(cpu, disp)


@op(0xE0)
def loopne(opcode: int, cpu: CPU, d: Decode) -> None:
    disp = cpu.fetch_s8()
    _loop_common(cpu, disp, True, lambda c: c.flag(ZF) == 0)


@op(0xE1)
def loope(opcode: int, cpu: CPU, d: Decode) -> None:
    disp = cpu.fetch_s8()
    _loop_common(cpu, disp, True, lambda c: c.flag(ZF) == 1)


@op(0xE2)
def loop(opcode: int, cpu: CPU, d: Decode) -> None:
    disp = cpu.fetch_s8()
    _loop_common(cpu, disp, True, lambda c: True)


@op(0xE3)
def jcxz(opcode: int, cpu: CPU, d: Decode) -> None:
    disp = cpu.fetch_s8()
    _loop_common(cpu, disp, False, lambda c: c.regs[REG_CX] == 0)


# ---------------------------------------------------------------- interrupts

@op(0xCD)
def int_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.raise_int(cpu.fetch8())


@op(0xCC)
def int3(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.raise_int(3)


@op(0xCE)
def into(opcode: int, cpu: CPU, d: Decode) -> None:
    if cpu.flag(OF):
        cpu.raise_int(4)


@op(0xCF)
def iret(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.do_iret()


@op(0xFA)
def cli(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(IF, False)


@op(0xFB)
def sti(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(IF, True)


@op(0xF4)
def hlt(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.halted = True


@op(0xFC)
def cld(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(DF, False)


@op(0xFD)
def std(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(DF, True)


@op(0xF8)
def clc(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(CF, False)


@op(0xF9)
def stc(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(CF, True)


@op(0xF5)
def cmc(opcode: int, cpu: CPU, d: Decode) -> None:
    cpu.set_flag(CF, not cpu.flag(CF))


# ------------------------------------------------------------------- IO ports

@op(0xE4, 0xE5)
def in_acc_imm(opcode: int, cpu: CPU, d: Decode) -> None:
    port = cpu.fetch8()
    sz = 8 if opcode == 0xE4 else d.opsize
    cpu.set_reg(REG_AX, sz, cpu.port_in(port, sz))


@op(0xEC, 0xED)
def in_acc_dx(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = 8 if opcode == 0xEC else d.opsize
    cpu.set_reg(REG_AX, sz, cpu.port_in(cpu.regs[REG_DX], sz))


@op(0xE6, 0xE7)
def out_imm_acc(opcode: int, cpu: CPU, d: Decode) -> None:
    port = cpu.fetch8()
    sz = 8 if opcode == 0xE6 else d.opsize
    cpu.port_out(port, sz, cpu.get_reg(REG_AX, sz))


@op(0xEE, 0xEF)
def out_dx_acc(opcode: int, cpu: CPU, d: Decode) -> None:
    sz = 8 if opcode == 0xEE else d.opsize
    cpu.port_out(cpu.regs[REG_DX], sz, cpu.get_reg(REG_AX, sz))


# ------------------------------------------------------------------- strings

def _string_op(opcode: int, cpu: CPU, d: Decode) -> None:
    # The byte forms (A4/AA/AC/AE) are bytes no matter what the operand-size
    # prefix says; only the "word" forms (A5/AB/AD/AF) follow d.opsize, which is
    # 16 by default and 32 under a 0x66 prefix.  Taking d.opsize for all of them
    # made `lodsb` read a word and step SI by two, so every string printed with
    # the classic lodsb loop came out with every other character missing --
    # which looked like a corrupt string, not like an emulator fault.
    size = 8 if opcode in (0xA4, 0xAA, 0xAC, 0xAE) else d.opsize
    width = size // 8
    delta = (-width) if cpu.flag(DF) else width
    src_seg = d.seg if d.seg is not None else SEG_DS
    rep = d.rep is not None

    def step_si() -> None:
        cpu.regs[REG_SI] = (cpu.regs[REG_SI] + delta) & 0xFFFF

    def step_di() -> None:
        cpu.regs[REG_DI] = (cpu.regs[REG_DI] + delta) & 0xFFFF

    def dec_cx() -> bool:
        cpu.regs[REG_CX] = (cpu.regs[REG_CX] - 1) & 0xFFFF
        return cpu.regs[REG_CX] != 0

    if opcode in (0xA4, 0xA5):                     # movs
        while True:
            cpu.wr_mem(cpu.a(cpu.sregs[SEG_ES], cpu.regs[REG_DI]), width,
                       cpu.rd_mem(cpu.a(cpu.sregs[src_seg], cpu.regs[REG_SI]), width))
            step_si()
            step_di()
            if not rep or not dec_cx():
                break
        return
    if opcode in (0xAA, 0xAB):                     # stos
        while True:
            cpu.wr_mem(cpu.a(cpu.sregs[SEG_ES], cpu.regs[REG_DI]), width,
                       cpu.get_reg(REG_AX, size))
            step_di()
            if not rep or not dec_cx():
                break
        return
    if opcode in (0xAC, 0xAD):                     # lods
        while True:
            cpu.set_reg(REG_AX, size,
                        cpu.rd_mem(cpu.a(cpu.sregs[src_seg], cpu.regs[REG_SI]), width))
            step_si()
            if not rep or not dec_cx():
                break
        return
    if opcode in (0xAE, 0xAF):                     # scas
        while True:
            value = cpu.rd_mem(cpu.a(cpu.sregs[SEG_ES], cpu.regs[REG_DI]), width)
            step_di()
            cpu._flags_sub(cpu.get_reg(REG_AX, size), value, size)
            if not rep:
                break
            if not dec_cx():
                break
            if d.rep == 0xF3 and cpu.flag(ZF) == 0:
                break
            if d.rep == 0xF2 and cpu.flag(ZF) == 1:
                break
        return
    if opcode in (0xA6, 0xA7):                     # cmps
        while True:
            a = cpu.rd_mem(cpu.a(cpu.sregs[src_seg], cpu.regs[REG_SI]), width)
            b = cpu.rd_mem(cpu.a(cpu.sregs[SEG_ES], cpu.regs[REG_DI]), width)
            step_si()
            step_di()
            cpu._flags_sub(a, b, d.opsize)
            if not rep:
                break
            if not dec_cx():
                break
            if d.rep == 0xF3 and cpu.flag(ZF) == 0:
                break
            if d.rep == 0xF2 and cpu.flag(ZF) == 1:
                break
        return
    raise AssertionError(f"string opcode {opcode:#x}")


# ------------------------------------------------------------------ assembly

def build_tables() -> tuple[Dict[int, Handler], Dict[int, Handler]]:
    """Wire up every handler and return (one_byte_table, 0f_table).

    Order matters: the group-1 opcodes 0x84..0x8F are registered first and the
    single-instruction handlers (test 0x84/0x85, xchg 0x86/0x87, mov 0x88..0x8F)
    are installed afterwards so that they win.  Doing it the other way round
    silently replaces `test` and `xchg` with group-1 handlers, which is exactly
    the kind of bug that is invisible until a real program runs.
    """
    _register_group1()
    _register_arith_family()
    _register_mov()
    _register_incdec()
    _register_shifts()
    _register_jcc()

    for opcode in (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
        OPS[opcode] = _string_op
    return OPS, OPS0F
