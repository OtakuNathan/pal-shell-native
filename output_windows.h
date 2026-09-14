#pragma once
#include <windows.h>
#include <algorithm>
#include <cstdint>
#include <bcrypt.h>
#include <filesystem>
#include <fcntl.h>
#include <io.h>
#include <sys/stat.h>
#include <string>
#include <stdexcept>

namespace dynabridge::pal_shell {
class OutputFiles {
    std::filesystem::path directory_;
public:
    int out = -1, err = -1;
    std::string stdout_path, stderr_path;
    OutputFiles() {
        unsigned char random[16];
        if (BCryptGenRandom(nullptr, random, sizeof(random), BCRYPT_USE_SYSTEM_PREFERRED_RNG))
            throw std::runtime_error("output identity generation failed");
        std::string name = "pal-native-shell-";
        for (auto c : random) { name += "0123456789abcdef"[c >> 4]; name += "0123456789abcdef"[c & 15]; }
        directory_ = std::filesystem::temp_directory_path() / name;
        if (!std::filesystem::create_directory(directory_)) throw std::runtime_error("output directory exists");
        try {
            stdout_path = (directory_ / "stdout").u8string();
            stderr_path = (directory_ / "stderr").u8string();
            out = _wopen((directory_/"stdout").c_str(), _O_CREAT|_O_EXCL|_O_RDWR|_O_BINARY|_O_NOINHERIT, _S_IREAD|_S_IWRITE);
            err = _wopen((directory_/"stderr").c_str(), _O_CREAT|_O_EXCL|_O_RDWR|_O_BINARY|_O_NOINHERIT, _S_IREAD|_S_IWRITE);
            if (out < 0 || err < 0) throw std::runtime_error("output file creation failed");
        } catch (...) { cleanup(); throw; }
    }
    void cleanup() noexcept {
        if (out >= 0) _close(out);
        if (err >= 0) _close(err);
        std::error_code error;
        std::filesystem::remove_all(directory_, error);
    }
    ~OutputFiles() { cleanup(); }
    static std::uint64_t size(int fd) { auto n = _filelengthi64(fd); if (n < 0) throw std::runtime_error("output size failed"); return n; }
    static std::string read(int fd, std::size_t length) {
        auto position = _lseeki64(fd, 0, SEEK_CUR);
        _lseeki64(fd, 0, SEEK_SET);
        std::string result(length, '\0');
        std::size_t offset = 0;
        while (offset < length) {
            int n = _read(fd, &result[offset], static_cast<unsigned>(std::min<std::size_t>(length-offset, 65536)));
            if (n <= 0) { _lseeki64(fd, position, SEEK_SET); throw std::runtime_error("output read failed"); }
            offset += n;
        }
        _lseeki64(fd, position, SEEK_SET);
        return result;
    }
    void append(const char* bytes, std::size_t count, bool error = false) {
        int fd = error ? err : out;
        while (count) {
            int n = _write(fd, bytes, static_cast<unsigned>(count));
            if (n <= 0) throw std::runtime_error("output append failed");
            bytes += n; count -= n;
        }
    }
};
}
