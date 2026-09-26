"""
Run a myos image under QEMU.

Why a whole module for this: the 16-bit kernel is verified in the in-tree
emulator, but the 32-bit one cannot be -- that emulator has a 16-bit register file
and no descriptor model, so a protected-mode kernel would need a second
interpreter.  QEMU provides real firmware (SeaBIOS: A20, E820, EDD) and a real
CPU, at the cost of needing a way to *read* what the guest says and a way to make
it stop.  Those are the two things this module provides:

  * the guest's console output goes to COM1 and is captured here, so a transcript
    of a headless run is the text the user would have seen on the screen;
  * QEMU's isa-debug-exit device turns a write to port 0xF4 into the process exit
    status (value << 1) | 1, so a self-test can end a run with a verdict instead
    of leaving the harness to time out.

Deliberately no dependency on anything outside the standard library, and QEMU is
optional: without it the callers skip rather than fail.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from . import toolchain

# The value the guest writes to port 0xF4, and the process status QEMU exits with:
# (value << 1) | 1.  Matches DEBUG_EXIT_* in boot/boot.inc.
DEBUG_EXIT_PORT = 0xF4
DEBUG_EXIT_PASS = 0x10
DEBUG_EXIT_FAIL = 0x02


def debug_exit_status(value: int) -> int:
    """The process exit status QEMU reports for writing `value` to port 0xF4."""
    return (value << 1) | 1


DEBUG_EXIT_PASS_STATUS = debug_exit_status(DEBUG_EXIT_PASS)
DEBUG_EXIT_FAIL_STATUS = debug_exit_status(DEBUG_EXIT_FAIL)


def find(tc: Optional[toolchain.Toolchain] = None) -> Optional[Path]:
    """Path to qemu-system-i386, or None when it is not installed."""
    tc = tc or toolchain.discover()
    tool = tc.tools.get("qemu-system-i386")
    return tool.path if tool is not None and tool.found else None


def available(tc: Optional[toolchain.Toolchain] = None) -> bool:
    return find(tc) is not None


def data_disk_arguments(disk: Path) -> list[str]:
    """The QEMU arguments that attach a disk as the primary ATA master.

    The kernel drives the primary channel of a legacy IDE controller, and that is
    where it looks for the partition holding its filesystem.  Booting from the
    floppy and hanging the data disk here is the configuration `run.py --arch 32`
    uses and the one the tests use; it is also how a machine can have a filesystem
    without the medium it booted from being the medium it writes to.
    """
    return ["-drive", f"file={disk},format=raw,if=ide,index=0"]


def build_command(image: Path, *, qemu: Path, serial: str = "stdio",
                  display: str = "none", memory_mb: int = 32,
                  debug_exit: bool = True, boot: str = "a",
                  monitor: Optional[str] = "none",
                  interrupt_log: Optional[Path] = None,
                  extra: Sequence[str] = ()) -> list[str]:
    """Assemble a QEMU command line.

    An argv list, never a shell string: the path to QEMU on Windows lives under
    "C:\\Program Files", and quoting rules that differ per shell are a bad thing
    to depend on.
    """
    command = [
        str(qemu),
        "-machine", "pc",
        "-m", str(memory_mb),
        "-drive", f"file={image},format=raw,if=floppy",
        "-boot", boot,
        "-display", display,
        "-serial", serial,
        "-no-reboot",
    ]
    if monitor is not None:
        command += ["-monitor", monitor]
    if debug_exit:
        command += ["-device", f"isa-debug-exit,iobase={DEBUG_EXIT_PORT:#x},iosize=0x04"]
    if interrupt_log is not None:
        command += ["-d", "int,cpu_reset", "-D", str(interrupt_log)]
    command += list(extra)
    return command


@dataclass
class QemuRun:
    """What a finished run produced."""

    status: Optional[int]          # process status, None if it had to be killed
    transcript: str                # everything the guest wrote to COM1
    timed_out: bool = False
    command: list[str] = field(default_factory=list)
    # Markers a scripted session waited for and never saw.  Reported rather than
    # ignored: a step whose marker never appeared still sends its text, so a silent
    # timeout turns a broken session into a confusing transcript instead of a
    # failure that names what was missing.
    unmet_waits: list[str] = field(default_factory=list)

    @property
    def passed_self_test(self) -> bool:
        return self.status == DEBUG_EXIT_PASS_STATUS


class QemuProcess:
    """A running QEMU with its serial port attached to this process.

    Two transports.  With `serial_port` the guest's COM1 is a TCP socket, which is
    what the tests use: a socket buffers and applies backpressure, while the stdio
    chardev fed by a pipe delivered the first byte of a burst and dropped the rest
    -- which looked exactly like a kernel that reads one character per interrupt.
    Without it, COM1 is this process's stdio, which is what a person wants.
    """

    def __init__(self, command: Sequence[str], serial_port: Optional[int] = None) -> None:
        self.command = list(command)
        self.serial_port = serial_port
        self._socket = None
        if serial_port is not None:
            self._process = subprocess.Popen(
                self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT, bufsize=0)
            self._socket = self._connect(serial_port)
        else:
            self._process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0)
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _connect(self, port: int):
        import socket

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=5)
                connection.settimeout(0.5)
                return connection
            except OSError:
                if self._process.poll() is not None:
                    raise RuntimeError(f"qemu exited with {self._process.returncode}")
                time.sleep(0.1)
        raise TimeoutError(f"the guest's serial port on {port} never accepted a connection")

    # ------------------------------------------------------------------- output

    def _read_loop(self) -> None:
        if self._socket is not None:
            import socket

            while True:
                try:
                    chunk = self._socket.recv(4096)
                except socket.timeout:
                    if self._process.poll() is not None:
                        break
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                with self._lock:
                    self._buffer += chunk
            return

        assert self._process.stdout is not None
        while True:
            try:
                chunk = os.read(self._process.stdout.fileno(), 4096)
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                self._buffer += chunk

    def transcript(self) -> str:
        with self._lock:
            return bytes(self._buffer).decode("utf-8", "replace")

    def wait_for(self, marker: str, timeout: float = 10.0) -> bool:
        """Wait until the guest has printed `marker`.  False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.transcript():
                return True
            if self._process.poll() is not None:
                return marker in self.transcript()
            time.sleep(0.02)
        return marker in self.transcript()

    def wait_for_exit(self, timeout: float = 30.0) -> bool:
        try:
            self._process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    # -------------------------------------------------------------------- input

    def send(self, text: str) -> None:
        """Type into the guest's serial port."""
        data = text.encode("utf-8")
        if self._socket is not None:
            self._socket.sendall(data)
            return
        if self._process.stdin is None:
            raise RuntimeError("this QEMU has no stdin")
        self._process.stdin.write(data)
        self._process.stdin.flush()

    # ----------------------------------------------------------------- shut down

    def status(self) -> Optional[int]:
        return self._process.poll()

    def alive(self) -> bool:
        return self._process.poll() is None

    def kill(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        for stream in (self._process.stdin, self._process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def run_scripted(image: Path, keys: str = "", *, timeout: float = 30.0,
                 qemu: Optional[Path] = None, keep_alive: float = 0.0,
                 interrupt_log: Optional[Path] = None,
                 extra: Sequence[str] = ()) -> QemuRun:
    """Boot `image`, optionally type `keys`, and wait for the guest to stop.

    `keys` is written once the guest has produced some output, which is what makes
    a scripted shell session deterministic instead of a race with BIOS POST.  The
    run ends when the guest exits through the debug port; if it does not, the
    process is killed and the transcript returned anyway, because a hung kernel's
    output is the evidence needed to fix it.
    """
    qemu = qemu or find()
    if qemu is None:
        raise FileNotFoundError("qemu-system-i386 was not found")
    command = build_command(image, qemu=qemu, interrupt_log=interrupt_log, extra=extra)
    process = QemuProcess(command)
    try:
        if keys:
            process.wait_for("", timeout=1.0)      # let the guest start talking
            process.send(keys)
        finished = process.wait_for_exit(timeout=timeout)
        if not finished and keep_alive:
            time.sleep(keep_alive)
            finished = process.wait_for_exit(timeout=1.0)
        timed_out = not finished
        status = process.status()
        if timed_out:
            process.kill()
            status = process.status()
        process.wait_for_exit(timeout=2.0)          # let the reader drain
        return QemuRun(status=status, transcript=process.transcript(),
                       timed_out=timed_out, command=command)
    finally:
        process.kill()
        process.close()


@dataclass
class SessionStep:
    """One step of a scripted session.

    `wait` is a marker that must appear in the guest's output first, and then
    either `serial` (text typed on COM1) or `keys` (key names pressed through the
    QEMU monitor, i.e. the real PS/2 path) is delivered.
    """

    wait: str
    serial: str = ""
    keys: tuple[str, ...] = ()
    timeout: float = 10.0


class _Monitor:
    """The QEMU monitor over TCP, used to press keys on the emulated keyboard."""

    def __init__(self, port: int) -> None:
        import socket

        self._socket = socket.create_connection(("127.0.0.1", port), timeout=10)
        self._socket.settimeout(2.0)
        time.sleep(0.3)
        self._drain()

    def _drain(self) -> str:
        import socket

        text = ""
        try:
            while True:
                chunk = self._socket.recv(65536)
                if not chunk:
                    break
                text += chunk.decode("utf-8", "replace")
        except (socket.timeout, OSError):
            pass
        return text

    def command(self, text: str) -> str:
        self._socket.sendall((text + "\n").encode())
        time.sleep(0.05)
        return self._drain()

    def send_key(self, name: str) -> None:
        self.command(f"sendkey {name}")

    def close(self) -> None:
        try:
            self.command("quit")
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass


def run_session(image: Path, steps: Sequence[SessionStep], *, timeout: float = 60.0,
                qemu: Optional[Path] = None, monitor_port: Optional[int] = None,
                serial_port: Optional[int] = None,
                extra: Sequence[str] = ()) -> QemuRun:
    """Boot the guest and drive it step by step.

    Typing everything at once does not work: the guest's UART is initialised, and
    its receive FIFO cleared, only once the kernel's drivers are up, so bytes sent
    before the prompt appears are simply gone -- which looks exactly like a shell
    that ignores input.  Each step therefore waits for a marker first.

    The guest's serial port is a TCP socket by default (see QemuProcess): the stdio
    chardev drops all but the first byte of a burst, which would make every test
    here a coin toss.  With `monitor_port` a QEMU monitor is exposed as well, which
    is how a step can press real keys instead of typing on the serial port.
    """
    qemu = qemu or find()
    if qemu is None:
        raise FileNotFoundError("qemu-system-i386 was not found")

    if serial_port is None:
        serial_port = _free_port()
    # The monitor is always present, and QEMU always starts paused, so that both
    # sockets can be connected before the guest runs a single instruction.  A
    # chardev with no client discards what the guest writes, and the first thing the
    # kernel writes is its banner: connecting after boot loses exactly the output a
    # test wants to assert on.
    if monitor_port is None:
        monitor_port = _free_port()
    command = build_command(image, qemu=qemu,
                            serial=f"tcp:127.0.0.1:{serial_port},server,nowait",
                            monitor=f"tcp:127.0.0.1:{monitor_port},server,nowait",
                            extra=[*extra, "-S"])
    process = QemuProcess(command, serial_port=serial_port)
    monitor: Optional[_Monitor] = None
    unmet: list[str] = []
    try:
        monitor = _Monitor(monitor_port)
        monitor.command("cont")
        for step in steps:
            if step.wait and not process.wait_for(step.wait, timeout=step.timeout):
                unmet.append(step.wait)
            if step.serial:
                process.send(step.serial)
            for key in step.keys:
                monitor.send_key(key)
        finished = process.wait_for_exit(timeout=timeout)
        timed_out = not finished
        status = process.status()
        if timed_out:
            process.kill()
            status = process.status()
        process.wait_for_exit(timeout=2.0)
        return QemuRun(status=status, transcript=process.transcript(),
                       timed_out=timed_out, command=command, unmet_waits=unmet)
    finally:
        if monitor is not None:
            monitor.close()
        process.kill()
        process.close()


def _free_port() -> int:
    """A port nobody is listening on, so parallel tests do not collide."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
