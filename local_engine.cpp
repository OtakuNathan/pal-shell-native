#include "runtime.h"
#include "local_wire_generated.h"

#include <cerrno>
#include <condition_variable>
#include <csignal>
#include <deque>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <mutex>
#include <set>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

namespace {
using portal::pal_shell::Engine;
using portal::pal_shell::Event;
using EngineID = portal::pal_shell::id_t;

constexpr std::size_t frame_limit = 1024 * 1024;
constexpr std::size_t queue_limit = 8 * frame_limit;

bool transfer(int fd, std::uint8_t* bytes, std::size_t size, bool write) {
    while (size) {
        auto count = write ? ::send(fd, bytes, size, 0) : ::recv(fd, bytes, size, 0);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return false;
        bytes += count;
        size -= static_cast<std::size_t>(count);
    }
    return true;
}

class Replies {
public:
    explicit Replies(int fd) : fd_(fd), writer_([this] { write(); }) {}
    ~Replies() {
        stop();
        writer_.join();
    }
    void stop() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopped_ = true;
        }
        ::shutdown(fd_, SHUT_RDWR);
        ready_.notify_one();
    }
    void send(std::uint64_t id, w::bytes body) {
        if (body.empty() || body.size() + 8 > frame_limit) { stop(); return; }
        const auto length = body.size() + 8;
        w::bytes frame(12);
        for (unsigned i = 0; i < 4; ++i) frame[i] = static_cast<std::uint8_t>(length >> ((3 - i) * 8));
        for (unsigned i = 0; i < 8; ++i) frame[4 + i] = static_cast<std::uint8_t>(id >> ((7 - i) * 8));
        frame.insert(frame.end(), body.begin(), body.end());
        {
            std::unique_lock<std::mutex> lock(mutex_);
            if (stopped_) return;
            if (queued_ + frame.size() > queue_limit) {
                lock.unlock();
                stop();
                return;
            }
            queued_ += frame.size();
            queue_.push_back(std::move(frame));
        }
        ready_.notify_one();
    }
private:
    void write() {
        for (;;) {
            w::bytes frame;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [&] { return stopped_ || !queue_.empty(); });
                if (stopped_) return;
                frame = std::move(queue_.front());
                queue_.pop_front();
                queued_ -= frame.size();
            }
            if (!transfer(fd_, frame.data(), frame.size(), true)) { stop(); return; }
        }
    }
    int fd_;
    std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<w::bytes> queue_;
    std::size_t queued_ = 0;
    bool stopped_ = false;
    std::thread writer_;
};

LocalEvent project(const Event& event) {
    return {event.request, event.session, event.output, event.event_kind, event.event_sequence,
            event.watch_generation, event.elapsed_ms, event.remaining_ms, event.wake_remaining_ms,
            event.watching, event.has_deadline, event.has_wake, event.status, event.error.substr(0, 8192),
            event.stdout_path, event.stderr_path, event.stdout_total, event.stderr_total,
            event.returncode, event.signal, event.has_returncode, event.tty, event.truncated};
}

std::string shell_quote(const std::string& input) {
    std::string output = "'";
    for (char c : input) {
        if (c == '\'') output += "'\\''";
        else output += c;
    }
    return output + "'";
}

class Controller {
public:
    Controller(Engine& engine, bool role, std::string epoch)
        : engine_(engine), role_(role), epoch_(std::move(epoch)) {}
    std::string submit(LocalCommand c) {
        const auto& op = c.operation;
        const auto q = c.request;
        const auto s = c.session;
        if (!q || q <= last_request_) return "invalid or repeated native request id";
        last_request_ = q;
        try {
            if (op == "run") {
                if (c.authorized) {
#if defined(__linux__)
                    if (role_ || c.tty || c.epoch != epoch_ || c.approval.empty() ||
                        approvals_.size() >= 65536 || !approvals_.insert(c.approval).second)
                        return "invalid or retired local approval";
                    auto command = "exec /usr/bin/sudo -n -- /bin/sh -c " + shell_quote(c.text);
                    if (c.has_stdin) engine_.run_authorized_with_stdin(q, "/bin/sh", command, c.cwd, false, c.wait, c.timeout, 0, c.stdin);
                    else engine_.run_authorized(q, "/bin/sh", command, c.cwd, false, c.wait, c.timeout, 0);
#else
                    return "local sudo requires Linux";
#endif
                } else if (c.has_stdin)
                    engine_.run_with_stdin(q, c.shell, c.text, c.cwd, c.tty, c.wait, c.timeout, 0, c.stdin);
                else engine_.run(q, c.shell, c.text, c.cwd, c.tty, c.wait, c.timeout, 0);
            } else if (op == "read") engine_.read(q, s, c.wait);
            else if (op == "watch") engine_.watch(q, s, c.wait, c.timeout);
            else if (op == "extend") engine_.extend(q, s, c.timeout);
            else if (op == "unwatch") engine_.unwatch(q, s);
            else if (op == "write") engine_.write(q, s, c.text);
            else if (op == "resize") engine_.resize(q, s, c.rows, c.columns);
            else if (op == "terminate") engine_.terminate(q, s);
            else if (op == "release") engine_.release(q, s);
            else if (op == "release_output") engine_.release_output(q, s);
            else if (op == "acknowledge") engine_.acknowledge(q, s, c.terminal);
            else if (op == "cancel_request") engine_.cancel_request(q, s);
            else if (op == "acquire_write") engine_.acquire_write(q);
            else if (op == "release_write") engine_.release_write(q, s);
            else return "unknown local engine operation";
            return {};
        } catch (const std::exception& error) {
            return std::string(error.what()).substr(0, 8192);
        }
    }
private:
    Engine& engine_;
    bool role_;
    std::string epoch_;
    EngineID last_request_ = 0;
    std::set<std::string> approvals_;
};
}

int main(int argc, char** argv) {
    try {
        if (argc != 8) throw std::invalid_argument("local engine is started with a private inherited socket");
        int fd = std::stoi(argv[1]);
        const std::string epoch = argv[2];
        unsigned capacity = static_cast<unsigned>(std::stoul(argv[3]));
        EngineID first = std::stoull(argv[4]), end = std::stoull(argv[5]);
        const std::string output = argv[6];
        const std::string scope = argv[7];
        if (fd < 3 || epoch.size() != 32 || first == 0 || end <= first || end > (EngineID{1} << 48) ||
            output.empty() || (scope != "role" && scope != "resident"))
            throw std::invalid_argument("invalid local engine launch context");
        if (fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) throw std::runtime_error("cannot protect control socket");
        std::signal(SIGPIPE, SIG_IGN);
        Replies replies(fd);
        Engine engine(capacity, first, end, output);
        Controller controller(engine, scope == "role", epoch);
        engine.bind([&](const Event& event) {
            try {
                w::bytes body;
                if (w::encode_into<w::record<LocalEvent>>(project(event), body) != w::errc::ok)
                    replies.stop();
                else replies.send(0, std::move(body));
            } catch (...) { replies.stop(); }
        });
        rpc::router router;
        router.define<LocalRpcHello>([&] { return LocalHello{static_cast<EngineID>(::getpid()), epoch, PAL_SHELL_VERSION}; });
        router.define<LocalRpcSubmit>([&](LocalCommand command) { return controller.submit(std::move(command)); });
        std::uint64_t last = 0;
        for (;;) {
            std::uint8_t header[4];
            if (!transfer(fd, header, sizeof(header), false)) break;
            std::size_t length = 0;
            for (auto byte : header) length = (length << 8) | byte;
            if (length < 8 || length > frame_limit) break;
            w::bytes frame(length);
            if (!transfer(fd, frame.data(), length, false)) break;
            std::uint64_t id = 0;
            for (unsigned i = 0; i < 8; ++i) id = (id << 8) | frame[i];
            if (id <= last) break;
            last = id;
            w::bytes response;
            router.dispatch_into(w::bytes(frame.begin() + 8, frame.end()), response);
            replies.send(id, std::move(response));
        }
        replies.stop();
        engine.close();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "pal-shell-local-worker: " << error.what() << '\n';
        return 1;
    }
}
