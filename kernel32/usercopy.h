// myos 32-bit kernel: talking to memory a user program owns.
//
// The kernel can read any byte of the address space; a user program cannot read the
// kernel's.  That asymmetry is the whole point of ring 3, and it stops being true
// the moment the kernel dereferences a pointer a syscall was handed: the pointer is
// a number the user chose, and it may point at kernel memory, at unmapped memory, or
// at a page that is mapped but not for user access.
//
// So every syscall argument that is a pointer goes through here.  The checks are
// page by page, not just at the ends: a buffer that starts and ends inside user
// memory but crosses a kernel page in the middle is exactly the shape of an attack
// that a "range looks fine" test would wave through.

#pragma once

#include "types.h"

namespace myos {

// Every error a syscall can return is a negative number, so "how many bytes" and
// "what went wrong" fit in one register.
//
// The values live in a band of their own, -32..-39, because the filesystem already
// uses -1..-16 and two namespaces that overlap are two namespaces that will be
// confused for each other -- `-14` was both `FS_BAD_FD` and, in the first draft of
// this file, `E_FAULT`.  `sys_error_text()` prints either band; a libc maps these to
// its own `errno` values, which is what libcs do.
constexpr int32 E_PERM = -32;       // not permitted
constexpr int32 E_NO_ENT = -33;     // no such file or directory
constexpr int32 E_BAD_F = -34;      // bad file descriptor
constexpr int32 E_AGAIN = -35;      // would block
constexpr int32 E_NO_MEM = -36;     // out of memory
constexpr int32 E_FAULT = -37;      // the user's pointer is not usable
constexpr int32 E_INVAL = -38;      // invalid argument
constexpr int32 E_NO_SYS = -39;     // no such syscall

// Where user addresses live.  One flat window: the kernel maps user pages inside it
// and nothing else there, so "is this address the user's?" is a range test plus a
// page-table check rather than a guess.
constexpr uint32 USER_BASE = 0x40000000;
constexpr uint32 USER_LIMIT = 0x50000000;           // 256 MiB of user window
constexpr uint32 USER_IMAGE_MAX = 256 * 1024;
constexpr uint32 USER_STACK_BOTTOM = USER_BASE + 0x00100000;    // 1 MiB above the base
constexpr uint32 USER_STACK_BYTES = 16 * 1024;

// True when every page of [address, address + bytes) is mapped and user-accessible.
// A zero-length range at the very end of the window is allowed, which is what makes
// `write(fd, buffer, 0)` behave rather than fail.
bool user_range_ok(uint32 address, uint32 bytes);
bool user_string_ok(uint32 address, uint32 limit, uint32* out_length);

// Copies, or returns E_FAULT and touches nothing it is not allowed to.  The
// destination of copy_from_user and the source of copy_to_user are the kernel's, so
// they are trusted; the other side is not.
int32 copy_from_user(void* destination, uint32 source, uint32 bytes);
int32 copy_to_user(uint32 destination, const void* source, uint32 bytes);

}  // namespace myos
