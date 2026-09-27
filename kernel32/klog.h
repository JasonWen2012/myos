// myos 32-bit kernel: a small log the shell can read back.
//
// Deliberately not a `printf` into a buffer: the kernel has no vsnprintf and adding
// one to serve a debug feature is the wrong direction.  What it has instead is a ring
// of short messages recorded at the handful of places where "did we get that far?"
// is the question -- boot milestones, mounting a volume, entering and leaving ring 3,
// and every panic.  `dmesg` prints them oldest first.

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 KLOG_ENTRIES = 32;
constexpr uint32 KLOG_MESSAGE_BYTES = 64;

// Copies the message into the ring.  Everything is truncated to KLOG_MESSAGE_BYTES-1
// characters, which is why the call sites keep their messages short.
void klog(const char* message);

uint32 klog_count();            // messages recorded since boot
void klog_report();             // `dmesg`

}  // namespace myos
