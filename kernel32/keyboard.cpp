// myos 32-bit kernel: the keyboard and the input queue.

#include "keyboard.h"

#include "console.h"
#include "cpu.h"
#include "idt.h"
#include "io.h"
#include "pic.h"
#include "types.h"

namespace myos {
namespace {

constexpr uint16 KBD_DATA_PORT = 0x60;
constexpr uint16 KBD_STATUS_PORT = 0x64;

// Set-1 scan codes.  A zero means "no ASCII form": modifiers, function keys and
// the like, which the line editor has no use for.
constexpr char UNSHIFTED[128] = {
    0,   27,  '1', '2', '3', '4', '5', '6', '7', '8', '9', '0', '-', '=', '\b', '\t',
    'q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', '[', ']', '\n', 0,  'a', 's',
    'd', 'f', 'g', 'h', 'j', 'k', 'l', ';', '\'', '`', 0,  '\\', 'z', 'x', 'c', 'v',
    'b', 'n', 'm', ',', '.', '/', 0,   '*', 0,  ' ', 0,  0,   0,   0,   0,   0,
    0,   0,   0,   0,   0,   0,   0,   '7', '8', '9', '-', '4', '5', '6', '+', '1',
    '2', '3', '0', '.', 0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
    0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
    0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
};

constexpr char SHIFTED[128] = {
    0,   27,  '!', '@', '#', '$', '%', '^', '&', '*', '(', ')', '_', '+', '\b', '\t',
    'Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P', '{', '}', '\n', 0,  'A', 'S',
    'D', 'F', 'G', 'H', 'J', 'K', 'L', ':', '"', '~', 0,  '|', 'Z', 'X', 'C', 'V',
    'B', 'N', 'M', '<', '>', '?', 0,   '*', 0,  ' ', 0,   0,   0,   0,   0,   0,
    0,   0,   0,   0,   0,   0,   0,   '7', '8', '9', '-', '4', '5', '6', '+', '1',
    '2', '3', '0', '.', 0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
    0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
    0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
};

constexpr uint8 SCANCODE_SHIFT_LEFT = 0x2A;
constexpr uint8 SCANCODE_SHIFT_RIGHT = 0x36;
constexpr uint8 SCANCODE_CAPS_LOCK = 0x3A;
constexpr uint8 SCANCODE_EXTENDED = 0xE0;

char queue[KEY_QUEUE_SIZE];
uint32 queue_head = 0;
uint32 queue_tail = 0;
bool shift_held = false;
bool caps_on = false;
bool saw_extended = false;
uint32 scancode_count = 0;

}  // namespace

void key_push(char c) {
    if (c == '\0') {
        return;
    }
    const uint32 next = (queue_head + 1) % KEY_QUEUE_SIZE;
    if (next == queue_tail) {
        return;                             // full: drop, rather than overwrite
    }
    queue[queue_head] = c;
    queue_head = next;
}

bool key_available() {
    return queue_head != queue_tail;
}

char key_get() {
    // Interrupts are enabled by the time anyone calls this, so an empty queue is
    // what `hlt` is for: the next keystroke (or timer tick) wakes the CPU, and this
    // loop costs nothing while it waits.
    while (!key_available()) {
        asm volatile("hlt");
    }
    const char c = queue[queue_tail];
    queue_tail = (queue_tail + 1) % KEY_QUEUE_SIZE;
    return c;
}

char keyboard_translate(uint8 scancode) {
    if (scancode == SCANCODE_EXTENDED) {
        saw_extended = true;
        return 0;
    }
    const bool released = (scancode & 0x80) != 0;
    const uint8 code = static_cast<uint8>(scancode & 0x7F);

    if (code == SCANCODE_SHIFT_LEFT || code == SCANCODE_SHIFT_RIGHT) {
        shift_held = !released;
        return 0;
    }
    if (code == SCANCODE_CAPS_LOCK) {
        if (!released) {
            caps_on = !caps_on;
        }
        return 0;
    }
    if (released) {
        saw_extended = false;
        return 0;
    }
    if (saw_extended) {
        saw_extended = false;               // arrows and friends: no ASCII form
        return 0;
    }
    const bool upper = shift_held != caps_on;
    const char c = upper ? SHIFTED[code] : UNSHIFTED[code];
    // Shift must not capitalise the digits row's symbols twice: the tables already
    // carry both forms, so `upper` only chooses between them.
    return c;
}

void keyboard_handle_scancode(uint8 scancode) {
    ++scancode_count;
    key_push(keyboard_translate(scancode));
}

void keyboard_init() {
    irq_install_handler(PIC_IRQ_KEYBOARD, []() {
        // Port 0x60 is read here rather than in the dispatcher so the dispatcher
        // stays device-agnostic; the read is also what clears the 8042.
        keyboard_handle_scancode(inb(KBD_DATA_PORT));
    });
    // COM1 shares the queue: a kernel with a serial console has two keyboards, and
    // the shell should not care which one a line was typed on.  The UART's receive
    // interrupt is enabled only now, when there is an IDT to deliver it.
    irq_install_handler(PIC_IRQ_SERIAL, serial_handle_rx);
    serial_enable_rx_interrupt();
}

uint32 keyboard_scancode_count() {
    return scancode_count;
}

}  // namespace myos
