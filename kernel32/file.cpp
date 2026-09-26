// myos 32-bit kernel: the open-file table behind kernel32/file.h.

#include "file.h"

#include "libc.h"

namespace myos {
namespace {

struct OpenFile {
    bool used;
    uint32 inode;
    uint32 offset;
    Inode inode_data;               // the inode is small; keeping it here avoids a
                                    // disk read for every read() call
};

OpenFile g_files[FILE_MAX_OPEN];

OpenFile* lookup(int32 fd) {
    if (fd < 0 || static_cast<uint32>(fd) >= FILE_MAX_OPEN) {
        return nullptr;
    }
    OpenFile& file = g_files[fd];
    return file.used ? &file : nullptr;
}

}  // namespace

int32 file_open(const char* path) {
    uint32 index = 0;
    const int32 status = fs_resolve(path, &index);
    if (status < 0) {
        return status;
    }
    Inode inode;
    const int32 read_status = fs_inode_read(index, &inode);
    if (read_status < 0) {
        return read_status;
    }
    if (inode.type == FS_TYPE_DIR) {
        return FS_IS_DIR;
    }
    if (inode.type != FS_TYPE_FILE) {
        return FS_NO_ENT;
    }
    for (uint32 slot = 0; slot < FILE_MAX_OPEN; ++slot) {
        if (!g_files[slot].used) {
            g_files[slot].used = true;
            g_files[slot].inode = index;
            g_files[slot].offset = 0;
            g_files[slot].inode_data = inode;
            return static_cast<int32>(slot);
        }
    }
    return FS_BAD_FD;
}

int32 file_read(int32 fd, void* buffer, uint32 count) {
    OpenFile* file = lookup(fd);
    if (file == nullptr) {
        return FS_BAD_FD;
    }
    const int32 got = fs_read_data(file->inode_data, file->offset, buffer, count);
    if (got > 0) {
        file->offset += static_cast<uint32>(got);
    }
    return got;
}

int32 file_close(int32 fd) {
    OpenFile* file = lookup(fd);
    if (file == nullptr) {
        return FS_BAD_FD;
    }
    file->used = false;
    file->inode = 0;
    file->offset = 0;
    memset(&file->inode_data, 0, sizeof(Inode));
    return FS_OK;
}

int32 file_remaining(int32 fd) {
    OpenFile* file = lookup(fd);
    if (file == nullptr) {
        return FS_BAD_FD;
    }
    if (file->offset >= file->inode_data.size) {
        return 0;
    }
    return static_cast<int32>(file->inode_data.size - file->offset);
}

int32 file_size(int32 fd) {
    OpenFile* file = lookup(fd);
    if (file == nullptr) {
        return FS_BAD_FD;
    }
    return static_cast<int32>(file->inode_data.size);
}

uint32 file_open_count() {
    uint32 open = 0;
    for (uint32 slot = 0; slot < FILE_MAX_OPEN; ++slot) {
        if (g_files[slot].used) {
            ++open;
        }
    }
    return open;
}

}  // namespace myos
