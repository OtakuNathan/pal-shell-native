// Connection I/O is confined to one caller-owned libuv executor.
// Python callbacks only enqueue immutable events on their asyncio owner.
#pragma once
#include "dynabridge/extensions/flux_foundry/uv_executor.h"
#include <condition_variable>
#include <deque>
#include <future>
#include <map>
#include <mutex>
#include <thread>
#include <atomic>
#include <functional>
#ifdef _WIN32
#include <winsock2.h>
#else
#include <sys/socket.h>
#include <fcntl.h>
#endif
namespace shell_transport {
using Buffer = std::vector<unsigned char>;
constexpr size_t frame_limit = 1024*1024, queue_limit = 8*1024*1024;
struct Channel;
struct Pending {
    uint64_t id=0, deadline=0;
    Buffer request, response;
    std::string error;
    Channel* channel=nullptr;
    flux_foundry::extension::external_async_callback_fp_t callback=nullptr;
    void* user=nullptr;
    std::shared_ptr<void> keepalive;
};
void start_request(Channel*,uint64_t,Buffer,int);

struct Loop {
    uv_loop_t loop{};
    std::thread thread;
    dynabridge::flux_foundry_extensions::uv_executor* executor = nullptr;
    std::mutex admission;
    bool closing = false;
    std::atomic<size_t> queued{0};
    std::vector<std::shared_ptr<Channel>> channels;
    Loop() {
        std::promise<void> ready; auto f=ready.get_future();
        thread=std::thread([this,&ready] {
            if (uv_loop_init(&loop)) { ready.set_exception(std::make_exception_ptr(std::runtime_error("RPC loop initialization failed"))); return; }
            try {
                dynabridge::flux_foundry_extensions::uv_executor ex(&loop); executor=&ex;
                ready.set_value(); uv_run(&loop,UV_RUN_DEFAULT); executor=nullptr;
            } catch (...) { ready.set_exception(std::current_exception()); }
            if (uv_loop_close(&loop)) std::terminate();
        });
        try { f.get(); } catch (...) { thread.join(); throw; }
    }
    void post(std::function<void()> fn) {
        std::lock_guard<std::mutex> guard(admission);
        if(closing) throw std::runtime_error("RPC executor closed");
        if(queued.load()>=256) throw std::runtime_error("RPC ingress capacity exceeded");
        ++queued;
        executor->dispatch(flux_foundry::task_wrapper_sbo([this,fn=std::move(fn)]() noexcept {
            --queued; fn();
        }));
    }
    void close();
    ~Loop() { close(); }
};
struct Channel : std::enable_shared_from_this<Channel> {
    Loop* owner;
    uv_os_sock_t fd;
    PyObject* callback;
    uv_poll_t poll{};
    uv_timer_t timer{}, requests_timer{};
    bool listening=false;
    bool closed=false, poll_ready=false, timer_ready=false, requests_ready=false;
    int close_count=0;
    std::string close_reason;
    std::map<uint64_t,std::shared_ptr<Pending>> requests;
    std::vector<std::shared_ptr<void>> retired;
    uint64_t largest_id=0;
    Buffer input;
    size_t expected=4, queued_bytes=0;
    struct Write { Buffer bytes; size_t offset=0; };
    std::deque<Write> writes;
    Channel(Loop* o,uv_os_sock_t f,PyObject* c):owner(o),fd(f),callback(c) { Py_INCREF(callback); }
    ~Channel() { auto g=PyGILState_Ensure(); Py_DECREF(callback); PyGILState_Release(g); }
    void event(const char* kind, const Buffer& bytes={}, const char* error="") {
        auto g=PyGILState_Ensure();
        auto* value=PyObject_CallFunction(callback,"sy#s",kind,bytes.data(),static_cast<Py_ssize_t>(bytes.size()),error);
        if(!value) { PyErr_WriteUnraisable(callback); } else Py_DECREF(value);
        PyGILState_Release(g);
    }
    void finish(const char* reason) {
        if(closed) return;
        closed=true; writes.clear(); input.clear(); queued_bytes=0;
        close_reason=reason;
        auto pending=std::move(requests); requests.clear();
        for(auto& item:pending) complete(item.second,{},reason);
        close_count=int(poll_ready)+int(timer_ready)+int(requests_ready);
        auto done=[](uv_handle_t* h) {
            auto* raw=static_cast<Channel*>(h->data);
            if(--raw->close_count==0) {
                auto keep=raw->shared_from_this();
                raw->retired.clear();
                raw->event("closed",{},raw->close_reason.c_str());
                auto& channels=raw->owner->channels;
                channels.erase(std::remove(channels.begin(),channels.end(),keep),channels.end());
            }
        };
        if(poll_ready) { uv_poll_stop(&poll); uv_close(reinterpret_cast<uv_handle_t*>(&poll),done); }
        if(timer_ready) { uv_timer_stop(&timer); uv_close(reinterpret_cast<uv_handle_t*>(&timer),done); }
        if(requests_ready) { uv_timer_stop(&requests_timer); uv_close(reinterpret_cast<uv_handle_t*>(&requests_timer),done); }
        if(!close_count) event("closed",{},reason);
    }

    void start() {
        int code=uv_poll_init_socket(&owner->loop,&poll,fd);
        if(code) { finish(uv_strerror(code)); return; }
        poll_ready=true; poll.data=this;
        code=uv_timer_init(&owner->loop,&timer);
        if(code) { finish(uv_strerror(code)); return; }
        timer_ready=true; timer.data=this;
        code=uv_timer_init(&owner->loop,&requests_timer);
        if(code) { finish(uv_strerror(code)); return; }
        requests_ready=true; requests_timer.data=this;
        arm();
    }
    void arm() {
        if(closed) return;
        int code=uv_poll_start(&poll,UV_READABLE|(writes.empty()?0:UV_WRITABLE),[](uv_poll_t* p,int s,int events){
            static_cast<Channel*>(p->data)->progress(s,events);
        });
        if(code) finish(uv_strerror(code));
    }
    static bool again() {
#ifdef _WIN32
        auto e=WSAGetLastError(); return e==WSAEWOULDBLOCK || e==WSAEINTR;
#else
        return errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR;
#endif
    }
    void send(Buffer bytes) {
        if(closed) return;
        if(bytes.empty() || bytes.size()>frame_limit || queued_bytes+bytes.size()+4>queue_limit) { finish("RPC send queue capacity exceeded"); return; }
        size_t n=bytes.size(); Buffer frame{(unsigned char)(n>>24),(unsigned char)(n>>16),(unsigned char)(n>>8),(unsigned char)n};
        frame.insert(frame.end(),bytes.begin(),bytes.end()); queued_bytes+=frame.size();
        writes.push_back({std::move(frame),0}); arm();
    }
    void tick() {
        retired.clear();
        uv_update_time(&owner->loop);
        auto now=uv_now(&owner->loop);
        std::vector<std::shared_ptr<Pending>> expired;
        for(auto it=requests.begin();it!=requests.end();) {
            if(it->second->deadline<=now) {expired.push_back(it->second);it=requests.erase(it);} else ++it;
        }
        for(auto& p:expired) complete(p,{},"RPC response timed out");
        if(requests.empty() && retired.empty()) uv_timer_stop(&requests_timer);
    }
    void complete(std::shared_ptr<Pending> p,Buffer response,const char* error) {
        p->response=std::move(response); p->error=error;
        retired.push_back(std::move(p->keepalive));
        p->callback(p->user);
    }
    void request(std::shared_ptr<Pending> p) {
        if(closed) {complete(p,{},"RPC channel closed before send");return;}
        if(requests.size()>=32 || requests.count(p->id) || queued_bytes+p->request.size()+12>queue_limit) {
            complete(p,{},"RPC capacity exceeded before send");
        } else {
            uv_update_time(&owner->loop); p->deadline+=uv_now(&owner->loop);
            requests.emplace(p->id,p); largest_id=std::max(largest_id,p->id);
            Buffer frame;
            if(p->id) for(int i=7;i>=0;--i) frame.push_back(static_cast<unsigned char>(p->id>>(i*8)));
            frame.insert(frame.end(),p->request.begin(),p->request.end()); send(std::move(frame));
        }
        if(!closed) uv_timer_start(&requests_timer,[](uv_timer_t* t){static_cast<Channel*>(t->data)->tick();},1,10);
    }
    void cancel(uint64_t id) {
        auto it=requests.find(id);
        if(it!=requests.end()) {auto p=it->second;requests.erase(it);complete(p,{},"RPC request cancelled");}
    }
    void progress(int status,int flags) {
        try {
            if(status<0) { finish(uv_strerror(status)); return; }
            if(listening) {
                for(int round=0;round<16;++round) {
                    auto client=::accept(fd,nullptr,nullptr);
                    if(client==static_cast<uv_os_sock_t>(-1)) { if(!again()) finish("RPC accept failed"); break; }
#ifdef _WIN32
                    SetHandleInformation(reinterpret_cast<HANDLE>(client),HANDLE_FLAG_INHERIT,0);
#else
                    fcntl(client,F_SETFD,FD_CLOEXEC);
#endif
                    Buffer value;
                    auto number=static_cast<uint64_t>(client);
                    for(int i=7;i>=0;--i) value.push_back(static_cast<unsigned char>(number>>(i*8)));
                    event("accepted",value);
                }
                return;
            }
            if(flags&UV_WRITABLE) {
                for(int round=0;round<16 && !writes.empty();++round) {
                    auto& w=writes.front();
#ifdef MSG_NOSIGNAL
                    constexpr int send_flags=MSG_NOSIGNAL;
#else
                    constexpr int send_flags=0;
#endif
                    auto n=::send(fd,reinterpret_cast<const char*>(w.bytes.data()+w.offset),static_cast<int>(w.bytes.size()-w.offset),send_flags);
                    if(n<0) { if(!again()) finish("RPC send failed"); break; }
                    if(!n) break;
                    w.offset+=n; queued_bytes-=n;
                    if(w.offset==w.bytes.size()) writes.pop_front();
                }
            }
            if(!closed && (flags&UV_READABLE)) {
                for(int round=0;round<16;++round) {
                    unsigned char b[65536];
                    auto n=::recv(fd,reinterpret_cast<char*>(b),static_cast<int>(std::min(sizeof(b),expected-input.size())),0);
                    if(n==0) { finish("RPC peer closed"); break; }
                    if(n<0) { if(!again()) finish("RPC receive failed"); break; }
                    if(input.empty()) uv_timer_start(&timer,[](uv_timer_t* t){static_cast<Channel*>(t->data)->finish("RPC incomplete frame timed out");},30000,0);
                    input.insert(input.end(),b,b+n);
                    if(expected==4 && input.size()==4) {
                        size_t size=(size_t(input[0])<<24)|(size_t(input[1])<<16)|(size_t(input[2])<<8)|input[3];
                        if(!size || size>frame_limit) { finish("RPC invalid frame length"); break; }
                        expected=size+4;
                    } else if(input.size()==expected) {
                        Buffer frame(input.begin()+4,input.end()); input.clear(); expected=4; uv_timer_stop(&timer);
                        uint64_t id=0;
                        const bool legacy=frame.size()>=4 && (std::equal(frame.begin(),frame.begin()+4,"DRPR") || std::equal(frame.begin(),frame.begin()+4,"DRPC"));
                        if(!legacy && frame.size()<9) { finish("RPC invalid request envelope"); break; }
                        if(!legacy) for(int i=0;i<8;++i) id=(id<<8)|frame[i];
                        auto found=requests.find(id);
                        if(found!=requests.end()) {
                            auto p=found->second; requests.erase(found);
                            complete(p, legacy ? std::move(frame) : Buffer(frame.begin()+8,frame.end()), "");
                        } else event("frame",frame);
                    }
                }
            }
            arm();
        } catch (...) { finish("RPC I/O failure"); }
    }
};
struct RpcAwaitable {
    struct context_t { std::shared_ptr<Pending>* pending=nullptr; };
    using result_t=std::shared_ptr<Pending>*;
    static int init_ctx(context_t* c,std::shared_ptr<Pending>* input) noexcept {try {c->pending=new std::shared_ptr<Pending>(*input);return 0;} catch(...) {return -1;}}
    static void destroy_ctx(context_t* c) noexcept {delete c->pending;}
    static void free_result(result_t p) noexcept {delete p;}
    static result_t collect(context_t* c) noexcept {return std::exchange(c->pending,nullptr);}
    static int submit(context_t* c,flux_foundry::extension::external_async_callback_fp_t cb,void* user) noexcept {
        auto p=*c->pending;p->callback=cb;p->user=user;
        try {p->channel->request(p);} catch(...) {p->channel->complete(p,{},"RPC request allocation failed");}
        return 0;
    }
};
struct RpcReceiver {
    using value_type=flux_foundry::result_t<std::shared_ptr<Pending>,std::exception_ptr>;
    Channel* channel;
    uint64_t id;
    void emplace(value_type&& result) noexcept {
        Buffer frame;
        for(int i=7;i>=0;--i) frame.push_back(static_cast<unsigned char>(id>>(i*8)));
        if(result.has_error()) {channel->event("response",frame,"RPC await failed");return;}
        auto p=std::move(result).value();
        frame.insert(frame.end(),p->response.begin(),p->response.end());
        channel->event("response",frame,p->error.c_str());
    }
};
inline void start_request(Channel* c,uint64_t id,Buffer frame,int timeout) {
    namespace ff=flux_foundry;
    auto bp=ff::make_blueprint<std::shared_ptr<Pending>>() | ff::await_external_async<RpcAwaitable>()
        | ff::then([](typename ff::extension::external_async_awaitable<RpcAwaitable>::async_result_type&& result) -> RpcReceiver::value_type {
            if(result.has_error()) return RpcReceiver::value_type(ff::error_tag,result.error());
            auto holder=std::move(result).value();
            return RpcReceiver::value_type(ff::value_tag,*holder);
        }) | ff::end();
    using BP=decltype(bp);using Runner=ff::flow_runner<BP,RpcReceiver>;
    auto p=std::make_shared<Pending>();p->channel=c;p->id=id;p->request=std::move(frame);p->deadline=timeout;
    auto runner=std::make_shared<Runner>(ff::make_lite_ptr<BP>(std::move(bp)),ff::make_lite_ptr<ff::flow_controller>(),RpcReceiver{c,id});
    p->keepalive=runner;
    (*runner)(p);
}
inline void Loop::close() {
    { std::lock_guard<std::mutex> guard(admission);
      if(!closing) { closing=true; executor->dispatch(flux_foundry::task_wrapper_sbo([this]() noexcept {
          for(auto& c:channels) c->finish("RPC executor closed");
          executor->close();
      })); }
    }
    if(thread.joinable()) thread.join();
    channels.clear();
}
using LoopPtr=std::shared_ptr<Loop>;
struct Handle { LoopPtr loop; std::shared_ptr<Channel> channel; };
inline LoopPtr& get_loop(PyObject* p) { auto* v=static_cast<LoopPtr*>(PyCapsule_GetPointer(p,"shell.rpc.loop")); if(!v) throw std::runtime_error("Invalid RPC executor"); return *v; }
inline Handle& get_channel(PyObject* p) { auto* v=static_cast<Handle*>(PyCapsule_GetPointer(p,"shell.rpc.channel")); if(!v) throw std::runtime_error("Invalid RPC channel"); return *v; }
inline PyObject* make_loop(PyObject*,PyObject*) {
    try { return PyCapsule_New(new LoopPtr(std::make_shared<Loop>()),"shell.rpc.loop",[](PyObject* p){
        auto* v=static_cast<LoopPtr*>(PyCapsule_GetPointer(p,"shell.rpc.loop"));
        // Explicit close is required while Python callbacks can still run.
        auto* s=PyEval_SaveThread(); delete v; PyEval_RestoreThread(s);
    }); } catch(const std::exception& e) { PyErr_SetString(PyExc_RuntimeError,e.what()); return nullptr; }
}
inline PyObject* open_channel(PyObject*,PyObject* args) {
    PyObject *p,*callback; unsigned long long fd; int listening=0;
    if(!PyArg_ParseTuple(args,"OKO|p",&p,&fd,&callback,&listening)) return nullptr;
    if(!PyCallable_Check(callback)) { PyErr_SetString(PyExc_TypeError,"callback required"); return nullptr; }
    try {
        auto loop=get_loop(p); auto c=std::make_shared<Channel>(loop.get(),static_cast<uv_os_sock_t>(fd),callback);
        c->listening=listening;
        loop->post([loop,c]{loop->channels.push_back(c); c->start();});
        return PyCapsule_New(new Handle{loop,c},"shell.rpc.channel",[](PyObject* cap){
            auto* h=static_cast<Handle*>(PyCapsule_GetPointer(cap,"shell.rpc.channel"));
            try { auto c=h->channel; h->loop->post([c]{c->finish("RPC channel released");}); } catch(...) {}
            auto* s=PyEval_SaveThread(); delete h; PyEval_RestoreThread(s);
        });
    } catch(const std::exception& e) { PyErr_SetString(PyExc_RuntimeError,e.what()); return nullptr; }
}
inline PyObject* send_frame(PyObject*,PyObject* args) {
    PyObject* p; const char* data; Py_ssize_t length;
    if(!PyArg_ParseTuple(args,"Oy#",&p,&data,&length)) return nullptr;
    try { auto& h=get_channel(p); auto c=h.channel; Buffer bytes(data,data+length); h.loop->post([c,bytes=std::move(bytes)]() mutable {c->send(std::move(bytes));}); Py_RETURN_NONE;
    } catch(const std::exception& e) {PyErr_SetString(PyExc_RuntimeError,e.what());return nullptr;}
}
inline PyObject* request_frame(PyObject*,PyObject* args) {
    PyObject* p;const char* data;Py_ssize_t count;unsigned long long id;int timeout;
    if(!PyArg_ParseTuple(args,"OKy#i",&p,&id,&data,&count,&timeout)) return nullptr;
    if(count<1 || count>static_cast<Py_ssize_t>(frame_limit-8) || timeout<1) {PyErr_SetString(PyExc_ValueError,"Invalid RPC request limits");return nullptr;}
    try {auto& h=get_channel(p);auto c=h.channel;Buffer bytes(data,data+count);
        h.loop->post([c,id,bytes=std::move(bytes),timeout]() mutable {start_request(c.get(),id,std::move(bytes),timeout);});Py_RETURN_NONE;
    } catch(const std::exception& e){PyErr_SetString(PyExc_RuntimeError,e.what());return nullptr;}
}
inline PyObject* cancel_request(PyObject*,PyObject* args) {
    PyObject* p;unsigned long long id;
    if(!PyArg_ParseTuple(args,"OK",&p,&id)) return nullptr;
    try {auto& h=get_channel(p);auto c=h.channel;h.loop->post([c,id]{c->cancel(id);});Py_RETURN_NONE;}
    catch(const std::exception& e){PyErr_SetString(PyExc_RuntimeError,e.what());return nullptr;}
}
inline PyObject* close_channel(PyObject*,PyObject* p) {
    try {auto& h=get_channel(p); auto c=h.channel; h.loop->post([c]{c->finish("RPC channel closed");});Py_RETURN_NONE;}
    catch(const std::exception& e){PyErr_SetString(PyExc_RuntimeError,e.what());return nullptr;}
}
inline PyObject* close_loop(PyObject*,PyObject* p) {
    try {auto loop=get_loop(p); auto* saved=PyEval_SaveThread(); loop->close(); PyEval_RestoreThread(saved);Py_RETURN_NONE;}
    catch(const std::exception& e){PyErr_SetString(PyExc_RuntimeError,e.what());return nullptr;}
}
} // namespace shell_transport
