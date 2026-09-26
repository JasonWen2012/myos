// myos 32-bit kernel: the fixed-width types everything else is written against.
//
// Deliberately not <stdint.h>: the kernel is freestanding, and pulling in a host
// header for four typedefs invites the compiler's hosted assumptions with it.
// The types are the i386 ones, stated once, here.

#pragma once

namespace myos {

using uint8 = unsigned char;
using int8 = signed char;
using uint16 = unsigned short;
using int16 = short;
using uint32 = unsigned int;
using int32 = int;
using uint64 = unsigned long long;
using int64 = long long;

using size_t = uint32;
using uintptr = uint32;

static_assert(sizeof(uint8) == 1, "uint8 must be one byte");
static_assert(sizeof(uint16) == 2, "uint16 must be two bytes");
static_assert(sizeof(uint32) == 4, "uint32 must be four bytes");
static_assert(sizeof(uint64) == 8, "uint64 must be eight bytes");

}  // namespace myos
