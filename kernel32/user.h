// myos 32-bit kernel: running a program in ring 3.
//
// A user program is a file in the myfs volume with the same 16-byte self-checking
// header the kernel image has, an architecture byte of 3, and a flat layout starting
// at USER_BASE.  The kernel reads it into memory, maps fresh pages for it, copies the
// bytes in, gives it a stack, and drops to ring 3.
//
// There is a task table and a scheduler now, but a user program is still not a task of
// its own: `run` starts a program *on behalf of* whichever task called it, waits for
// its `exit` syscall, and gets control back through return_to_kernel, with preemption
// held off for the duration (see sched.h).  That is enough to prove the privilege
// boundary, the syscall path and the user-copy checks; turning a user program into a
// task -- its own address space half, its own stack, fork/exec/wait -- is phase 2b.

#pragma once

#include "types.h"

namespace myos {

constexpr uint8 USER_IMAGE_ARCH = 3;                // IMG_ARCH_* in boot/boot.inc is 1 and 2
constexpr uint32 USER_IMAGE_HEADER_BYTES = 16;

struct UserRun {
    uint32 exit_code;
    uint32 pages;               // user pages mapped for the image and its stack
    uint32 syscalls;            // how many the program made
    uint32 bytes;               // size of the image
};

// Runs one program and waits for it to exit.  `report` may be null.  Returns a
// negative errno when the program could not be started at all; a program that ran and
// exited with a non-zero code is a success here, with the code in the report.
int32 exec_user(const char* path, UserRun* report);

// Validates a user image header (magic, architecture, checksum, entry inside the
// image).  Exposed for the self-test, which checks the checks that would otherwise
// only run on the happy path.
bool user_image_ok(const uint8* header, uint32 bytes, uint32* out_entry,
                   uint32* out_size);

// How many programs this boot has run, and how many pages are mapped for user mode
// right now (zero when nothing is running).
uint32 user_programs_run();
uint32 user_pages_mapped();
void user_report();

}  // namespace myos
