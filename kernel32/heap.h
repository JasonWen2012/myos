// myos 32-bit kernel: the kernel heap.
//
// A first-fit allocator over one contiguous arena of physical pages, reserved from
// the page allocator at boot.  Contiguous on purpose: a heap whose pages are
// scattered is not a heap, and reserving up front means `kmalloc` can never fail
// because the memory it wanted was in the wrong shape.
//
// Every block carries a 16-byte header with a magic number and a checksum of the
// header's own fields, because the interesting failure of a heap is not "it ran out"
// but "something wrote over the header and the next allocation walked into it".

#pragma once

#include "types.h"

namespace myos {

constexpr uint32 HEAP_ARENA_BYTES = 1u * 1024u * 1024u;
constexpr uint32 HEAP_MIN_ALIGN = 16;           // every payload is 16-byte aligned

struct HeapStats {
    uint32 arena_bytes;
    uint32 used_bytes;          // payload bytes handed out
    uint32 free_bytes;          // payload bytes free (headers are not counted)
    uint32 largest_free;        // biggest single allocation still possible
    uint32 blocks;              // used plus free blocks
    uint32 live_blocks;
    uint32 allocations;
    uint32 bad_frees;           // kfree calls on something that was not a live block
};

// Reserves the arena.  After this, `kmalloc` works or reports zero.
void heap_init();

// Returns an aligned block, or nullptr when the arena cannot satisfy the request.
void* kmalloc(uint32 size);
void kfree(void* pointer);

HeapStats heap_stats();
void heap_report();

}  // namespace myos
