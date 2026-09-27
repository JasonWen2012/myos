// myos 32-bit kernel: the ring-3 loader behind kernel32/user.h.

#include "user.h"

#include "bootinfo.h"
#include "console.h"
#include "fs.h"
#include "heap.h"
#include "io.h"
#include "klog.h"
#include "libc.h"
#include "paging.h"
#include "pmm.h"
#include "sched.h"
#include "syscall.h"
#include "task.h"
#include "tss.h"
#include "usercopy.h"

extern "C" {
// user.asm: returns only when the program has exited, with its exit code.
myos::uint32 run_user_image(myos::uint32 entry, myos::uint32 user_stack_top);
}

namespace myos {
namespace {

uint32 g_programs = 0;
uint32 g_pages = 0;                 // user pages currently mapped

uint32 page_count(uint32 bytes) {
    return (bytes + PAGE_SIZE - 1) / PAGE_SIZE;
}

// Maps `count` fresh pages at `address`, zeroing each one first: a page from the
// allocator holds whatever the last owner left there, and handing that to ring 3
// would be a way to read kernel memory from a program.
bool map_user_pages(uint32 address, uint32 count, uint32* out_pages) {
    for (uint32 index = 0; index < count; ++index) {
        const uint32 physical = pmm_alloc_page();
        if (physical == 0) {
            return false;
        }
        memset(reinterpret_cast<void*>(physical), 0, PAGE_SIZE);
        if (!page_map(address + index * PAGE_SIZE, physical,
                      PAGE_PRESENT | PAGE_WRITE | PAGE_USER)) {
            pmm_free_page(physical);
            return false;
        }
        ++g_pages;
    }
    *out_pages = count;
    return true;
}

// Unmaps a range and gives the frames back, so `run` twice in a row is the same as
// running once as far as the allocator is concerned.
void unmap_user_pages(uint32 address, uint32 count) {
    for (uint32 index = 0; index < count; ++index) {
        const uint32 page = address + index * PAGE_SIZE;
        const uint32 physical = page_translate(page);
        page_unmap(page);
        if (physical != 0) {
            pmm_free_page(physical & PAGE_ADDRESS_MASK);
        }
        if (g_pages > 0) {
            --g_pages;
        }
    }
}

}  // namespace

bool user_image_ok(const uint8* header, uint32 bytes, uint32* out_entry,
                   uint32* out_size) {
    if (header == nullptr || bytes < sizeof(ImageHeader)) {
        return false;
    }
    const ImageHeader* info = reinterpret_cast<const ImageHeader*>(header);
    if (info->magic != IMAGE_MAGIC) {
        return false;
    }
    if (info->arch != USER_IMAGE_ARCH) {
        // A kernel image is not a user program, and this is where that shows up.
        return false;
    }
    uint8 sum = 0;
    for (uint32 index = 0; index < IMAGE_HEADER_SIZE; ++index) {
        sum = static_cast<uint8>(sum + header[index]);
    }
    if (sum != 0) {
        return false;
    }
    if (info->size < IMAGE_HEADER_SIZE || info->size > USER_IMAGE_MAX) {
        return false;
    }
    if (info->entry < IMAGE_HEADER_SIZE || info->entry >= info->size) {
        return false;
    }
    if (out_entry != nullptr) {
        *out_entry = info->entry;
    }
    if (out_size != nullptr) {
        *out_size = info->size;
    }
    return true;
}

int32 exec_user(const char* path, UserRun* report) {
    uint32 index = 0;
    int32 status = fs_resolve(path, &index);
    if (status < 0) {
        return status;
    }
    Inode inode;
    status = fs_inode_read(index, &inode);
    if (status < 0) {
        return status;
    }
    if (inode.type != FS_TYPE_FILE) {
        return FS_IS_DIR;
    }
    if (inode.size < USER_IMAGE_HEADER_BYTES || inode.size > USER_IMAGE_MAX) {
        return E_INVAL;
    }

    // The image is read through the heap rather than straight into user pages: the
    // header has to be validated before a single byte of it is mapped, and a
    // half-mapped program is not something to leave lying around.
    uint8* image = static_cast<uint8*>(kmalloc(inode.size));
    if (image == nullptr) {
        return E_NO_MEM;
    }
    const int32 read = fs_read_data(inode, 0, image, inode.size);
    if (read != static_cast<int32>(inode.size)) {
        kfree(image);
        return read < 0 ? read : FS_IO;
    }
    uint32 entry = 0;
    uint32 size = 0;
    if (!user_image_ok(image, inode.size, &entry, &size)) {
        kfree(image);
        klog("refused a user image whose header did not check out");
        return E_INVAL;
    }

    const uint32 image_pages = page_count(size);
    const uint32 stack_pages = page_count(USER_STACK_BYTES);
    uint32 mapped = 0;
    if (!map_user_pages(USER_BASE, image_pages, &mapped)) {
        unmap_user_pages(USER_BASE, mapped);
        kfree(image);
        return E_NO_MEM;
    }
    uint32 stack_mapped = 0;
    if (!map_user_pages(USER_STACK_BOTTOM, stack_pages, &stack_mapped)) {
        unmap_user_pages(USER_STACK_BOTTOM, stack_mapped);
        unmap_user_pages(USER_BASE, image_pages);
        kfree(image);
        return E_NO_MEM;
    }
    // The pages were zeroed when they were mapped; this copies the program over the
    // ones that hold it.  Identity mapping is what makes writing to the physical
    // address the same thing as writing through the user's page.
    memcpy(reinterpret_cast<void*>(USER_BASE), image, size);
    kfree(image);

    // A ring-0 stack for the CPU to switch to on every syscall and interrupt from
    // ring 3.  Handing the shell's own stack to the TSS would put the syscall frame
    // on top of the shell's locals.
    const uint32 kernel_stack = pmm_alloc_pages(TSS_KERNEL_STACK_PAGES);
    if (kernel_stack == 0) {
        unmap_user_pages(USER_STACK_BOTTOM, stack_pages);
        unmap_user_pages(USER_BASE, image_pages);
        return E_NO_MEM;
    }
    tss_set_kernel_stack(kernel_stack + TSS_KERNEL_STACK_PAGES * PAGE_SIZE);

    kprintf("run: %s, %u bytes at %p, stack %p, entry offset %x\n", path, size,
            USER_BASE, USER_STACK_BOTTOM + USER_STACK_BYTES, entry);
    klog("entering ring 3");

    ++g_programs;
    const uint32 syscalls_before = syscall_count();
    // Preemption is suspended for the ring-3 run.  This is the shape phase 2a leaves
    // behind, and the reason is narrow: the user program is not a task of its own yet,
    // it is a synchronous excursion by whichever task called `run`, on a temporary
    // kernel stack the TSS was pointed at a few lines up.  A timer that switched away
    // from here would leave esp0 naming the *other* task's stack, and this excursion's
    // kernel stack would be nobody's.  Ticks are still counted while it runs; only the
    // switch is held off.  Phase 2b makes the user program a task and removes this.
    sched_pause();
    const uint32 exit_code = run_user_image(USER_BASE + entry,
                                            USER_STACK_BOTTOM + USER_STACK_BYTES);
    sched_resume();
    // Back in the kernel, on the shell's stack, with the address space the user used
    // no longer mapped: ring 3 cannot read a byte of what it was doing.
    //
    // The return path deliberately arrives with interrupts disabled (it runs on a
    // stack it has just restored, and a timer tick in the middle of that would be a
    // tick on a half-restored frame).  Turning them back on is the kernel's decision,
    // and it has to happen here: the shell waits for input with `hlt`, so a kernel
    // that comes back with IF clear prints its report and then never reads another
    // keystroke.
    interrupts_enable();
    if (report != nullptr) {
        report->exit_code = exit_code;
        report->pages = image_pages + stack_pages;
        report->syscalls = syscall_count() - syscalls_before;
        report->bytes = size;
    }
    kprintf("run: %s exited with code %u\n", path, exit_code);
    klog("back from ring 3");

    unmap_user_pages(USER_STACK_BOTTOM, stack_pages);
    unmap_user_pages(USER_BASE, image_pages);
    pmm_free_pages(kernel_stack, TSS_KERNEL_STACK_PAGES);
    // esp0 goes back to the *calling task's* stack, not to the boot stack: with a
    // scheduler the boot stack is only correct for task 0, and a kernel task that
    // runs a program must get its own stack back for its next ring-3 trap.
    task_restore_kernel_stack();
    return FS_OK;
}

uint32 user_programs_run() {
    return g_programs;
}

uint32 user_pages_mapped() {
    return g_pages;
}

void user_report() {
    kprintf("user: %u program(s) run, %u page(s) mapped for user mode now\n",
            g_programs, g_pages);
    syscall_report();
}

}  // namespace myos
