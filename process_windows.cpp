#include "process_posix.h"
#include <windows.h>
#include <algorithm>
#include <io.h>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <vector>
#include <stdexcept>

namespace dynabridge::pal_shell {
namespace {
struct Process { HANDLE process, job; std::filesystem::path script; };
std::mutex guard;
std::map<pid_t, Process> processes;
std::wstring wide(const std::string& text) {
    if (text.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), int(text.size()), nullptr, 0);
    if (!n) throw std::runtime_error("invalid UTF-8 process argument");
    std::wstring result(n, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), int(text.size()), &result[0], n);
    return result;
}
void check(BOOL value, const char* step) { if (!value) throw std::runtime_error(std::string(step)+": "+std::to_string(GetLastError())); }
struct Handle {
    HANDLE h = nullptr;
    ~Handle() { if (h && h != INVALID_HANDLE_VALUE) CloseHandle(h); }
    HANDLE take() { auto result = h; h = nullptr; return result; }
};
}
Child spawn_child(const std::string& shell, const std::string& command, const std::string& cwd,
                  bool tty, int stdout_fd, int, bool) {
    if (tty) throw std::runtime_error("pty_unsupported: Windows prototype has no ConPTY");
    // Keep the approved script beside this session's private output, not in argv.
    wchar_t output[32768];
    auto len = GetFinalPathNameByHandleW(reinterpret_cast<HANDLE>(_get_osfhandle(stdout_fd)), output, 32768, 0);
    if (!len || len >= 32768) throw std::runtime_error("output path lookup failed");
    auto script = std::filesystem::path(output).parent_path()/L"command.ps1";
    {
        std::ofstream file(script, std::ios::binary);
        file << "\xef\xbb\xbf[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\r\n"
                "$OutputEncoding = [Console]::OutputEncoding\r\n$ErrorActionPreference = 'Stop'\r\n" << command;
        if (!file) throw std::runtime_error("PowerShell script write failed");
    }
    Handle out_read, out_write, err_read, err_write, input, job, process, thread;
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    check(CreatePipe(&out_read.h, &out_write.h, &sa, 0), "stdout pipe");
    check(CreatePipe(&err_read.h, &err_write.h, &sa, 0), "stderr pipe");
    check(SetHandleInformation(out_read.h, HANDLE_FLAG_INHERIT, 0), "stdout ownership");
    check(SetHandleInformation(err_read.h, HANDLE_FLAG_INHERIT, 0), "stderr ownership");
    input.h = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ|FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);
    check(input.h != INVALID_HANDLE_VALUE, "stdin");
    job.h = CreateJobObjectW(nullptr, nullptr); check(job.h != nullptr, "job creation");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    check(SetInformationJobObject(job.h, JobObjectExtendedLimitInformation, &limits, sizeof(limits)), "job limits");
    STARTUPINFOEXW si{}; si.StartupInfo.cb = sizeof(si);
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = input.h; si.StartupInfo.hStdOutput = out_write.h; si.StartupInfo.hStdError = err_write.h;
    SIZE_T size = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
    std::vector<unsigned char> attributes(size);
    si.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
    check(InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &size), "process attributes");
    struct Cleanup { LPPROC_THREAD_ATTRIBUTE_LIST p; ~Cleanup() { DeleteProcThreadAttributeList(p); } } cleanup{si.lpAttributeList};
    HANDLE inherited[] = {input.h, out_write.h, err_write.h};
    check(UpdateProcThreadAttribute(si.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited, sizeof(inherited), nullptr, nullptr), "handle isolation");
    auto executable = wide(shell), directory = wide(cwd);
    auto line = L"\""+executable+L"\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \""+script.wstring()+L"\"";
    PROCESS_INFORMATION pi{};
    check(CreateProcessW(executable.c_str(), &line[0], nullptr, nullptr, TRUE,
          CREATE_SUSPENDED|CREATE_NO_WINDOW|EXTENDED_STARTUPINFO_PRESENT, nullptr,
          directory.empty() ? nullptr : directory.c_str(), &si.StartupInfo, &pi), "PowerShell spawn");
    process.h = pi.hProcess; thread.h = pi.hThread;
    int out = -1, err = -1;
    try {
        check(AssignProcessToJobObject(job.h, process.h), "job assignment");
        out = _open_osfhandle(reinterpret_cast<intptr_t>(out_read.h), _O_RDONLY|_O_BINARY|_O_NOINHERIT);
        if (out < 0) throw std::runtime_error("stdout descriptor failed");
        out_read.take();
        err = _open_osfhandle(reinterpret_cast<intptr_t>(err_read.h), _O_RDONLY|_O_BINARY|_O_NOINHERIT);
        if (err < 0) throw std::runtime_error("stderr descriptor failed");
        err_read.take();
        check(ResumeThread(thread.h) != DWORD(-1), "process resume");
        std::lock_guard<std::mutex> lock(guard);
        processes.emplace(int(pi.dwProcessId), Process{process.h, job.h, script});
        process.take(); job.take();
        return {int(pi.dwProcessId), out, err};
    } catch (...) {
        TerminateProcess(process.h, 1); WaitForSingleObject(process.h, INFINITE);
        if (out >= 0) _close(out); if (err >= 0) _close(err);
        throw;
    }
}
int observe_exit(pid_t pid) {
    HANDLE process;
    { std::lock_guard<std::mutex> lock(guard); process = processes.at(pid).process; }
    return WaitForSingleObject(process, INFINITE) == WAIT_OBJECT_0 ? 0 : int(GetLastError());
}
int reap_child(pid_t pid) {
    std::lock_guard<std::mutex> lock(guard);
    auto process = processes.at(pid);
    DWORD code = 1; check(GetExitCodeProcess(process.process, &code), "process status");
    CloseHandle(process.job); CloseHandle(process.process); processes.erase(pid);
    std::error_code error; std::filesystem::remove(process.script, error);
    return int(code);
}
void signal_child(pid_t pid, int, int) {
    std::lock_guard<std::mutex> lock(guard);
    auto found = processes.find(pid);
    if (found != processes.end()) TerminateJobObject(found->second.job, 1);
}
int read_child_pipe(int fd, char* bytes, std::size_t size) {
    DWORD available = 0;
    if (!PeekNamedPipe(reinterpret_cast<HANDLE>(_get_osfhandle(fd)), nullptr, 0, nullptr, &available, nullptr))
        return GetLastError() == ERROR_BROKEN_PIPE ? 0 : -1;
    if (!available) return -2;
    return _read(fd, bytes, static_cast<unsigned>(std::min<std::size_t>(available, size)));
}
}
