// myos 32-bit kernel: freestanding string and memory helpers.
//
// The loops are written by hand rather than with the compiler's builtin expansion
// (which -fno-builtin suppresses anyway) so that this file cannot call itself.

#include "libc.h"

extern "C" void* memset(void* dest, int value, myos::size_t count) {
    unsigned char* out = static_cast<unsigned char*>(dest);
    for (myos::size_t i = 0; i < count; ++i) {
        out[i] = static_cast<unsigned char>(value);
    }
    return dest;
}

extern "C" void* memcpy(void* dest, const void* src, myos::size_t count) {
    unsigned char* out = static_cast<unsigned char*>(dest);
    const unsigned char* in = static_cast<const unsigned char*>(src);
    for (myos::size_t i = 0; i < count; ++i) {
        out[i] = in[i];
    }
    return dest;
}

extern "C" void* memmove(void* dest, const void* src, myos::size_t count) {
    unsigned char* out = static_cast<unsigned char*>(dest);
    const unsigned char* in = static_cast<const unsigned char*>(src);
    if (out == in || count == 0) {
        return dest;
    }
    if (out < in) {
        for (myos::size_t i = 0; i < count; ++i) {
            out[i] = in[i];
        }
    } else {
        // Overlapping and moving up: copy backwards, or the source is overwritten
        // before it is read.  A caller scrolling a screen buffer hits this.
        for (myos::size_t i = count; i > 0; --i) {
            out[i - 1] = in[i - 1];
        }
    }
    return dest;
}

extern "C" int memcmp(const void* a, const void* b, myos::size_t count) {
    const unsigned char* left = static_cast<const unsigned char*>(a);
    const unsigned char* right = static_cast<const unsigned char*>(b);
    for (myos::size_t i = 0; i < count; ++i) {
        if (left[i] != right[i]) {
            return left[i] < right[i] ? -1 : 1;
        }
    }
    return 0;
}

extern "C" myos::size_t strlen(const char* text) {
    myos::size_t length = 0;
    while (text[length] != '\0') {
        ++length;
    }
    return length;
}

extern "C" int strcmp(const char* a, const char* b) {
    while (*a != '\0' && *a == *b) {
        ++a;
        ++b;
    }
    return static_cast<unsigned char>(*a) - static_cast<unsigned char>(*b);
}

extern "C" int strncmp(const char* a, const char* b, myos::size_t count) {
    for (myos::size_t i = 0; i < count; ++i) {
        if (a[i] != b[i]) {
            return static_cast<unsigned char>(a[i]) - static_cast<unsigned char>(b[i]);
        }
        if (a[i] == '\0') {
            return 0;
        }
    }
    return 0;
}

extern "C" char* strcpy(char* dest, const char* src) {
    char* out = dest;
    while ((*out++ = *src++) != '\0') {
    }
    return dest;
}

namespace myos {

size_t utoa_base(uint32 value, char* buffer, uint32 base) {
    static const char digits[] = "0123456789abcdef";
    char reversed[32];
    size_t length = 0;

    if (value == 0) {
        buffer[0] = '0';
        buffer[1] = '\0';
        return 1;
    }
    while (value != 0 && length < sizeof(reversed)) {
        reversed[length++] = digits[value % base];
        value /= base;
    }
    for (size_t i = 0; i < length; ++i) {
        buffer[i] = reversed[length - 1 - i];
    }
    buffer[length] = '\0';
    return length;
}

size_t utoa(uint32 value, char* buffer) {
    return utoa_base(value, buffer, 10);
}

}  // namespace myos
