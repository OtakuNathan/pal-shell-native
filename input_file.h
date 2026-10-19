#pragma once
#include <cerrno>
#include <cstdio>
#include <string>
#include <system_error>
#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>
#include <filesystem>
#include <fcntl.h>
#include <io.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace dynabridge::pal_shell {
// A finite input, independent of pipe capacity. The child inherits its own
// descriptor; closing the parent's copy never truncates unread input.
class InputFile {
    FILE* file_ = nullptr;
public:
    explicit InputFile(const std::string& bytes) {
#ifdef _WIN32
        unsigned char random[16];
        if (BCryptGenRandom(nullptr, random, sizeof(random), BCRYPT_USE_SYSTEM_PREFERRED_RNG))
            throw std::runtime_error("stdin identity generation failed");
        std::string name = "pal-native-shell-input-";
        for (auto byte : random) {
            name += "0123456789abcdef"[byte >> 4];
            name += "0123456789abcdef"[byte & 15];
        }
        auto path = std::filesystem::temp_directory_path() / name;
        HANDLE handle = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_DELETE, nullptr, CREATE_NEW,
            FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
        if (handle == INVALID_HANDLE_VALUE) throw std::runtime_error("stdin file creation failed");
        int descriptor = _open_osfhandle(reinterpret_cast<intptr_t>(handle), _O_RDWR | _O_BINARY | _O_NOINHERIT);
        if (descriptor < 0) { CloseHandle(handle); throw std::runtime_error("stdin descriptor failed"); }
        file_ = _fdopen(descriptor, "w+b");
        if (!file_) _close(descriptor);
#else
        file_ = std::tmpfile();
#endif
        if (!file_) throw std::system_error(errno, std::generic_category(), "stdin file");
        try {
#ifdef _WIN32
            if (_setmode(fd(), _O_BINARY) < 0)
#else
            if (fcntl(fd(), F_SETFD, FD_CLOEXEC) < 0)
#endif
                throw std::system_error(errno, std::generic_category(), "stdin descriptor");
            if (std::fwrite(bytes.data(), 1, bytes.size(), file_) != bytes.size()
                || std::fflush(file_) || std::fseek(file_, 0, SEEK_SET))
                throw std::system_error(errno ? errno : EIO, std::generic_category(), "stdin write");
        } catch (...) { std::fclose(file_); throw; }
    }
    ~InputFile() { std::fclose(file_); }
    InputFile(const InputFile&) = delete;
    InputFile& operator=(const InputFile&) = delete;
    int fd() const {
#ifdef _WIN32
        return _fileno(file_);
#else
        return fileno(file_);
#endif
    }
};
}
