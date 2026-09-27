// The same first program, written in C++ and linked by the project's own linker.
//
// Two things are being checked here.  The first is that the toolchain works for user
// code at all: g++ -m32, the in-tree linker, the user layout, the header patched into
// place -- a C++ user program is a different build path from the kernel's, and a
// different build path is a thing that breaks quietly.
//
// The second is the syscall convention from the other side.  `user/hello.asm` proves
// that a hand-written caller agrees with the kernel; this proves that what a compiler
// generates around an `int 0x80` agrees with it too -- including the register
// pressure, the EBX operand and the fact that the result comes back in EAX.
//
// There is no libc out here.  The only things this program can do are the syscalls in
// docs/abi.md, which is the whole point of a user mode existing.

namespace {

// The only calling convention that matters in a freestanding user program: the
// kernel's.  `-fno-pic -fno-pie` is what makes the address of a string the address
// the kernel mapped it at; a position-independent program would be asking the kernel
// to relocate it, which this kernel does not do.
inline int syscall3(int number, int first, int second, int third) {
    int result;
    asm volatile("int $0x80"
                 : "=a"(result)
                 : "a"(number), "b"(first), "c"(second), "d"(third)
                 : "memory");
    return result;
}

inline void write_stdout(const char* text, int count) {
    syscall3(4, 1, reinterpret_cast<int>(text), count);
}

inline int string_length(const char* text) {
    int length = 0;
    while (text[length] != '\0') {
        ++length;
    }
    return length;
}

// Decimal, without a libc's printf: the point of the program is the syscall, not the
// formatting, and a tiny loop keeps the interesting part visible.
void write_number(int value) {
    char digits[12];
    int length = 0;
    if (value < 0) {
        write_stdout("-", 1);
        value = -value;
    }
    do {
        digits[length++] = static_cast<char>('0' + value % 10);
        value /= 10;
    } while (value != 0 && length < 11);
    for (int index = 0; index < length / 2; ++index) {
        const char swap = digits[index];
        digits[index] = digits[length - 1 - index];
        digits[length - 1 - index] = swap;
    }
    write_stdout(digits, length);
}

}  // namespace

extern "C" void user_entry() {
    static const char message[] = "hello from a C++ user program\n";
    write_stdout(message, string_length(message));

    // getpid has to be answered by the kernel rather than by anything this program
    // can compute: with one task in the system the answer is 1, but it comes back
    // through EAX from ring 0.
    const int pid = syscall3(20, 0, 0, 0);
    static const char pid_text[] = "getpid() in user mode returned ";
    write_stdout(pid_text, string_length(pid_text));
    write_number(pid);
    write_stdout("\n", 1);

    // Exit non-zero if the kernel answered something other than 1, so a broken
    // syscall path is a failing exit code and not just a line of text.
    syscall3(1, pid == 1 ? 0 : 2, 0, 0);
    for (;;) {
    }
}
