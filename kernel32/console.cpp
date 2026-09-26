// myos 32-bit kernel: the text console.
//
// One console with two sinks: the VGA text buffer, which is what the user sees,
// and COM1, which is what a headless run and every automated test can read.  Both
// get the same characters, so a serial transcript is the screen.

#include "console.h"

#include "io.h"
#include "keyboard.h"
#include "libc.h"

namespace myos {
namespace {

constexpr uint16 VGA_BASE = 0xB800;
constexpr uint16 VGA_PORT_INDEX = 0x3D4;
constexpr uint16 VGA_PORT_DATA = 0x3D5;
constexpr uint32 VGA_COLUMNS = 80;
constexpr uint32 VGA_ROWS = 25;
constexpr uint16 SERIAL_BASE = 0x3F8;

// A cell is (attribute << 8) | character.  Getting that order backwards fills the
// screen with attribute 0x00 -- black on black -- which looks exactly like a
// screen that was never drawn.
inline uint16 cell(char c, uint8 attribute) {
    return static_cast<uint16>((static_cast<uint16>(attribute) << 8) |
                               static_cast<uint8>(c));
}

volatile uint16* const video = reinterpret_cast<volatile uint16*>(VGA_BASE * 16);

uint32 cursor_row = 0;
uint32 cursor_col = 0;
uint8 active_attribute = attr(COLOR_LIGHT_GREY, COLOR_BLACK);
uint32 serial_received = 0;

void vga_move_hardware_cursor() {
    uint16 position = static_cast<uint16>(cursor_row * VGA_COLUMNS + cursor_col);
    outb(VGA_PORT_INDEX, 0x0F);
    outb(VGA_PORT_DATA, static_cast<uint8>(position & 0xFF));
    outb(VGA_PORT_INDEX, 0x0E);
    outb(VGA_PORT_DATA, static_cast<uint8>(position >> 8));
}

void vga_scroll() {
    // Move rows 1..24 up one row and blank the last one.  memmove, not memcpy:
    // source and destination overlap, and copying forwards would repeat row 1
    // down the whole screen.
    volatile uint16* destination = video;
    volatile uint16* source = video + VGA_COLUMNS;
    for (uint32 i = 0; i < (VGA_ROWS - 1) * VGA_COLUMNS; ++i) {
        destination[i] = source[i];
    }
    uint16 blank = cell(' ', active_attribute);
    for (uint32 i = (VGA_ROWS - 1) * VGA_COLUMNS; i < VGA_ROWS * VGA_COLUMNS; ++i) {
        video[i] = blank;
    }
}

void vga_newline() {
    cursor_col = 0;
    if (++cursor_row >= VGA_ROWS) {
        vga_scroll();
        cursor_row = VGA_ROWS - 1;
    }
}

void vga_putc(char c) {
    if (c == '\n') {
        vga_newline();
    } else if (c == '\r') {
        cursor_col = 0;
    } else if (c == '\b') {
        if (cursor_col > 0) {
            --cursor_col;
            video[cursor_row * VGA_COLUMNS + cursor_col] = cell(' ', active_attribute);
        }
    } else {
        video[cursor_row * VGA_COLUMNS + cursor_col] = cell(c, active_attribute);
        if (++cursor_col >= VGA_COLUMNS) {
            vga_newline();
        }
    }
    vga_move_hardware_cursor();
}

}  // namespace

// ------------------------------------------------------------------- serial

void serial_init() {
    outb(SERIAL_BASE + 1, 0x00);            // no interrupts yet: the IDT is not up
    outb(SERIAL_BASE + 3, 0x80);            // DLAB: the next two writes are the divisor
    outb(SERIAL_BASE + 0, 0x01);            // divisor 1 -> 115200 baud
    outb(SERIAL_BASE + 1, 0x00);
    outb(SERIAL_BASE + 3, 0x03);            // 8 bits, no parity, one stop, DLAB off
    // FIFO on, both queues cleared, and a one-byte trigger level.  The trigger
    // level matters for typing: at the usual 14 bytes, a single keystroke sits in
    // the FIFO until the character-timeout interrupt fires, which the user feels as
    // a sluggish console.
    outb(SERIAL_BASE + 2, 0x07);
    outb(SERIAL_BASE + 4, 0x0B);            // DTR, RTS and OUT2
}

bool serial_rx_ready() {
    return (inb(SERIAL_BASE + 5) & 0x01) != 0;
}

uint8 serial_read_byte() {
    return inb(SERIAL_BASE + 0);
}

void serial_enable_rx_interrupt() {
    outb(SERIAL_BASE + 1, 0x01);            // received data available
}

void serial_handle_rx() {
    // Drain everything the UART has, not one byte: with the FIFO enabled a single
    // interrupt can be reporting several bytes, and reading one of them leaves the
    // rest sitting there until another interrupt happens to arrive -- which shows up
    // as characters dropping out of a typed line.  Reading the data register is also
    // what clears the interrupt, so the loop ends when the UART says it is empty.
    while (serial_rx_ready()) {
        key_push(static_cast<char>(serial_read_byte()));
        ++serial_received;
    }
}

uint32 serial_bytes_received() {
    return serial_received;
}

void serial_putc(char c) {
    // Wait for the transmit register.  A missing or dead UART would spin here
    // forever; QEMU always has one, and the alternative (silently dropping output)
    // would hide exactly the failures this console exists to report.
    while ((inb(SERIAL_BASE + 5) & 0x20) == 0) {
    }
    outb(SERIAL_BASE + 0, static_cast<uint8>(c));
}

// ------------------------------------------------------------------ console

void console_init() {
    cursor_row = 0;
    cursor_col = 0;
    active_attribute = attr(COLOR_LIGHT_GREY, COLOR_BLACK);
    console_clear();
}

void console_clear() {
    uint16 blank = cell(' ', active_attribute);
    for (uint32 i = 0; i < VGA_ROWS * VGA_COLUMNS; ++i) {
        video[i] = blank;
    }
    cursor_row = 0;
    cursor_col = 0;
    vga_move_hardware_cursor();
}

void console_putc(char c) {
    vga_putc(c);
    // Serial terminals need the carriage return that a VGA text buffer does not:
    // LF alone moves down a line without returning to column 0.
    if (c == '\n') {
        serial_putc('\r');
    }
    serial_putc(c);
}

void console_write(const char* text, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        console_putc(text[i]);
    }
}

void console_puts(const char* text) {
    while (*text != '\0') {
        console_putc(*text++);
    }
}

void console_set_color(uint8 attribute) {
    active_attribute = attribute;
}

uint8 console_color() {
    return active_attribute;
}

void console_put_dec(uint32 value) {
    char buffer[12];
    utoa(value, buffer);
    console_puts(buffer);
}

void console_put_hex8(uint8 value) {
    static const char digits[] = "0123456789ABCDEF";
    console_putc(digits[(value >> 4) & 0x0F]);
    console_putc(digits[value & 0x0F]);
}

void console_put_hex16(uint16 value) {
    console_put_hex8(static_cast<uint8>(value >> 8));
    console_put_hex8(static_cast<uint8>(value & 0xFF));
}

void console_put_hex32(uint32 value) {
    console_put_hex16(static_cast<uint16>(value >> 16));
    console_put_hex16(static_cast<uint16>(value & 0xFFFF));
}

uint32 decimal_digits(uint32 value) {
    uint32 digits = 1;
    while (value >= 10) {
        value /= 10;
        ++digits;
    }
    return digits;
}

void kprintf(const char* format, ...) {
    __builtin_va_list args;
    __builtin_va_start(args, format);

    for (const char* p = format; *p != '\0'; ++p) {
        if (*p != '%') {
            console_putc(*p);
            continue;
        }
        ++p;
        // A width, and a leading zero meaning "pad with zeroes" -- enough for the
        // fixed-width hex a memory map wants, without carrying a printf.
        bool zero_pad = false;
        uint32 width = 0;
        if (*p == '0') {
            zero_pad = true;
            ++p;
        }
        while (*p >= '0' && *p <= '9') {
            width = width * 10 + static_cast<uint32>(*p - '0');
            ++p;
        }
        switch (*p) {
            case 's': {
                const char* text = __builtin_va_arg(args, const char*);
                console_puts(text != nullptr ? text : "(null)");
                break;
            }
            case 'c':
                console_putc(static_cast<char>(__builtin_va_arg(args, int)));
                break;
            case 'd': {
                int32 value = __builtin_va_arg(args, int32);
                if (value < 0) {
                    console_putc('-');
                    value = -value;
                }
                const uint32 digits = decimal_digits(static_cast<uint32>(value));
                while (zero_pad && width > digits) {
                    console_putc('0');
                    --width;
                }
                console_put_dec(static_cast<uint32>(value));
                break;
            }
            case 'u': {
                const uint32 value = __builtin_va_arg(args, uint32);
                const uint32 digits = decimal_digits(value);
                while (zero_pad && width > digits) {
                    console_putc('0');
                    --width;
                }
                console_put_dec(value);
                break;
            }
            case 'x':
            case 'p': {
                const uint32 value = __builtin_va_arg(args, uint32);
                // console_put_hex32 always writes exactly eight digits, so the only
                // width with anything left to pad is one larger than that.  Padding
                // by the value's *own* digit count double-counted the leading zeroes
                // the helper already emits: `%08x` of 0x0885097F came out as
                // "00885097F", nine characters, and the host comparing that with the
                // eight it computed saw a mismatch that was not there.
                if (zero_pad) {
                    uint32 remaining = width;
                    while (remaining > 8) {
                        console_putc('0');
                        --remaining;
                    }
                }
                console_put_hex32(value);
                break;
            }
            case '%':
                console_putc('%');
                break;
            case '\0':
                --p;                        // trailing '%': stop at the terminator
                break;
            default:
                console_putc('%');
                console_putc(*p);
                break;
        }
    }
    __builtin_va_end(args);
}

}  // namespace myos
