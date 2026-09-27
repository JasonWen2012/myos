// myos 32-bit kernel: system calls.
//
// The ABI is written down in docs/abi.md and this file is one of its two
// implementations.  The convention is the dull, portable one: the call number in
// EAX, arguments in EBX, ECX, EDX, ESI, EDI in that order, and the result in EAX --
// a negative errno when something went wrong.  Nothing here needs `sysenter` or the
// `int 0x80` fast path; what it needs is to be small enough to read and stable
// enough that a user program compiled against it keeps working.

#pragma once

#include "idt.h"
#include "types.h"

namespace myos {

// Call numbers.  They follow Linux's i386 table where a call exists there, so a
// future libc does not have to translate anything.
constexpr uint32 SYS_EXIT = 1;
constexpr uint32 SYS_WRITE = 4;
constexpr uint32 SYS_GETPID = 20;
constexpr uint32 SYS_YIELD = 158;

// Called from the `int 0x80` stub with the register frame the stub saved.  The return
// value goes into that frame's EAX, which is where `iret` takes it from.  C linkage
// because the stub in kernel32/ring3.asm names it directly and cannot spell a mangled
// name -- the same reason the exception and IRQ dispatchers are declared this way.
extern "C" {
void syscall_dispatch(Registers* regs);
}

// One text function for both error bands: the filesystem's (-1..-16, in fs.h) and the
// syscall errors of usercopy.h (-32..-39).  A caller that got a negative number and
// wants to say what it was should not have to know which table it came from.
const char* sys_error_text(int32 error);

// How many syscalls have been made, and the number of the last one: `vm` and the
// self-test report them, and they are the only evidence that a user program did
// anything through the kernel rather than on its own.
uint32 syscall_count();
uint32 syscall_last();
const char* syscall_name(uint32 number);
void syscall_report();

}  // namespace myos
