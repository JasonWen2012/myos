**English** | [简体中文](./README-CN.md)

# myos

An x86 operating system written from scratch, running two kernels on top of its own bootloader and its own linker:

| | 16-bit kernel | 32-bit kernel |
|---|---|---|
| How it runs | Real mode, runs in the project's own emulator | Protected mode, C++ (`g++ -m32`), runs in QEMU |
| What it can do now | Boot, type, command line | All of the above + interrupts/timer/serial + **its own hard disk driver and file system** + **multiple tasks with a preemptive round-robin scheduler** |
| Can it store files | No | Yes, and they survive a reboot (stored in `data/myfs-data.img`) |

Want to just play with it: **skip to [5-minute quick start](#5-minute-quick-start)**. Want to first know "what is this, and why was it written this way", see
[`docs/design.md`](docs/design.md).

---

## 5-minute quick start

In the project root directory (`myos`), run with **cmd or PowerShell**. Building only needs Python (standard library), no make required.

### Route A: 16-bit kernel (no need to install any virtual machine)

```bat
python build.py all
python run.py --interactive
```

You'll see the kernel boot, print a banner, then stop at `myos>`. Type whatever you like:

```
help
info
mem
fact 5
reboot
```

Press `Ctrl+C` (or `Esc`) to exit the interactive session. This kernel runs inside the **project's own 16-bit emulator**, so there are no external dependencies at all, and no need for a graphics card or permissions.

### Route B: 32-bit kernel (can store files, uses QEMU)

```bat
python build.py all --arch 32
python build.py data-disk
python run.py --arch 32
```

A QEMU window will pop up (showing the kernel's VGA display), and the same text is also printed in this terminal.
Type in the QEMU window (or type directly in this terminal—both are input):

```
help
ls /
cat /readme.txt
write /note.txt hello myos
cat /note.txt
df
run /bin/hello              ← a program running in ring 3, with its own address space
run /bin/badwrite           ← and one that tries to read kernel memory: exit code 37
schedtest                   ← two tasks, preempted by the timer, then reaped
ps                          ← the task table
dmesg                       ← what the kernel logged on the way
reboot                      ← marks the volume clean before shutting down
```

**Then run `python run.py --arch 32` again, and type `ls /` and
`cat /note.txt`—the file is still there.** This is "long-term storage": it doesn't just stay in memory, it's really written into
`data/myfs-data.img`.

On Windows you can also look at that disk directly:

```bat
python tools/myfs.py --image data/myfs-data.img --partition 1 --list
python tools/myfs.py --image data/myfs-data.img --partition 1 --extract /note.txt --out note.txt
```

`myfs.py` is **another implementation** of the file system format (written in Python); the one in the kernel is the C++ one. The two cross-check each other: at build time a checksum is computed for every file and written into `/manifest`, and the kernel recomputes it on every boot.

### Take a look at what it looks like internally

```bat
python memmap.py --arch 32 --image    :: disk/memory layout (LBA, partitions, volumes all listed)
python memmap.py --live               :: boot the 16-bit kernel once and print a runtime memory snapshot
python build.py doctor                :: report toolchain detection results (whether NASM/g++/QEMU were found)
```

---

## Typing and usage rules

- **ASCII only**: both the keyboard and serial port accept only visible ASCII (codes 32..126). Currently you can't type Chinese.
- Enter submits (`Enter` / `\r` / `\n` all work), backspace deletes characters, a line is at most 79 characters and at most 8 words.
- Command names are **case-insensitive** (`HELP` and `help` are the same), arguments are case-sensitive.
- Exiting: for 16-bit interactive, press `Ctrl+C`; for 32-bit, close the QEMU window, or type `reboot` in the guest.
- No extra switch is needed for 32-bit: `python run.py --arch 32` lets you type (`--interactive` is for the 16-bit emulator). The QEMU window and this terminal **both** display output, and input can come from either side.

## Command quick reference

### 16-bit kernel

| Command | Purpose |
|---|---|
| `help` | Command list |
| `echo <text>` | Echo back verbatim |
| `clear` | Clear the screen |
| `info` | Version, image size, stack pointer, segment registers |
| `mem` | Memory reported by firmware / memory used by myos itself |
| `ticks` | BIOS's 18.2 Hz timing (decimal + hexadecimal) |
| `fact <0-8>` | Recursive factorial (demonstrates the stack and calling convention) |
| `keylog` | Who owns INT 09h, and why that determines whether input works |
| `reboot` | Reboot |

### 32-bit kernel: general

| Command | Purpose |
|---|---|
| `help` `echo` `clear` `mem` `ticks` `fact` `keylog` `reboot` | Same as the 16-bit kernel |
| `info` | Version, image/entry address, GDT/IDT, PIC vectors, boot drive number |
| `vm` | Physical pages, page tables, identity map and the heap (`vm fault` tests the panic path) |
| `run <path>` | Run a program from the volume in ring 3 (`run /bin/hello`) |
| `ps` | The task table: pid, state, CPU ticks, times switched to, page directory |
| `schedtest` | Create two tasks, let the timer preempt them, then reap them |
| `check` | Run the in-guest checks and stay in the shell |
| `dmesg` | The kernel's own log, oldest first |
| `selftest` | Run the kernel self-test (68 items) and **report the result via exit code**; tests rely on this |

### 32-bit kernel: disk and files

| Command | Purpose |
|---|---|
| `ls [path]` | List a directory (default `/`), directories show entry count, files show size and inode number |
| `cat <path>` | Print file contents (reports size first, adds nothing after the content) |
| `write <path> <text>` | Create or **overwrite** a file; with no text it just empties it |
| `mkdir <path>` | Create a directory |
| `rm <path>` | Delete a file or an **empty** directory (refuses non-empty, also refuses the root directory) |
| `stat <path>` | Type / inode / size / number of blocks used |
| `df` | How much of the volume is used, how much is left (blocks and inodes reported separately) |
| `fs` | Volume version, label, state (clean/dirty), partition location |
| `blk` | Hard disk model, capacity, myfs volume in the partition table |
| `blk test` | Write a pattern to the "test area" at the end of the disk and read it back to compare (the only self-test that writes to disk) |
| `sync` | Mark the volume as "cleanly unmounted" (writes are already write-through, there's no buffer to flush) |
| `fsck` | Scan the whole volume: reclaim leaked blocks, fix free counts, clear the dirty flag |
| `fstest` | The kernel's own write-path self-test (22 items), restores the volume to its original state afterward |

### 32-bit kernel: ring 3 and the programs in /bin

The volume ships three programs (built from `user/` at build time), and `run` starts one of
them in ring 3 — a real user program, on its own pages, which cannot reach kernel memory:

```
myos> run /bin/hello
run: /bin/hello, 72 bytes at 40000000, stack 40104000, entry offset 00000010
hello from ring 3
run: /bin/hello exited with code 0
run: 5 page(s) of user address space, 2 syscall(s)
myos> run /bin/badwrite          ← asks the kernel to print 8 bytes of kernel memory
run: /bin/badwrite exited with code 37     ← 37 = E_FAULT: refused, nothing leaked
myos> run /bin/hellocpp
hello from a C++ user program
getpid() in user mode returned 1
run: /bin/hellocpp exited with code 0
```

The contract those programs are written against — syscall numbers, registers, error codes, the
user address window and the user-image format — is in [`docs/abi.md`](docs/abi.md). A program is
refused before a single page of it is mapped unless its header checks out, and the kernel's own
manifest check catches a corrupted file independently of that.

A real session looks roughly like this:

```
myos> fs
fs: myfs v1 'myos', 2048 blocks of 512 bytes, 64 inodes
fs: state clean, 2036 free blocks, 57 free inodes
myos> write /note.txt hello myos
write: /note.txt, 10 bytes, 2035 free blocks
myos> cat /note.txt
cat: /note.txt (10 bytes)
hello myos
cat: 10 bytes read
myos> df
df: 2048 blocks of 512 bytes: 7 used, 2035 free
df: 64 inodes: 7 used, 56 free
```

### 32-bit kernel: more than one thing running

The kernel has a task table now, and a round-robin scheduler driven by the timer. `schedtest`
creates two tasks that never yield — each one spins until the timer has taken the CPU away from it
and given it back twice — so the lines below can only appear if preemption really happens:

```
myos> schedtest
schedtest: creating two tasks and letting the timer preempt them
  ok   the boot context is task 0, it is running, and it is alone
  ok   each new task gets a pid of its own
  ok   a new task is not yet runnable and runs in a page directory of its own
  ok   a new task's kernel stack is heap memory with its canary in place
sched: alpha round 1 (ticks 0)
sched: beta round 1 (ticks 0)
sched: alpha round 2 (ticks 2)
sched: beta round 2 (ticks 2)
  ok   a task's exit code reaches the task that created it
  ok   the interrupt flag survives being switched away and back
  ok   the timer charged CPU time to every runnable task
  ok   the round robin switched into every task
  ok   neither task overran its kernel stack
  ok   reaping both tasks gives their stacks back to the heap
  ok   the TSS and CR3 name the task that is running
schedtest: 11 ok, 0 skipped, 0 failed
schedtest: every task ran, exited, and gave its stack back
myos> ps
ps: 1 task(s) in the table, 4 created, 4 reaped, 17 switch(es)
ps: pid state ticks switches parent name
ps: 0 running 130 6 none kmain
sched: 17 switch(es), 146 tick(s) charged, quantum 2 tick(s)
sched: current pid 0 (kmain), preemption on
```

What each task owns: a pid, a state (`new` → `ready` → `running` → `zombie`), an 8 KiB kernel
stack with a canary at the bottom, its own page directory (the kernel's mappings are shared, the
directory is not, so a task can have private pages later), and the two registers that have to
follow it — `CR3` and the TSS's `esp0`.

What does **not** exist yet: `fork`, copy-on-write, signals, pipes, per-process file descriptors,
and user programs as tasks. `run` still executes a ring-3 program synchronously on behalf of the
task that called it. Those are the next milestone; see [`docs/roadmap.md`](docs/roadmap.md).

---

## Where files are actually stored

| Location | What it is | Will it be overwritten by the build |
|---|---|---|
| `files/` | Files preset in the repo, packaged into the **build output** | Repackaged on every `build.py` |
| `images/myos32-hd.img` | The built hard disk image (containing the myfs volume), used by tests | Rewritten by `build.py all` |
| `data/myfs-data.img` | **Your own disk**, what `run.py --arch 32` mounts | **Never** overwritten by the build |

So: if you want to keep something, write it on the `data/` disk (that is, in the guest, `write` to `/`). The files in `files/` are sample files "shipped with the firmware"; `build.py clean` only deletes `build/` and `images/*.img`, and won't touch `data/`.

That "never overwritten" promise has one consequence worth knowing before it surprises you: **a data disk made by an older build does not have things later builds started shipping** — the ring-3 programs in `/bin`, for instance. `run /bin/hello` then answers `no such file or directory`. The fix is a *merge*, not a rebuild:

```bat
python build.py data-disk --update    :: add files/ and /bin programs; your own files stay
```

Every name the build owns is created or replaced, and nothing else on the disk is touched — so a note you wrote in the guest survives. `run.py` says so too: when it mounts a data disk with no `/bin`, it prints the hint before starting QEMU. To put a single file in by hand:

```bat
python tools/myfs.py --image data/myfs-data.img --partition 1 --put /bin/hello --from build/user/hello
```

Volume specifications (`myfs` format):

- 1 MiB = 2048 blocks × 512 bytes, 64 inodes.
- Maximum single file size **136704 bytes** (11 direct blocks + 1 single-level indirect block).
- Maximum file name length **29 characters**, compared as-is (case-sensitive).
- Subdirectories are supported, but **`..` is not** (the format doesn't store a parent pointer, so it can't guess).
- Only **overwrite writes**, no append writes; no timestamps and no permissions.

## Directory structure

```
boot/          Bootloader: boot16.asm (512-byte boot sector) + stage2.asm (loads the kernel)
kernel16/      16-bit kernel (pure assembly: console, keyboard, shell)
kernel32/      32-bit kernel (C++: GDT/IDT/PIC/PIT, keyboard, serial, ATA driver, myfs, tasks/scheduler, shell)
emulator/      Project's own 16-bit x86 emulator + BIOS + VGA text rendering (the 16-bit kernel is validated with it)
tools/         cofllink.py project's own linker, myfs.py file system tool, qemu.py test backend, image.py images
files/         Example files: packaged into the myfs volume at build time, plus a /manifest checksum list
user/          Ring-3 programs (header.asm + assembly and C++ sources); built into /bin of that volume
tests/         Regression tests (238)
build.py       Build: assemble + link + package images; also creates your data disk
run.py         Run images: automatically chooses the backend based on the image header (16-bit→emulator, 32-bit→QEMU)
memmap.py      Print memory/disk layout, all numbers measured from source and artifacts
docs/design.md Design and pitfalls record (read this if you want to go deeper)
```

## Common commands at a glance

```bat
python build.py all                     :: 16-bit: boot sector + stage2 + kernel + floppy/hard disk images
python build.py all --arch 32           :: 32-bit: same as above, plus the myfs volume and a 2 MiB hard disk image
python build.py data-disk               :: create data/myfs-data.img (won't rebuild if it already exists)
python build.py data-disk --update      :: merge files/ and /bin programs into it; your own files stay
python build.py doctor                  :: toolchain detection
python build.py clean                   :: clean build/ and images/*.img (doesn't touch data/)

python run.py                           :: run the 16-bit in the project's own emulator and print the screen
python run.py --interactive             :: 16-bit interactive (Ctrl+C to exit)
python run.py --arch 32                 :: 32-bit: QEMU window + automatically mounts your data disk, type directly
python run.py --arch 32 --no-build      :: don't rebuild, run the existing image directly
python run.py --dump                    :: include registers/machine state

python memmap.py --arch 32 --image       :: disk layout
python memmap.py --live                  :: 16-bit runtime memory snapshot
python tools/myfs.py --image data/myfs-data.img --partition 1 --list
python tools/myfs.py --image data/myfs-data.img --partition 1 --extract /note.txt --out note.txt
python tools/myfs.py --image data/myfs-data.img --partition 1 --put /bin/hello --from build/user/hello
python tools/myfs.py --image data/myfs-data.img --partition 1 --check
```

## Tests: how do you know it isn't broken

```bat
python -m unittest discover -s tests          :: all (238)
python -m unittest discover -s tests -v       :: with each test case name
python -m unittest tests.test_kernel32 -v     :: only the 32-bit group
python -m unittest tests.test_myfs            :: only the file system format group
```

- **16-bit** is validated with the project's own emulator: highly deterministic, no QEMU needed.
- **32-bit** is validated with QEMU: real firmware, real CPU. The self-test turns the result into a process exit code via the `isa-debug-exit` device (`0x21` = pass), so a "green light" comes from the guest itself, not from the test waiting until timeout.
- On machines **without QEMU installed**, the 32-bit portion of the tests will **skip** (not fail).
- The file system portion validates persistence using "two independent boots", and the **kernel writes, host reads** to cross-verify.

---

## FAQ

**Why must the 32-bit use QEMU?**
Because the project's own emulator only implements up to 16-bit real mode (32-bit registers, descriptors, and exceptions haven't been written yet).
`run.py` reads the architecture byte in the image header and automatically chooses the backend; if you force the emulator to run a 32-bit image, it will clearly error out rather than run off the rails. Adding protected mode to the project's own emulator is one of the future plans.

**My written file disappeared?**
Two possibilities:
1. You wrote to a build artifact (a disk in `images/`). Every `build.py` rewrites them.
2. You wrote on `data/myfs-data.img`, but started with `python run.py` (default 16-bit)—the 16-bit kernel has no file system, and can't see that disk either. Use `--arch 32`.

**Can I input Chinese?**
No: both keyboard scancode decoding and serial input accept only ASCII (32..126). File contents also won't contain non-ASCII.

**How do I exit?**
16-bit interactive: `Ctrl+C`. 32-bit: close the QEMU window, or type `reboot` in the guest (it marks the volume as cleanly unmounted before rebooting, so shutting down directly won't make the next boot complain).

**What happens if QEMU isn't installed?**
The 16-bit works as usual (project's own emulator). The 32-bit will report that `qemu-system-i386` can't be found; the 32-bit group in the tests will skip. Just install one (Windows version of QEMU, `qemu-system-i386.EXE` in `PATH` or
`C:\Program Files\qemu\`).

**`ls` says "no filesystem mounted"?**
This happens when no disk is mounted; the reason is in parentheses (`no ATA device on the primary channel` = this boot didn't attach a hard disk to the guest). Starting with `run.py --arch 32` will automatically mount `data/myfs-data.img`.

**`run /bin/hello` says "no such file or directory"?**
Your data disk was made before the build started shipping programs in `/bin` — and a data disk is never rebuilt, which is what keeps your own files. Merge them in:

```bat
python build.py data-disk --update
```

It creates or replaces only the names the build owns (`files/` and `/bin/*`) and leaves everything else, including files you wrote in the guest, exactly as it was. `run.py` prints a hint when the disk it is about to mount has no `/bin`. If you would rather look at the build's own disk, boot with `--no-build` after building: `images/myos32-hd.img` has the programs already.

**Why can't `..` be used?**
The format doesn't store a parent pointer, so returning "not supported" is more honest than guessing a directory.

**What are blocks and inodes?**
See the [glossary in `docs/design.md`](docs/design.md#术语表).

**How do I modify the kernel and add a command?**
16-bit: add a handler function in `kernel16/shell.asm` + add a line to the command table in `kernel16/main.asm` (the table stores "offsets relative to the table header"). 32-bit: write a `cmd_xxx()` in `kernel32/shell.cpp`, add a line to the `commands[]` table, and add a declaration above `cmd_help`. After changes, rebuild with `python build.py all
--arch 32`; run the tests once to confirm you didn't step on anything else.

**How big is the 32-bit kernel, and how much room is left?**
The bootloader reserves 896 sectors (448 KiB) for the kernel; about 224 sectors are used now. `build.py` will fail the build directly when the budget is exceeded, rather than producing a broken image.

## What doesn't exist yet

`fork`/copy-on-write, signals, pipes, user programs as their own tasks (a `run` program still executes synchronously on behalf of whoever called it), multiple disks, timestamps and permissions, append writes, files larger than 136704 bytes, Chinese input, and 64-bit (there's no plan for that right now). Paging, a kernel heap, user mode with system calls, **and a preemptive round-robin scheduler** do exist now — `vm` reports the first two, `run` uses the third, `ps` shows the scheduler's task table, and `docs/roadmap.md` has what comes next. The complete design rationale and all the pitfalls encountered along the way are in [`docs/design.md`](docs/design.md),
and the seven-phase plan for the rest of it — what each phase delivers, how it is validated, and what it deliberately does not do — is in [`docs/roadmap.md`](docs/roadmap.md) (Chinese, like the design notes).

## Environment requirements

| Needed | Used for | Status on this machine |
|---|---|---|
| Python 3 | Build, run, test scripts (no make/cmake) | Present |
| NASM | Assembling the bootloader and 16-bit kernel | `C:\Users\JasonW\AppData\Local\bin\NASM\nasm.exe` |
| MSYS2 UCRT64 `g++` | Compiling the 32-bit kernel (`-m32`) | 16.1.0 |
| QEMU (`qemu-system-i386`) | Only for running the 32-bit kernel and its tests | `C:\Program Files\qemu\` |

This project **does not use WSL** (tried many times without success); the entire toolchain is native Windows; there is no `ld` that can produce a bare-metal image, so the 32-bit kernel is linked with the project's own linker `tools/cofllink.py`. `python build.py doctor` will tell you which tool wasn't found.