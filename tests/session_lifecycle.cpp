#include "session_lifecycle.h"
#undef NDEBUG
#include <cassert>
using dynabridge::pal_shell::SessionLifecycle;
template<class F> void rejected(F action) { bool rejected = false; try { action(); } catch (...) { rejected = true; } assert(rejected); }
int main() {
    SessionLifecycle s;
    s.start(100, 100);
    s.watch(110, 10, 50);
    assert(s.deadline == 250);
    auto old = s.generation;
    s.watch(115, 20, 0);
    assert(!s.fire(140, old));
    assert(s.fire(140, s.generation));
    assert(!s.fire(141, s.generation));
    s.unwatch();
    assert(!s.watching && !s.wake && s.deadline == 250);
    s.watch(150, 20, 0);
    s.stop();
    assert(!s.fire(180, s.generation));
    rejected([&] { s.extend(180, 20); });
    SessionLifecycle infinite;
    infinite.start(10, 0);
    rejected([&] { infinite.watch(20, 10, 20); });
    assert(infinite.deadline == 0 && infinite.generation == 0 && infinite.wake == 0);
    infinite.watch(20, 10, 0);
    assert(infinite.fire(30, infinite.generation));
    SessionLifecycle expired;
    expired.start(0, 10);
    rejected([&] { expired.extend(10, 20); });
    assert(expired.deadline == 10);
    expired.watch(5, 10, 0);
    assert(!expired.fire(15, expired.generation));
}
