// myos 32-bit kernel: the log behind kernel32/klog.h.

#include "klog.h"

#include "console.h"
#include "libc.h"

namespace myos {
namespace {

char messages[KLOG_ENTRIES][KLOG_MESSAGE_BYTES];
uint32 recorded = 0;            // how many have ever been written
uint32 next_slot = 0;

}  // namespace

void klog(const char* message) {
    char* slot = messages[next_slot];
    uint32 index = 0;
    while (index + 1 < KLOG_MESSAGE_BYTES && message[index] != '\0') {
        slot[index] = message[index];
        ++index;
    }
    slot[index] = '\0';
    next_slot = (next_slot + 1) % KLOG_ENTRIES;
    ++recorded;
}

uint32 klog_count() {
    return recorded;
}

void klog_report() {
    const uint32 shown = recorded < KLOG_ENTRIES ? recorded : KLOG_ENTRIES;
    kprintf("dmesg: %u message(s), showing the last %u\n", recorded, shown);
    // The ring is printed oldest first: when it has wrapped, the oldest entry is the
    // one after the next write position.
    uint32 first = recorded < KLOG_ENTRIES ? 0 : next_slot;
    for (uint32 index = 0; index < shown; ++index) {
        const uint32 slot = (first + index) % KLOG_ENTRIES;
        console_puts("  ");
        console_puts(messages[slot]);
        console_putc('\n');
    }
}

}  // namespace myos
