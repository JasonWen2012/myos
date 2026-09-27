// myos 32-bit kernel: the user-copy checks behind kernel32/usercopy.h.

#include "usercopy.h"

#include "libc.h"
#include "paging.h"
#include "pmm.h"

namespace myos {

bool user_range_ok(uint32 address, uint32 bytes) {
    if (bytes == 0) {
        // An empty range is fine as long as its address is inside the window: a
        // syscall with a length of zero should not fail for pointing at the end of
        // a buffer.
        return address >= USER_BASE && address <= USER_LIMIT;
    }
    if (address < USER_BASE) {
        return false;
    }
    if (address + bytes < address) {
        return false;                       // wrapped around the address space
    }
    if (address + bytes > USER_LIMIT) {
        return false;
    }
    const uint32 last = (address + bytes - 1) & PAGE_ADDRESS_MASK;
    for (uint32 page = address & PAGE_ADDRESS_MASK; page <= last; page += PAGE_SIZE) {
        const uint32 flags = page_flags(page);
        if ((flags & PAGE_PRESENT) == 0 || (flags & PAGE_USER) == 0) {
            return false;
        }
    }
    return true;
}

bool user_string_ok(uint32 address, uint32 limit, uint32* out_length) {
    uint32 length = 0;
    while (length < limit) {
        // One byte at a time, through the same check every other user access uses:
        // a string that runs off the end of its page has to fail at the page, not
        // after a `strlen` has already read the kernel's memory.
        if (!user_range_ok(address + length, 1)) {
            return false;
        }
        const char* byte = reinterpret_cast<const char*>(address + length);
        if (*byte == '\0') {
            if (out_length != nullptr) {
                *out_length = length;
            }
            return true;
        }
        ++length;
    }
    return false;                           // no terminator within the limit
}

int32 copy_from_user(void* destination, uint32 source, uint32 bytes) {
    if (bytes == 0) {
        return 0;
    }
    if (!user_range_ok(source, bytes)) {
        return E_FAULT;
    }
    memcpy(destination, reinterpret_cast<const void*>(source), bytes);
    return static_cast<int32>(bytes);
}

int32 copy_to_user(uint32 destination, const void* source, uint32 bytes) {
    if (bytes == 0) {
        return 0;
    }
    if (!user_range_ok(destination, bytes)) {
        return E_FAULT;
    }
    memcpy(reinterpret_cast<void*>(destination), source, bytes);
    return static_cast<int32>(bytes);
}

}  // namespace myos
