// myos 32-bit kernel: the syscall table behind kernel32/syscall.h.

#include "syscall.h"

#include "console.h"
#include "fs.h"
#include "klog.h"
#include "libc.h"
#include "usercopy.h"

namespace myos {
namespace {

uint32 g_calls = 0;
uint32 g_last = 0;

// A syscall's user buffer is copied into a kernel buffer before it is written out:
// the console driver must not be handed a pointer that a user program can change
// between the check and the copy.
constexpr uint32 WRITE_CHUNK = 256;
char g_write_buffer[WRITE_CHUNK];

}  // namespace

const char* sys_error_text(int32 error) {
    switch (error) {
        case E_PERM: return "not permitted";
        case E_NO_ENT: return "no such file or directory";
        case E_BAD_F: return "bad file descriptor";
        case E_AGAIN: return "would block";
        case E_NO_MEM: return "out of memory";
        case E_FAULT: return "the user's buffer is not usable";
        case E_INVAL: return "invalid argument";
        case E_NO_SYS: return "no such system call";
        default: return fs_error_text(error);
    }
}

const char* syscall_name(uint32 number) {
    switch (number) {
        case SYS_EXIT: return "exit";
        case SYS_WRITE: return "write";
        case SYS_GETPID: return "getpid";
        case SYS_YIELD: return "yield";
        default: return "unknown";
    }
}

uint32 syscall_count() {
    return g_calls;
}

uint32 syscall_last() {
    return g_last;
}

// Defined in user.asm: restores the kernel stack saved by run_user_image and returns
// to it.  Never comes back here.
extern "C" void return_to_kernel(uint32 exit_code);

void syscall_dispatch(Registers* regs) {
    const uint32 number = regs->eax;
    ++g_calls;
    g_last = number;

    switch (number) {
        case SYS_WRITE: {
            // write(fd, buffer, count) -- only the console is writable today, and
            // the buffer is checked before a single byte of it is read.
            const uint32 descriptor = regs->ebx;
            uint32 address = regs->ecx;
            uint32 remaining = regs->edx;
            if (descriptor != 1 && descriptor != 2) {
                regs->eax = static_cast<uint32>(E_BAD_F);
                return;
            }
            if (!user_range_ok(address, remaining)) {
                regs->eax = static_cast<uint32>(E_FAULT);
                return;
            }
            uint32 written = 0;
            while (remaining > 0) {
                uint32 chunk = remaining < WRITE_CHUNK ? remaining : WRITE_CHUNK;
                memcpy(g_write_buffer, reinterpret_cast<const void*>(address), chunk);
                console_write(g_write_buffer, chunk);
                address += chunk;
                written += chunk;
                remaining -= chunk;
            }
            regs->eax = written;
            return;
        }
        case SYS_GETPID:
            // One task exists, so the answer is always 1.  Phase 2 makes this read
            // the current task's pid; the number a program sees does not change.
            regs->eax = 1;
            return;
        case SYS_YIELD:
            // Nothing to yield to yet: with no scheduler this returns immediately
            // rather than pretending to have done something.  A program that calls
            // it in a loop is a program that will spin, which is honest.
            regs->eax = 0;
            return;
        case SYS_EXIT: {
            const uint32 code = regs->ebx;
            klog("syscall exit from user mode");
            return_to_kernel(code);
            return;                         // not reached
        }
        default:
            regs->eax = static_cast<uint32>(E_NO_SYS);
            return;
    }
}

void syscall_report() {
    if (g_calls == 0) {
        console_puts("syscall: none yet (try `run /bin/hello`)\n");
        return;
    }
    kprintf("syscall: %u call(s), the last one %u (%s)\n", g_calls, g_last,
            syscall_name(g_last));
}

}  // namespace myos
