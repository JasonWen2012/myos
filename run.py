#!/usr/bin/env python3
"""
Run a myos image.

Backends:
  sim   the in-tree 16-bit emulator.  Always available, fully deterministic, and
        what every automated test uses.
  qemu  qemu-system-i386, for real-firmware cross-checking.  Optional.

Usage:
    python run.py                    boot the floppy image and print the screen
    python run.py --interactive      type into the running kernel (this is the
                                     interesting one: your keyboard feeds the OS)
    python run.py --screenshot       also write images/screen.png
    python run.py --dump             print the raw screen and kernel state
    python run.py --backend qemu     boot under QEMU instead
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build as myos_build  # noqa: E402
from emulator import screen as screen_render  # noqa: E402
from tools import myfs, qemu, toolchain  # noqa: E402

DEFAULT_STEPS = 400_000
# The simulator takes an instruction budget; QEMU takes a clock.  A user-facing run
# should not be cut short, so the bound is generous and only guards against a
# forgotten window.
QEMU_TIMEOUT_SECONDS = 600

# Interactive tuning.  The emulator is a pure-Python interpreter, so the speed is
# set by how many instructions run between screen updates: too few and it crawls,
# too many and keystrokes feel laggy.
INSTRUCTIONS_PER_FRAME = 30_000
FRAME_DELAY = 0.010

ANSI = {
    "clear": "\x1b[2J",
    "home": "\x1b[H",
    "reset": "\x1b[0m",
    "hide": "\x1b[?25l",
    "show": "\x1b[?25h",
    "fg": lambda n: f"\x1b[38;5;{n}m",
    "bg": lambda n: f"\x1b[48;5;{n}m",
}

# The 16 CGA colours as 256-colour terminal indices, which every modern terminal
# supports and which keeps the mapping readable.
CGA_TO_ANSI256 = (0, 4, 2, 6, 1, 5, 3, 7, 8, 12, 10, 14, 9, 13, 11, 15)


def _enable_raw_console() -> str:
    """Put the terminal in raw mode so keystrokes arrive one at a time.

    Returns a platform tag so the reader knows which API to use.  Under a pipe
    (as in the tests) this is skipped: the input is a script, not a keyboard, and
    stdin will simply hit EOF when it runs out.
    """
    if not sys.stdin.isatty():
        return "pipe"
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)                 # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        # Drop line input and echo; keep the window and processed-output bits so
        # that Ctrl+C still behaves.
        kernel32.SetConsoleMode(handle, (mode.value & ~0x0006) | 0x0200)
        return "win"
    import termios
    import tty

    fd = sys.stdin.fileno()
    termios.tcgetattr(fd)
    tty.setraw(fd)
    return "posix"


def _restore_console(tag: str) -> None:
    if tag == "win":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0006)
    elif tag == "posix":
        import termios

        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] |= termios.ICANON | termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)


class _KeyReader:
    """Reads keystrokes without blocking, in whatever mode the terminal is in."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self._buffer = ""

    def drain(self) -> str:
        if self.tag == "win":
            import msvcrt

            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):                  # extended key prefix
                    msvcrt.getwch()
                    continue
                self._buffer += ch
        elif self.tag == "posix":
            import select

            while select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                if not ch:
                    break
                self._buffer += ch
        else:
            chunk = sys.stdin.read(1)
            if chunk:
                self._buffer += chunk
        text, self._buffer = self._buffer, ""
        return text


# Named keys the kernel's BIOS key queue understands, as escape sequences.
ESCAPE_KEYS = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1b[2~": "insert", "\x1b[3~": "delete",
    "\x1b[5~": "pageup", "\x1b[6~": "pagedown",
    "\x1bOP": "f1", "\x1bOQ": "f2", "\x1bOR": "f3", "\x1bOS": "f4",
}


def _feed(machine, text: str) -> bool:
    """Queue terminal input into the emulated keyboard.  False means quit."""
    i = 0
    while i < len(text):
        ch = text[i]
        # Longest-match the escape sequences before treating ESC as a quit key.
        for sequence, key_name in ESCAPE_KEYS.items():
            if text.startswith(sequence, i):
                machine.press_key(key_name)
                i += len(sequence)
                break
        else:
            if ch == "\x1b":
                return False
            if ch in ("\x03", "\x04"):                      # Ctrl+C / Ctrl+D
                return False
            if ch in ("\r", "\n"):
                # Both forms are Enter.  A terminal in raw mode sends CR, but a
                # pipe gives LF because Python translates newlines while reading
                # text, and dropping LF made "type a line and press Enter" feed
                # every character except the one that ends the line.
                machine.press_key("enter")
            elif ch in ("\x08", "\x7f"):
                machine.press_key("backspace")
            elif ch == "\t":
                machine.press_key("tab")
            elif ch.isprintable():
                machine.type_text(ch)
            i += 1
    return True


def _render(machine, previous: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Write only the cells that changed, so the display does not flicker.

    Colours follow the VGA attribute byte, so the kernel's own colour choices
    (banner, prompts, errors) show up in the terminal exactly as on screen.
    """
    memory = machine.cpu.mem
    out = [ANSI["hide"], ANSI["home"]]
    current: list[tuple[int, int]] = []
    last_fg = last_bg = None
    for row in range(screen_render.ROWS):
        base = screen_render.VIDEO_BASE + row * screen_render.COLS * 2
        for col in range(screen_render.COLS):
            code = memory[base + col * 2]
            attr = memory[base + col * 2 + 1]
            current.append((code, attr))
            index = row * screen_render.COLS + col
            if index < len(previous) and previous[index] == (code, attr):
                continue
            out.append(f"\x1b[{row + 1};{col + 1}H")
            fg = CGA_TO_ANSI256[attr & 0x0F]
            bg = CGA_TO_ANSI256[(attr >> 4) & 0x07]
            if fg != last_fg:
                out.append(ANSI["fg"](fg))
                last_fg = fg
            if bg != last_bg:
                out.append(ANSI["bg"](bg))
                last_bg = bg
            out.append(chr(code) if 32 <= code < 127 else " ")

    # The kernel writes the video buffer directly and never calls INT 10h, so the
    # BIOS cursor word stays at 0.  Placing the terminal cursor after the last
    # written cell gives the same result and needs no kernel-specific knowledge.
    cursor_row, cursor_col = 0, 0
    for row in range(screen_render.ROWS):
        base = screen_render.VIDEO_BASE + row * screen_render.COLS * 2
        for col in range(screen_render.COLS):
            if memory[base + col * 2] not in (0, 0x20):
                cursor_row, cursor_col = row, min(col + 1, screen_render.COLS - 1)

    out.append(ANSI["reset"])
    out.append(f"\x1b[{cursor_row + 1};{cursor_col + 1}H")
    out.append(ANSI["show"])
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    return current


def _run_interactive(image: Path, steps: int) -> int:
    from emulator.machine import Machine

    machine = Machine()
    machine.load_image(image)
    machine.boot()

    tag = _enable_raw_console()
    interactive = tag != "pipe"
    print(ANSI["clear"] + ANSI["home"], end="")
    if interactive:
        print("myos interactive -- your keystrokes go to the kernel. "
              "Esc or Ctrl+C quits.")
    else:
        print("myos interactive -- stdin is not a terminal, feeding it as input.")
    time.sleep(0.6)

    previous: list[tuple[int, int]] = []
    try:
        while True:
            machine.run_until_idle(max_steps=INSTRUCTIONS_PER_FRAME,
                                   idle_polls=INSTRUCTIONS_PER_FRAME)
            previous = _render(machine, previous)
            if machine.cpu.halted and not machine.input_wait_seen:
                print(ANSI["reset"] + "\r\nkernel halted: " + (machine.halt_reason or ""))
                return 0
            if interactive:
                text = _KeyReader(tag).drain()
                if text and not _feed(machine, text):
                    return 0
                time.sleep(FRAME_DELAY)
            else:
                text = _KeyReader(tag).drain()
                if not text:
                    return 0                 # stdin exhausted: script finished
                _feed(machine, text)
    finally:
        _restore_console(tag)
        print(ANSI["reset"] + ANSI["show"], end="")
        print()


def _boot_with_simulator(image: Path, steps: int):
    from emulator.machine import Machine

    machine = Machine()
    machine.load_image(image)
    machine.boot()
    # run_until_idle, not run: a kernel with a shell in it never halts, it polls
    # INT 16h waiting for a key.  Running to the step budget would print a screen
    # that is correct and a reason ("step budget exhausted") that is alarming.
    reason = machine.run_until_idle(max_steps=steps, idle_polls=min(steps, 200_000))
    return machine, reason


def ensure_data_disk() -> Path:
    """The user's own disk, created once and then left alone.

    It is attached to the ATA controller as the primary master, so the kernel's
    filesystem has somewhere to live that survives a reboot and a rebuild.  It is
    deliberately not one of the images a build produces: `build.py all` rewrites
    those, and a rebuild that deletes what you saved is not a filesystem.
    """
    disk = myos_build.DATA_DISK
    if disk.is_file():
        return disk
    volume = myfs.write_data_disk(disk, files_dir=myos_build.FILES_DIR)
    print(f"created {disk} ({disk.stat().st_size} bytes, "
          f"{volume.free_blocks} free blocks) -- your files live here")
    return disk


def data_disk_arguments(disk: Path) -> list[str]:
    """The QEMU arguments that attach a disk as the primary ATA master."""
    return qemu.data_disk_arguments(disk)


def _boot_with_qemu(image: Path, tc: toolchain.Toolchain, steps: int) -> int:
    """Boot under QEMU, with a window and the guest's COM1 on this terminal.

    The 32-bit kernel cannot run in the in-tree emulator, so this is the backend
    that matters for it.  A display is requested on purpose: the kernel draws on
    the VGA screen, and the serial port carries the same text to the terminal, so
    both are visible at once.  No isa-debug-exit device is attached here -- a
    self-test ends the run by writing to that port, and for an interactive session
    the kernel should keep its screen instead.

    A 32-bit image also gets the data disk, because without it there is no
    filesystem to mount -- and a kernel with no disk is a kernel whose `ls` can
    only say why it cannot work.
    """
    import subprocess

    qemu_path = qemu.find(tc)
    if qemu_path is None:
        print("qemu-system-i386 was not found; install QEMU or use --backend sim",
              file=sys.stderr)
        return 2
    extra: list[str] = []
    if image_architecture(image) == 2:
        extra = data_disk_arguments(ensure_data_disk())
    command = qemu.build_command(image, qemu=qemu_path, serial="stdio", display="gtk",
                                 debug_exit=False, monitor="none", extra=extra)
    print("running:", " ".join(command))
    print("(close the QEMU window to stop; the guest's serial output appears here)")
    try:
        proc = subprocess.run(command, timeout=QEMU_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"qemu is still running after {QEMU_TIMEOUT_SECONDS}s; stopping it.")
        return 0
    return proc.returncode


def _print_screen(machine) -> None:
    for index, line in enumerate(machine.screen_chars()):
        if line.strip():
            print(f"{index:2d}| {line.rstrip()}")


def image_architecture(image: Path) -> int | None:
    """The architecture byte of a myos kernel image, or None if this is not one.

    Read from the disk image rather than from the build arguments: the boot sector
    is at LBA 0, stage2 at LBA 1..8 and the kernel image -- whose first byte is the
    header -- at LBA 128.  Deciding the backend from what is actually on the disk is
    what keeps the 16-bit emulator from being asked to execute a 32-bit kernel.
    """
    try:
        with image.open("rb") as handle:
            handle.seek(myos_build.KERNEL_LBA * 512)
            header = handle.read(8)
    except OSError:
        return None
    if len(header) < 8 or header[:4] != b"MYOS":
        return None
    return header[4]


def default_image(arch: int) -> Path:
    return myos_build.IMAGES_DIR / f"myos{arch}.img"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run a myos image")
    parser.add_argument("image", nargs="?", type=Path, default=None)
    parser.add_argument("--backend", choices=("sim", "qemu", "auto"), default="auto",
                        help="sim is the in-tree 16-bit emulator; qemu runs protected "
                             "mode; auto picks by the image's architecture byte")
    parser.add_argument("--arch", type=int, default=16, choices=(16, 32))
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--screenshot", action="store_true",
                        help="render the final screen to images/screen.png")
    parser.add_argument("--interactive", action="store_true",
                        help="type into the running kernel; Esc or Ctrl+C quits")
    parser.add_argument("--dump", action="store_true",
                        help="also print registers and the machine state digest")
    parser.add_argument("--no-build", action="store_true",
                        help="use the existing image without rebuilding")
    args = parser.parse_args(argv)

    tc = toolchain.discover()

    if not args.no_build:
        try:
            myos_build.build(arch=args.arch)
        except (myos_build.BuildError, toolchain.ToolFailed) as exc:
            print(f"build failed: {exc}", file=sys.stderr)
            return 1

    image = args.image if args.image is not None else default_image(args.arch)
    if not image.is_file():
        print(f"no such image: {image}", file=sys.stderr)
        return 1

    arch = image_architecture(image)
    if arch is None:
        print(f"{image} does not look like a myos image (no kernel header at "
              f"LBA {myos_build.KERNEL_LBA})", file=sys.stderr)
        return 1

    backend = args.backend
    if backend == "auto":
        if arch == 2:
            backend = "qemu"
        else:
            backend = "sim"
        print(f"backend auto-selected: {backend} (image architecture {arch})")
    if backend == "sim" and arch != 1:
        print(f"the in-tree emulator is 16-bit real mode only, and this image is "
              f"{32 if arch == 2 else arch}-bit; use --backend qemu", file=sys.stderr)
        return 2
    if backend == "qemu" and not tc.qemu_available:
        print("qemu-system-i386 was not found; install QEMU or use --backend sim",
              file=sys.stderr)
        return 2
    if backend == "qemu" and args.screenshot:
        print("--screenshot renders the in-tree emulator's screen; for QEMU, use its "
              "own screendump command over the monitor", file=sys.stderr)

    if backend == "qemu":
        return _boot_with_qemu(image, tc, args.steps)

    if args.interactive:
        return _run_interactive(image, args.steps)

    machine, reason = _boot_with_simulator(image, args.steps)
    print(f"image      : {image}")
    print(f"reason     : {reason}")
    print(f"instructions: {machine.cpu.instructions}")
    print()
    _print_screen(machine)

    if args.dump:
        print()
        print("registers:", {k: hex(v) for k, v in machine.register_dump().items()})
        cursor_row, cursor_col = machine.cursor()
        print(f"bios cursor: row {cursor_row}, column {cursor_col}")

    if args.screenshot:
        out = myos_build.IMAGES_DIR / "screen.png"
        framebuffer = bytes(machine.cpu.mem[screen_render.VIDEO_BASE:
                                            screen_render.VIDEO_BASE
                                            + screen_render.COLS * screen_render.ROWS * 2])
        screen_render.render_png(framebuffer, out)
        print(f"screenshot : {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
