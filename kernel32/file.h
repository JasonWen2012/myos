// myos 32-bit kernel: a tiny open-file table over the filesystem.
//
// The shell could call fs_read_data directly, and for `cat` it nearly does.  The
// reason this layer exists is that "open, read some, read some more, close" is the
// shape everything else in an OS expects, and having it here means the shell never
// has to keep a file's bytes in its own hands -- which is how a shell ends up
// copying a whole file into a buffer it does not have.

#pragma once

#include "fs.h"
#include "types.h"

namespace myos {

constexpr uint32 FILE_MAX_OPEN = 8;

// Opens a file for reading, or returns a negative FsError.  Directories are
// refused with FS_IS_DIR: nothing here can walk one, and pretending otherwise
// would be the first step towards a second directory reader.
int32 file_open(const char* path);

// Copies up to `count` bytes and returns how many, 0 at end of file, or a negative
// FsError.  A short read is not an error: running out of file is normal.
int32 file_read(int32 fd, void* buffer, uint32 count);

int32 file_close(int32 fd);

// Bytes left between the read position and the end of the file, or a negative
// FsError.  `cat` uses it to print the size it is about to copy.
int32 file_remaining(int32 fd);
int32 file_size(int32 fd);

// How many descriptors are open, for the `fstest` and self-test reports.
uint32 file_open_count();

}  // namespace myos
