// Installed root-owned, mode 0755 (never setuid). Invoked by actual sudo.
// The monitor outlives its unprivileged caller and reaps the root process group.
#include <cerrno>
#include <csignal>
#include <cstdlib>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t stopping = 0;
static void stop(int) { stopping = 1; }
int main(int argc, char** argv) {
    if (argc != 3 || geteuid() != 0 || argv[1][0] != '/') return 126;
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
