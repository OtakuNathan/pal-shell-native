// Installed root-owned, mode 0755 (never setuid). Invoked by actual sudo.
// The monitor outlives its unprivileged caller and reaps the root process group.
#include <cerrno>
#include <csignal>
#include <cstdlib>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <cstring>
#include <memory>
#include <sys/stat.h>
#include "input_file.h"
#ifdef __APPLE__
#include <CommonCrypto/CommonDigest.h>
#endif

static volatile sig_atomic_t stopping = 0;
static void stop(int) { stopping = 1; }
int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--stdin-capabilities") == 0) {
#ifdef __APPLE__
        std::puts("1");
#else
        std::puts("0");
#endif
        return 0;
    }
    if ((argc != 3 && argc != 5) || geteuid() != 0 || argv[1][0] != '/') return 126;
    std::unique_ptr<dynabridge::pal_shell::InputFile> approved_input;
    if (argc == 5) {
#ifdef __APPLE__
        try {
            std::size_t parsed = 0;
            const auto length = std::stoull(argv[3], &parsed);
            if (parsed != std::strlen(argv[3]) || length > 65536 || std::strlen(argv[4]) != 64) return 126;
            struct stat info{};
            if (fstat(0, &info) || !S_ISREG(info.st_mode) || info.st_size != static_cast<off_t>(length)
                || lseek(0, 0, SEEK_SET) < 0) return 126;
            std::string input(length, '\0');
            std::size_t offset = 0;
            while (offset < length) {
                const auto count = read(0, &input[offset], length - offset);
                if (count < 0 && errno == EINTR) continue;
                if (count <= 0) return 126;
                offset += static_cast<std::size_t>(count);
            }
            unsigned char digest[CC_SHA256_DIGEST_LENGTH];
            CC_SHA256(input.data(), static_cast<CC_LONG>(input.size()), digest);
            std::string actual;
            for (auto byte : digest) {
                actual += "0123456789abcdef"[byte >> 4];
                actual += "0123456789abcdef"[byte & 15];
            }
            if (actual != argv[4]) return 126;
            // The worker must not be able to modify input after verification.
            approved_input = std::make_unique<dynabridge::pal_shell::InputFile>(input);
            if (dup2(approved_input->fd(), 0) < 0) return 126;
        } catch (...) { return 126; }
#else
        return 126;
#endif
    }
    const pid_t parent = getppid();
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGHUP, stop);
    const pid_t child = fork();
    if (child < 0) return 126;
    if (!child) {
        setpgid(0, 0);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGHUP, SIG_DFL);
        execl(argv[1], argv[1], "-lc", argv[2], static_cast<char*>(nullptr));
        _exit(126);
    }
    setpgid(child, child);
    int status = 0, ticks = 0;
    for (;;) {
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child || (result < 0 && errno != EINTR)) break;
        if (getppid() != parent) stopping = 1;
        if (stopping) {
            kill(-child, ticks++ < 20 ? SIGTERM : SIGKILL);
        }
        usleep(50000);
    }
    // No background root process in this group survives a one-command grant.
    kill(-child, SIGKILL);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
