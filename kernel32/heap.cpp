// myos 32-bit kernel: the heap behind kernel32/heap.h.

#include "heap.h"

#include "console.h"
#include "libc.h"
#include "panic.h"
#include "pmm.h"

namespace myos {
namespace {

constexpr uint32 HEAP_MAGIC = 0x48424C4B;       // 'K','L','B','H' little endian
constexpr uint32 HEAP_HEADER_BYTES = 16;
// The smallest payload worth splitting a block for: a header plus something a caller
// would not be annoyed to receive.
constexpr uint32 HEAP_MIN_PAYLOAD = 16;

struct Block {
    uint32 magic;               // +0
    uint32 size;                // +4  payload bytes, 16-byte aligned
    uint32 free;                // +8  1 when the block is available
    uint32 check;               // +12 magic ^ size ^ free
};
static_assert(sizeof(Block) == HEAP_HEADER_BYTES, "a block header is 16 bytes");

uint8* g_arena = nullptr;
uint32 g_arena_bytes = 0;
uint32 g_allocations = 0;
uint32 g_bad_frees = 0;

uint32 checksum(const Block& block) {
    return block.magic ^ block.size ^ block.free;
}

uint32 aligned_size(uint32 size) {
    if (size < HEAP_MIN_PAYLOAD) {
        size = HEAP_MIN_PAYLOAD;
    }
    return (size + (HEAP_MIN_ALIGN - 1)) & ~(HEAP_MIN_ALIGN - 1);
}

Block* first_block() {
    return reinterpret_cast<Block*>(g_arena);
}

// The block that follows in memory, or nullptr at the end of the arena.
Block* next_block(Block* block) {
    uint8* next = reinterpret_cast<uint8*>(block) + HEAP_HEADER_BYTES + block->size;
    if (next + HEAP_HEADER_BYTES > g_arena + g_arena_bytes) {
        return nullptr;
    }
    return reinterpret_cast<Block*>(next);
}

bool block_is_sane(const Block* block) {
    if (block == nullptr) {
        return false;
    }
    const uint8* start = reinterpret_cast<const uint8*>(block);
    if (start < g_arena || start + HEAP_HEADER_BYTES > g_arena + g_arena_bytes) {
        return false;
    }
    return block->magic == HEAP_MAGIC && block->check == checksum(*block) &&
           block->size >= HEAP_MIN_PAYLOAD &&
           block->size % HEAP_MIN_ALIGN == 0 &&
           start + HEAP_HEADER_BYTES + block->size <= g_arena + g_arena_bytes;
}

// Merges every free block that follows this one into it.
void coalesce(Block* block) {
    for (;;) {
        Block* next = next_block(block);
        if (next == nullptr || !block_is_sane(next) || next->free == 0) {
            return;
        }
        block->size += HEAP_HEADER_BYTES + next->size;
        block->check = checksum(*block);
    }
}

Block* find_previous(Block* wanted) {
    Block* previous = nullptr;
    for (Block* block = first_block(); block != wanted;) {
        previous = block;
        block = next_block(block);
        if (block == nullptr) {
            return nullptr;             // `wanted` is not in the chain
        }
    }
    return previous;
}

}  // namespace

void heap_init() {
    const uint32 physical = pmm_alloc_pages(HEAP_ARENA_BYTES / PAGE_SIZE);
    if (physical == 0) {
        g_arena = nullptr;
        g_arena_bytes = 0;
        return;
    }
    // The arena is identity mapped, so a physical address is a usable pointer.
    g_arena = reinterpret_cast<uint8*>(physical);
    g_arena_bytes = HEAP_ARENA_BYTES;
    g_allocations = 0;
    g_bad_frees = 0;

    Block* block = first_block();
    block->magic = HEAP_MAGIC;
    block->size = g_arena_bytes - HEAP_HEADER_BYTES;
    block->free = 1;
    block->check = checksum(*block);
}

void* kmalloc(uint32 size) {
    if (g_arena == nullptr) {
        return nullptr;
    }
    const uint32 wanted = aligned_size(size);
    for (Block* block = first_block(); block != nullptr; block = next_block(block)) {
        if (!block_is_sane(block)) {
            // Something already walked over a header.  Stopping here with the block
            // address is far more useful than a corrupted allocation later.
            panic("heap block header is corrupt");
        }
        if (block->free == 0 || block->size < wanted) {
            continue;
        }
        // Split only when the remainder can hold a header and a real payload.
        if (block->size >= wanted + HEAP_HEADER_BYTES + HEAP_MIN_PAYLOAD) {
            Block* rest = reinterpret_cast<Block*>(
                reinterpret_cast<uint8*>(block) + HEAP_HEADER_BYTES + wanted);
            rest->magic = HEAP_MAGIC;
            rest->size = block->size - wanted - HEAP_HEADER_BYTES;
            rest->free = 1;
            rest->check = checksum(*rest);
            block->size = wanted;
        }
        block->free = 0;
        block->check = checksum(*block);
        ++g_allocations;
        return reinterpret_cast<uint8*>(block) + HEAP_HEADER_BYTES;
    }
    return nullptr;
}

void kfree(void* pointer) {
    if (pointer == nullptr || g_arena == nullptr) {
        return;
    }
    uint8* payload = static_cast<uint8*>(pointer);
    if (payload < g_arena + HEAP_HEADER_BYTES || payload > g_arena + g_arena_bytes) {
        ++g_bad_frees;
        return;
    }
    Block* block = reinterpret_cast<Block*>(payload - HEAP_HEADER_BYTES);
    if (!block_is_sane(block) || block->free != 0) {
        ++g_bad_frees;
        return;
    }
    block->free = 1;
    block->check = checksum(*block);
    coalesce(block);
    Block* previous = find_previous(block);
    if (previous != nullptr && block_is_sane(previous) && previous->free != 0) {
        coalesce(previous);
    }
}

HeapStats heap_stats() {
    HeapStats stats;
    memset(&stats, 0, sizeof(stats));
    stats.arena_bytes = g_arena_bytes;
    stats.bad_frees = g_bad_frees;
    stats.allocations = g_allocations;
    if (g_arena == nullptr) {
        return stats;
    }
    for (Block* block = first_block(); block != nullptr; block = next_block(block)) {
        if (!block_is_sane(block)) {
            break;
        }
        ++stats.blocks;
        if (block->free != 0) {
            stats.free_bytes += block->size;
            if (block->size > stats.largest_free) {
                stats.largest_free = block->size;
            }
        } else {
            stats.used_bytes += block->size;
            ++stats.live_blocks;
        }
    }
    return stats;
}

void heap_report() {
    const HeapStats stats = heap_stats();
    if (g_arena == nullptr) {
        console_puts("heap: no arena (the page allocator had nothing to give)\n");
        return;
    }
    kprintf("heap: arena %u bytes, %u free (largest %u), %u used\n",
            stats.arena_bytes, stats.free_bytes, stats.largest_free,
            stats.used_bytes);
    kprintf("heap: %u block(s), %u live, %u allocation(s), %u bad free(s)\n",
            stats.blocks, stats.live_blocks, stats.allocations, stats.bad_frees);
}

}  // namespace myos
