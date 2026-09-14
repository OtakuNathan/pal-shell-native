// Shell wire projection. Python payloads remain opaque bytes; framing, typed
// dispatch and codec errors belong to Dynabridge, I/O completion to FF/libuv.
#define DYNABRIDGE_IMPORT_DEF "rpc.def"
#define DYNABRIDGE_EXPORT_DEF "rpc.def"
#include "dynabridge/bridge.h"
#include "dynabridge/backends/rpc.h"
#include "dynabridge/backends/python_api.h"
#include <flow/flow.h>
#include <extension/external_async_awaitable.h>
#include <uv.h>
#ifndef _WIN32
#include <sys/socket.h>
#include <fcntl.h>
#include <unistd.h>
#endif
#include <cerrno>
#include <algorithm>
#include <utility>
#include <memory>
#include <stdexcept>

namespace {
namespace ff = flux_foundry;
using Bytes = dynabridge::rpc::bytes;
constexpr std::size_t max_frame = 1024 * 1024;
constexpr auto symbol = "shell_request";
#ifndef _WIN32
struct Input { int fd; Bytes request; int timeout; uv_loop_t* loop; };
struct Io {
    uv_poll_t poll{};
    uv_timer_t timer{};
    int fd = -1, closing = 0;
    bool finished = false, poll_ready = false, timer_ready = false;
    Bytes write, read;
    std::size_t sent = 0, expected = 4;
    std::exception_ptr error;
    ff::extension::external_async_callback_fp_t callback = nullptr;
    void* user = nullptr;
    ~Io() { if (fd >= 0) ::close(fd); }
    static void closed(uv_handle_t* h) {
        auto* s = static_cast<Io*>(h->data);
        if (--s->closing == 0) s->callback(s->user);
    }
    void finish(const char* message = nullptr) noexcept {
        if (finished) return;
        finished = true;
        if (message) {
            try { throw std::runtime_error(message); } catch (...) { error = std::current_exception(); }
        }
        closing = static_cast<int>(poll_ready) + static_cast<int>(timer_ready);
        if (poll_ready) { uv_poll_stop(&poll); uv_close(reinterpret_cast<uv_handle_t*>(&poll), closed); }
        if (timer_ready) { uv_timer_stop(&timer); uv_close(reinterpret_cast<uv_handle_t*>(&timer), closed); }
        if (!closing) callback(user);
    }
    void progress(int status, int events) noexcept {
        if (status < 0) { finish(uv_strerror(status)); return; }
        if ((events & UV_WRITABLE) && sent < write.size()) {
#ifdef MSG_NOSIGNAL
            const auto n = ::send(fd, write.data() + sent, write.size() - sent, MSG_NOSIGNAL);
#else
            const auto n = ::send(fd, write.data() + sent, write.size() - sent, 0);
#endif
            if (n > 0) sent += n;
            else if (n < 0 && errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) { finish("RPC send failed"); return; }
        }
        if (events & UV_READABLE) {
            unsigned char buffer[65536];
            const auto n = ::recv(fd, buffer, std::min(sizeof(buffer), expected - read.size()), 0);
            if (n == 0) { finish("RPC connection closed"); return; }
            if (n < 0 && errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) { finish("RPC receive failed"); return; }
            if (n > 0) {
                try { read.insert(read.end(), buffer, buffer + n); }
                catch (...) { finish("RPC allocation failed"); return; }
                if (expected == 4 && read.size() == 4) {
                    const std::size_t size = (std::size_t(read[0]) << 24) | (std::size_t(read[1]) << 16)
                        | (std::size_t(read[2]) << 8) | read[3];
                    if (!size || size > max_frame) { finish("RPC frame limit exceeded"); return; }
                    expected += size;
                }
                if (read.size() == expected) { finish(); return; }
            }
        }
        const int flags = UV_READABLE | (sent < write.size() ? UV_WRITABLE : 0);
        const int code = uv_poll_start(&poll, flags, [](uv_poll_t* p, int status, int events) {
            static_cast<Io*>(p->data)->progress(status, events);
        });
        if (code) finish(uv_strerror(code));
    }
};
struct SocketExchange {
    struct context_t { Io* state = nullptr; uv_loop_t* loop; int timeout; };
    using result_t = Io*;
    static int init_ctx(context_t* c, Input* in) noexcept {
        c->loop = in->loop; c->timeout = in->timeout;
        try {
            auto owned = std::make_unique<Io>();
            auto& s = *owned;
            s.fd = dup(in->fd);
            if (s.fd < 0 || fcntl(s.fd, F_SETFL, fcntl(s.fd, F_GETFL) | O_NONBLOCK) < 0) return -1;
#ifdef SO_NOSIGPIPE
            int yes = 1; setsockopt(s.fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof(yes));
#endif
            auto size = in->request.size();
            if (!size || size > max_frame) return -1;
            s.write = {static_cast<unsigned char>(size >> 24), static_cast<unsigned char>(size >> 16),
                       static_cast<unsigned char>(size >> 8), static_cast<unsigned char>(size)};
            s.write.insert(s.write.end(), in->request.begin(), in->request.end());
            c->state = owned.release();
            return 0;
        } catch (...) { return -1; }
    }
    static void destroy_ctx(context_t* c) noexcept { delete c->state; }
    static void free_result(result_t s) noexcept { delete s; }
    static result_t collect(context_t* c) noexcept { return std::exchange(c->state, nullptr); }
    static int submit(context_t* c, ff::extension::external_async_callback_fp_t callback, void* user) noexcept {
        auto* s = c->state; s->callback = callback; s->user = user;
        int code = uv_poll_init(c->loop, &s->poll, s->fd);
        if (code) { s->finish(uv_strerror(code)); return 0; }
        s->poll_ready = true; s->poll.data = s;
        code = uv_timer_init(c->loop, &s->timer);
        if (code) { s->finish(uv_strerror(code)); return 0; }
        s->timer_ready = true; s->timer.data = s;
        code = uv_timer_start(&s->timer, [](uv_timer_t* t) { static_cast<Io*>(t->data)->finish("RPC timed out"); }, c->timeout, 0);
        if (code) s->finish(uv_strerror(code)); else s->progress(0, UV_WRITABLE);
        return 0;
    }
};
using Result = ff::result_t<Bytes, std::exception_ptr>;
struct Receiver { using value_type = Result; std::unique_ptr<Result>* result; void emplace(Result&& r) noexcept { result->reset(new Result(std::move(r))); } };
#endif
PyObject* failure(const std::exception& e) { PyErr_SetString(PyExc_RuntimeError, e.what()); return nullptr; }
PyObject* pack(PyObject*, PyObject* arg) {
    if (!PyBytes_Check(arg)) { PyErr_SetString(PyExc_TypeError, "bytes required"); return nullptr; }
    try {
        auto b = dynabridge::rpc::detail::encode_request_values(dynabridge::rpc::detail::symbol_id(symbol),
            dynabridge::rpc::value::string(std::string(PyBytes_AS_STRING(arg), PyBytes_GET_SIZE(arg))));
        return PyBytes_FromStringAndSize(reinterpret_cast<const char*>(b.data()), b.size());
    } catch (const std::exception& e) { return failure(e); }
}
PyObject* unpack(PyObject*, PyObject* arg) {
    if (!PyBytes_Check(arg)) { PyErr_SetString(PyExc_TypeError, "bytes required"); return nullptr; }
    try {
        const auto* begin = reinterpret_cast<const unsigned char*>(PyBytes_AS_STRING(arg));
        auto request = dynabridge::rpc::detail::decode_request(Bytes(begin, begin + PyBytes_GET_SIZE(arg)));
        if (request.method != dynabridge::rpc::detail::symbol_id(symbol) || request.args.size() != 1
            || request.args[0].kind() != dynabridge::rpc::value_kind::string) throw std::runtime_error("invalid shell RPC request");
        const auto& value = request.args[0];
        return PyBytes_FromStringAndSize(value.string_data(), value.string_size());
    } catch (const std::exception& e) { return failure(e); }
}
PyObject* respond(PyObject*, PyObject* args) {
    const char *frame, *payload; Py_ssize_t count, size;
    if (!PyArg_ParseTuple(args, "y#y#", &frame, &count, &payload, &size)) return nullptr;
    try {
        dynabridge::rpc::router router;
        dynabridge::rpc_backend::export_context_t ctx;
        dynabridge::export_shell_request<std::string(std::string)>(ctx, router, [payload, size](std::string) { return std::string(payload, size); });
        auto* begin = reinterpret_cast<const unsigned char*>(frame);
        auto b = router.dispatch(Bytes(begin, begin + count));
        return PyBytes_FromStringAndSize(reinterpret_cast<const char*>(b.data()), b.size());
    } catch (const std::exception& e) { return failure(e); }
}
#ifndef _WIN32
PyObject* exchange_frame(PyObject*, PyObject* args) {
    int fd, timeout; const char* raw; Py_ssize_t count;
    if (!PyArg_ParseTuple(args, "iy#i", &fd, &raw, &count, &timeout)) return nullptr;
    if (timeout < 1 || count < 1 || count > static_cast<Py_ssize_t>(max_frame)) {
        PyErr_SetString(PyExc_ValueError, "invalid RPC budget"); return nullptr;
    }
    Bytes frame, output;
    try { frame.assign(raw, raw + count); }
    catch (const std::exception& e) { return failure(e); }
    std::exception_ptr error;
    auto* saved = PyEval_SaveThread();
    try {
        uv_loop_t loop{};
        if (uv_loop_init(&loop)) throw std::runtime_error("RPC loop initialization failed");
        std::unique_ptr<Result> result;
        auto bp = ff::make_blueprint<Input>() | ff::await_external_async<SocketExchange>()
            | ff::then([](typename ff::extension::external_async_awaitable<SocketExchange>::async_result_type&& in) -> Result {
                if (in.has_error()) return Result(ff::error_tag, in.error());
                auto s = std::move(in).value();
                if (s->error) return Result(ff::error_tag, s->error);
                Bytes response(s->read.begin() + 4, s->read.end());
                auto value = dynabridge::rpc::detail::decode_response(response);
                if (value.kind() != dynabridge::rpc::value_kind::string) throw std::runtime_error("invalid shell RPC response");
                return Result(ff::value_tag, Bytes(value.string_data(), value.string_data() + value.string_size()));
            }) | ff::end();
        auto blueprint = ff::make_lite_ptr<decltype(bp)>(std::move(bp));
        ff::flow_runner<decltype(bp), Receiver> runner(blueprint, ff::make_lite_ptr<ff::flow_controller>(), Receiver{&result});
        runner(Input{fd, std::move(frame), timeout, &loop});
        uv_run(&loop, UV_RUN_DEFAULT);
        const int close_error = uv_loop_close(&loop);
        if (close_error || !result) throw std::runtime_error("RPC loop did not drain");
        if (result->has_error()) std::rethrow_exception(result->error());
        output = std::move(*result).value();
    } catch (...) { error = std::current_exception(); }
    PyEval_RestoreThread(saved);
    try { if (error) std::rethrow_exception(error); }
    catch (const std::exception& e) { return failure(e); }
    return PyBytes_FromStringAndSize(reinterpret_cast<const char*>(output.data()), output.size());
}
#endif
PyMethodDef methods[] = {
    {"pack", pack, METH_O, nullptr}, {"unpack", unpack, METH_O, nullptr},
    { "respond", respond, METH_VARARGS, nullptr},
#ifndef _WIN32
    {"exchange", exchange_frame, METH_VARARGS, nullptr},
#endif
    {nullptr, nullptr, 0, nullptr}
};
PyModuleDef definition = {PyModuleDef_HEAD_INIT, "_pal_shell_rpc", "Dynabridge shell RPC and FF/libuv exchange", -1, methods};
}
PyMODINIT_FUNC PyInit__pal_shell_rpc() { return PyModule_Create(&definition); }
