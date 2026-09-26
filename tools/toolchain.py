"""
Toolchain discovery and invocation.

Everything the build needs is located here and nowhere else, so a missing tool
produces one clear message instead of a stack trace from deep inside a build
step.  Findings that are environment-specific (and surprising) are recorded in
Toolchain.notes and surfaced by `python build.py doctor`:

  * nasm 3.x is case sensitive: `[BITS 16]` is a hard error, only lowercase
    `[bits 16]` is accepted, and the error is reported on the following line.
  * The UCRT64/MSYS2 linkers cannot emit a bare-metal image at all -- they only
    support i386pe/i386pep targets, so a flat binary is refused outright.  The
    build therefore links with the in-tree COFF linker (tools.cofllink) and
    never relies on a system ld.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

# Where tools like to hide when they are not on PATH.
_EXTRA_DIRS = (
    Path(r"C:\msys64\ucrt64\bin"),
    Path(r"C:\msys64\mingw64\bin"),
    Path(r"C:\msys64\usr\bin"),
    Path(r"C:\Program Files\qemu"),
    Path(r"C:\Program Files (x86)\qemu"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "qemu",
    Path(os.environ.get("USERPROFILE", "")) / "scoop" / "apps" / "qemu" / "current",
    Path(os.environ.get("ProgramFiles", "")) / "NASM",
    Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "bin" / "NASM",
)


@dataclass
class Tool:
    name: str
    path: Optional[Path] = None
    version: str = ""
    required: bool = False
    note: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None

    def __str__(self) -> str:
        if not self.found:
            return f"{self.name:<18} MISSING" + (" (required)" if self.required else "")
        tail = f"  {self.version}" if self.version else ""
        return f"{self.name:<18} {self.path}{tail}"


@dataclass
class Toolchain:
    tools: dict[str, Tool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def get(self, name: str) -> Tool:
        return self.tools[name]

    def path(self, name: str) -> Path:
        tool = self.tools[name]
        if tool.path is None:
            raise ToolMissing(f"{name} was not found; run `python build.py doctor`")
        return tool.path

    def have(self, name: str) -> bool:
        return self.tools.get(name, Tool(name)).found

    @property
    def missing_required(self) -> list[str]:
        return [t.name for t in self.tools.values() if t.required and not t.found]

    @property
    def qemu_available(self) -> bool:
        return self.have("qemu-system-i386")

    def report(self) -> str:
        lines = ["toolchain:"]
        for tool in self.tools.values():
            lines.append("  " + str(tool))
        if self.notes:
            lines.append("notes:")
            lines.extend("  - " + n for n in self.notes)
        missing = self.missing_required
        if missing:
            lines.append(f"REQUIRED TOOLS MISSING: {', '.join(missing)}")
        else:
            lines.append("all required tools present")
        if not self.qemu_available:
            lines.append(
                "qemu-system-i386 not found: `run.py --backend qemu` is unavailable, "
                "but the built-in simulator backend works without it."
            )
        return "\n".join(lines)


class ToolMissing(RuntimeError):
    pass


class ToolFailed(RuntimeError):
    """A tool ran but returned a failure; carries its stderr for diagnosis."""

    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str,
                 stdout: str = "") -> None:
        self.cmd = [str(c) for c in cmd]
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        detail = stderr.strip() or stdout.strip() or "(no output)"
        super().__init__(f"{self.cmd[0]} failed ({returncode}):\n{detail}")


def _which(name: str) -> Optional[Path]:
    found = shutil.which(name)
    if found:
        return Path(found)
    for directory in _EXTRA_DIRS:
        if not directory or not str(directory):
            continue
        for suffix in (".exe", ""):
            candidate = directory / (name + suffix)
            if candidate.is_file():
                return candidate
    return None


def _version(path: Path, args: Iterable[str] = ("--version",)) -> str:
    try:
        proc = subprocess.run([str(path), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:120] if text else ""


def discover() -> Toolchain:
    """Locate every tool the project can use and record environment quirks."""
    tc = Toolchain()

    def add(name: str, required: bool, version_args: Iterable[str] = ("--version",),
            note: str = "") -> None:
        path = _which(name)
        tool = Tool(name=name, path=path, required=required, note=note)
        if path is not None:
            tool.version = _version(path, version_args)
        tc.tools[name] = tool

    add("nasm", required=True, version_args=("-v",))
    add("ndisasm", required=False, version_args=("-v",))
    add("g++", required=False)
    add("gcc", required=False)
    add("ld", required=False, note="system ld cannot emit bare-metal images here")
    add("objdump", required=False)
    add("qemu-system-i386", required=False, version_args=("--version",))
    add("qemu-img", required=False, version_args=("--version",))
    add("git", required=False, version_args=("--version",))

    nasm = tc.tools["nasm"]
    if nasm.found:
        major = nasm.version.split()[2].split(".")[0] if "version" in nasm.version else "?"
        if major.isdigit() and int(major) >= 3:
            tc.notes.append(
                "nasm 3.x is case sensitive: use lowercase [bits 16] and [org 0x7c00]; "
                "an uppercase directive reports the error on the following line."
            )

    ld = tc.tools["ld"]
    if ld.found:
        try:
            proc = subprocess.run([str(ld.path), "-m", "nosuchtarget", "-o", os.devnull],
                                  capture_output=True, text=True, timeout=20)
            supported = proc.stderr.strip()
            if "i386pe" in supported and "elf_i386" not in supported:
                tc.notes.append(
                    "system ld supports only PE targets, so it cannot link a flat binary; "
                    "the build uses tools/cofllink.py instead."
                )
        except (OSError, subprocess.SubprocessError):
            pass

    if not tc.qemu_available:
        tc.notes.append(
            "qemu-system-i386 not found; the simulator backend is used for all tests."
        )
    return tc


def run(cmd: Sequence[str | Path], cwd: Optional[Path] = None,
        check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    """Run a tool, raising ToolFailed with its stderr when check=True."""
    argv = [str(c) for c in cmd]
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=text)
    if check and proc.returncode != 0:
        raise ToolFailed(argv, proc.returncode, proc.stderr or "", proc.stdout or "")
    return proc


def assemble(source: Path, output: Path, fmt: str = "bin",
             include_dirs: Sequence[Path] = (), defines: Sequence[str] = (),
             tc: Optional[Toolchain] = None) -> Path:
    """Run nasm on one source file."""
    tc = tc or discover()
    cmd: list[str] = [str(tc.path("nasm")), "-f", fmt, "-o", str(output)]
    for directory in include_dirs:
        cmd += ["-I", str(directory) + os.sep]
    for define in defines:
        cmd.append(f"-D{define}")
    cmd.append(str(source))
    run(cmd)
    if not output.is_file():
        raise ToolFailed(cmd, 0, f"nasm reported success but {output} was not created")
    return output


def compile_cpp(source: Path, output: Path, include_dirs: Sequence[Path] = (),
                defines: Sequence[str] = (), optimize: str = "-O2",
                tc: Optional[Toolchain] = None) -> Path:
    """Compile freestanding 32-bit C++ to a PE32 object the in-tree linker reads."""
    tc = tc or discover()
    compiler = "g++" if tc.have("g++") else "gcc"
    cmd: list[str] = [
        str(tc.path(compiler)),
        "-m32", "-ffreestanding", "-nostdlib",
        "-fno-exceptions", "-fno-rtti", "-fno-pic", "-fno-pie",
        "-fno-stack-protector", "-fno-threadsafe-statics",
        "-fno-asynchronous-unwind-tables", "-fno-unwind-tables",
        "-fno-builtin", "-Wall", "-Wextra",
        # No floating point, no vector registers.  This is not an optimisation
        # choice: the default target enables SSE, so gcc happily copies an array
        # with `movdqu`/`movups`, and an i386 kernel that has not set
        # CR4.OSFXSR takes #UD on the first one -- which surfaces as a triple
        # fault in the middle of unrelated code, with no hint that a vector
        # instruction was involved.  Everything here is integer code.
        "-mno-sse", "-mno-sse2", "-mno-mmx", "-mno-3dnow", "-mno-avx",
        "-mno-80387",
        optimize, "-c", str(source), "-o", str(output),
    ]
    for directory in include_dirs:
        cmd += ["-I", str(directory)]
    for define in defines:
        cmd.append(f"-D{define}")
    run(cmd)
    return output


def objdump_details(obj: Path, tc: Optional[Toolchain] = None) -> str:
    """`objdump -dr` for one object, or an empty string when objdump is absent."""
    tc = tc or discover()
    if not tc.have("objdump"):
        return ""
    proc = run([str(tc.path("objdump")), "-dr", str(obj)], check=False)
    return proc.stdout or ""


def disassemble(image: Path, bits: int = 16, origin: int = 0,
                tc: Optional[Toolchain] = None) -> str:
    """ndisasm listing for a flat binary."""
    tc = tc or discover()
    if not tc.have("ndisasm"):
        return ""
    proc = run([str(tc.path("ndisasm")), "-b", str(bits), "-o", str(origin), str(image)],
               check=False)
    return proc.stdout or ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    tc = discover()
    print(tc.report())
    return 1 if tc.missing_required else 0


if __name__ == "__main__":
    sys.exit(main())
