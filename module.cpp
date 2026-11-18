#include "runtime.h"
#include "portal/extensions/flux_foundry/python_interpreter_executor.h"
#include <atomic>

namespace portal::pal_shell {
class Runtime : public Engine {
public:
    explicit Runtime(unsigned capacity) : Engine(capacity) {}
    ~Runtime() { close(); }

    void bind(std::function<void(const Event&)> callback) {
        Engine::bind([this](const Event& event) {
            auto snapshot = std::make_shared<Event>(event);
            gil_.dispatch(flux_foundry::task_wrapper_sbo([this, snapshot]() noexcept {
                delivering_ = this;
                try { callback_(*snapshot); }
                catch (...) { ++errors_; PyErr_Clear(); }
                delivering_ = nullptr;
            }));
        });
        callback_ = std::move(callback);
    }

    void close() {
        if (delivering_ == this)
            throw std::runtime_error("close must be called on the host thread, outside the native callback");
        if (closing_.exchange(true)) return;
        // As in the legacy binding, process reaping must allow other Python threads to run.
        PyThreadState* released = PyGILState_Check() ? PyEval_SaveThread() : nullptr;
        try {
            Engine::close();
        } catch (...) {
            if (released) PyEval_RestoreThread(released);
            throw;
        }
        if (released) PyEval_RestoreThread(released);
        gil_.shutdown();
        callback_ = {};
    }

    int callback_errors() const { return errors_.load() + Engine::callback_errors(); }

private:
    inline static thread_local Runtime* delivering_ = nullptr;
    std::atomic<int> errors_{0};
    std::atomic<bool> closing_{false};
    std::function<void(const Event&)> callback_;
    flux_foundry_extensions::python_interpreter_executor gil_;
};
}
#define PORTAL_IMPORT_DEF "import.def"
#define PORTAL_EXPORT_DEF "export.def"
#include "portal/portal.h"
#include "portal/backends/python.h"

namespace portal {
template<> struct py_backend::converter<pal_shell::id_t> {
    static PyObject* to(context_t&, pal_shell::id_t value) {
        return PyLong_FromUnsignedLongLong(value);
    }
    static optional<pal_shell::id_t> from(context_t&, PyObject* value) {
        if (!PyLong_CheckExact(value)) return {};
        auto converted = PyLong_AsUnsignedLongLong(value);
        if (PyErr_Occurred()) { PyErr_Clear(); return {}; }
        return optional<pal_shell::id_t>(converted);
    }
};
template<> struct py_backend::converter<bool> {
    static PyObject* to(context_t&, bool value) { return PyBool_FromLong(value); }
    static optional<bool> from(context_t&, PyObject* value) {
        if (!PyBool_Check(value)) return {};
        return optional<bool>(value == Py_True);
    }
};
template<> struct py_backend::converter<pal_shell::Event> {
    static PyObject* to(context_t&, const pal_shell::Event& event) {
        py_backend::object_ref result(PyDict_New(), py_backend::ref_policy::owned);
        if (!result) throw std::runtime_error("event allocation failed");
        auto put = [&result](const char* key, PyObject* raw) {
            py_backend::object_ref value(raw, py_backend::ref_policy::owned);
            if (!value || PyDict_SetItemString(result.get(), key, value.get()))
                throw std::runtime_error("event conversion failed");
        };
        put("request_id", PyLong_FromUnsignedLongLong(event.request));
        put("session_id", PyLong_FromUnsignedLongLong(event.session));
        put("output_id", PyLong_FromUnsignedLongLong(event.output));
        put("event_kind", PyUnicode_FromString(event.event_kind.c_str()));
        put("event_sequence", PyLong_FromUnsignedLongLong(event.event_sequence));
        put("watch_generation", PyLong_FromUnsignedLongLong(event.watch_generation));
        put("watching", PyBool_FromLong(event.watching));
        put("elapsed_ms", PyLong_FromUnsignedLongLong(event.elapsed_ms));
        put("remaining_ms", event.has_deadline ? PyLong_FromUnsignedLongLong(event.remaining_ms) : Py_NewRef(Py_None));
        put("wake_remaining_ms", event.has_wake ? PyLong_FromUnsignedLongLong(event.wake_remaining_ms) : Py_NewRef(Py_None));
        put("status", PyUnicode_FromString(event.status.c_str()));
        put("error", PyUnicode_DecodeUTF8(event.error.data(), event.error.size(), "replace"));
        put("stdout_path", PyUnicode_DecodeFSDefault(event.stdout_path.c_str()));
        put("stderr_path", PyUnicode_DecodeFSDefault(event.stderr_path.c_str()));
        if (event.inline_output) {
            put("stdout_bytes", PyBytes_FromStringAndSize(event.stdout_bytes.data(), event.stdout_bytes.size()));
            put("stderr_bytes", PyBytes_FromStringAndSize(event.stderr_bytes.data(), event.stderr_bytes.size()));
        }
        put("stdout_total", PyLong_FromUnsignedLongLong(event.stdout_total));
        put("stderr_total", PyLong_FromUnsignedLongLong(event.stderr_total));
        put("truncated", PyBool_FromLong(event.truncated));
        put("tty", PyBool_FromLong(event.tty));
        put("returncode", event.has_returncode ? PyLong_FromLong(event.returncode) : Py_NewRef(Py_None));
        put("signal", PyLong_FromLong(event.signal));
        return result.release();
    }
};
}

static PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT, "_pal_shell_runtime", "Pal native shell execution backend.", -1,
    nullptr, nullptr, nullptr, nullptr, nullptr
};

PyMODINIT_FUNC PyInit__pal_shell_runtime() {
    PyObject* result = PyModule_Create(&module_definition);
    if (!result) return nullptr;
    if (PyModule_AddIntConstant(result, "API_VERSION", 2) < 0) {
        Py_DECREF(result);
        return nullptr;
    }
    if (PyModule_AddIntConstant(result, "PRIVILEGE_GATE_VERSION", 1) < 0) {
        Py_DECREF(result);
        return nullptr;
    }
    if (PyModule_AddIntConstant(result, "STDIN_VERSION", 1) < 0) {
        Py_DECREF(result);
        return nullptr;
    }
    try {
        // Generated callables borrow their export context. Keep registration
        // metadata for the process lifetime, like a static extension type; do
        // not DECREF its types from a C++ destructor after Py_Finalize.
        // Runtime instances and their worker resources remain explicitly owned.
        static auto* registration = new portal::py_backend::export_context_t;
        auto& ctx = *registration;
        portal::py_backend::module_t module(result, portal::py_backend::ref_policy::borrowed);
        portal::exports::Runtime::register_all(ctx, module);
        using Signature = void(
            portal::object_param<portal::export_classes::Runtime, portal::export_t>,
            portal::callable_param<portal::import_symbols::notify, portal::import_t>);
        portal::export_connect<Signature>(ctx, module, [](portal::pal_shell::Runtime& runtime, auto& callback) {
            using Context = std::decay_t<decltype(callback)>;
            auto context = std::make_shared<Context>(std::move(callback));
            runtime.bind([context](const portal::pal_shell::Event& event) {
                portal::call_notify(*context, event);
            });
        });
        return result;
    } catch (const std::exception& error) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}
