// myos 32-bit kernel: the TSS behind kernel32/tss.h.

#include "tss.h"

#include "gdt.h"
#include "libc.h"

namespace myos {

static inline uint16 read_task_register() {
    uint16 value;
    asm volatile("str %0" : "=rm"(value));
    return value;
}

static inline void load_task_register(uint16 selector) {
    asm volatile("ltr %0" : : "rm"(selector));
}

namespace {

Tss tss;
uint32 g_kernel_stack = 0;
uint32 g_boot_kernel_stack = 0;

}  // namespace

void tss_init(uint32 kernel_stack_top) {
    memset(&tss, 0, sizeof(tss));
    tss.ss0 = GDT_SELECTOR_DATA;
    tss.esp0 = kernel_stack_top;
    // No I/O permission bitmap: the field holds the offset of the end of the TSS, and
    // the CPU reads it as "there is none" -- so ring 3 cannot touch a port.
    tss.iomap_base = static_cast<uint16>(sizeof(Tss));
    g_kernel_stack = kernel_stack_top;
    g_boot_kernel_stack = kernel_stack_top;
    load_task_register(GDT_SELECTOR_TSS);
}

void tss_set_kernel_stack(uint32 kernel_stack_top) {
    tss.esp0 = kernel_stack_top;
    g_kernel_stack = kernel_stack_top;
}

void tss_restore_boot_stack() {
    tss_set_kernel_stack(g_boot_kernel_stack);
}

uint32 tss_kernel_stack() {
    return g_kernel_stack;
}

uint16 tss_selector() {
    return GDT_SELECTOR_TSS;
}

uint32 tss_address() {
    return reinterpret_cast<uint32>(&tss);
}

uint16 tss_loaded_selector() {
    return read_task_register();
}

}  // namespace myos
