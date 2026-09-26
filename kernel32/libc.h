// myos 32-bit kernel: freestanding string and memory helpers.

#pragma once

#include "types.h"

// extern "C" so the compiler's own calls to memset/memcpy (a struct copy, a large
// initialisation) resolve to these rather than to a libc that is not linked in.
extern "C" {
void* memset(void* dest, int value, myos::size_t count);
void* memcpy(void* dest, const void* src, myos::size_t count);
void* memmove(void* dest, const void* src, myos::size_t count);
int memcmp(const void* a, const void* b, myos::size_t count);
myos::size_t strlen(const char* text);
int strcmp(const char* a, const char* b);
int strncmp(const char* a, const char* b, myos::size_t count);
char* strcpy(char* dest, const char* src);
}

namespace myos {

// utoa: decimal, no leading zeroes.  Returns the length written, not counting the
// terminator.  `buffer` must hold at least 11 bytes for a 32-bit value.
size_t utoa(uint32 value, char* buffer);

// utoa_base: the same, in any base from 2 to 16, lower case.
size_t utoa_base(uint32 value, char* buffer, uint32 base);

}  // namespace myos
