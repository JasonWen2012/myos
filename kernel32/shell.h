// myos 32-bit kernel: the command shell.

#pragma once

namespace myos {

// Reads lines from the shared input queue (keyboard and serial alike), echoes
// them, and dispatches the first word against a command table.  Never returns.
void shell_run();

}  // namespace myos
