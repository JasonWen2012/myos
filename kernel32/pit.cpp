// myos 32-bit kernel: the 8253/8254 programmable interval timer.

#include "pit.h"

#include "idt.h"
#include "io.h"
#include "pic.h"
#include "sched.h"
#include "types.h"

namespace myos {
namespace {

constexpr uint16 PIT_CHANNEL0 = 0x40;
constexpr uint16 PIT_COMMAND = 0x43;
volatile uint64 tick_count = 0;

void handle_tick() {
    ++tick_count;
    // The timer's other job: it is the only thing that can take the CPU away from a
    // task that does not want to give it up.  sched_tick() only counts and switches --
    // it runs in interrupt context, where nothing may block and nothing may allocate.
    sched_tick();
}

}  // namespace

void pit_init() {
    const uint32 divisor = PIT_FREQUENCY / PIT_TICK_HZ;
    outb(PIT_COMMAND, 0x36);                // channel 0, lobyte/hibyte, mode 3, binary
    outb(PIT_CHANNEL0, static_cast<uint8>(divisor & 0xFF));
    outb(PIT_CHANNEL0, static_cast<uint8>((divisor >> 8) & 0xFF));
    irq_install_handler(PIC_IRQ_TIMER, handle_tick);
}

uint64 pit_ticks() {
    return tick_count;
}

uint32 pit_ticks_per_second() {
    return PIT_TICK_HZ;
}

}  // namespace myos
