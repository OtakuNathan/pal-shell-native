#pragma once
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace dynabridge::pal_shell {
// Pure owner-thread transitions. The reactor supplies monotonic milliseconds.
struct SessionLifecycle {
    using clock_t = std::uint64_t;
    clock_t started = 0, deadline = 0, wake = 0, generation = 0;
    bool watching = true, stopped = false;

    void start(clock_t now, clock_t budget) {
        started = now;
        deadline = budget ? now + budget : 0;
    }
    void require_running(clock_t now) const {
        if (stopped || (deadline && now >= deadline))
            throw std::runtime_error("session_not_running: deadline reached or termination begun");
    }
    void extend(clock_t now, clock_t delta) {
        require_running(now);
        if (!deadline) throw std::runtime_error("no_deadline: session has no finite deadline");
        if (!delta || delta > std::numeric_limits<clock_t>::max() - deadline)
            throw std::runtime_error("invalid_extension: positive non-overflowing delta required");
        deadline += delta;
    }
    void watch(clock_t now, clock_t wait, clock_t delta) {
        require_running(now);
        if (!wait || wait > 300000) throw std::runtime_error("invalid_wait: use 1..300000 milliseconds");
        if (delta) extend(now, delta);
        watching = true;
        wake = now + wait;
        ++generation;
    }
    void unwatch() { watching = false; wake = 0; ++generation; }
    void stop() { stopped = true; wake = 0; }
    bool fire(clock_t now, clock_t captured_generation) {
        if (stopped || !watching || captured_generation != generation || !wake || now < wake
            || (deadline && now >= deadline)) return false;
        wake = 0;
        return true;
    }
};
}
