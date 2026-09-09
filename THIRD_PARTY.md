# Third-party code

The build downloads checksum-verified upstream source archives. Flux Foundry and
dynabridge headers are compiled into the extension; libuv is linked statically.
Their LICENSE files are installed alongside the wheel in `pal_shell_native_licenses`.

| Dependency | Source revision | License |
| --- | --- | --- |
| [Flux Foundry](https://github.com/OtakuNathan/flux_foundry) | `781375ab57884cbccb84b9d91133c2b2a22a95e9` | MIT |
| [dynabridge](https://github.com/OtakuNathan/dynabridge) | `0762d81175a6f3172b4a142b026d8392150fca6f` | Apache-2.0 |
| [libuv](https://github.com/libuv/libuv) | `v1.50.0` | See bundled libuv LICENSE (MIT and component notices) |

`dependencies/bridge-gil-lifetime.patch` is the pending dynabridge fix carried
from Pal: destroy Python-owned task captures while still holding the GIL. The
patch is applied only inside the build dependency tree, never to a sibling checkout.

Native shell sources were extracted from Pal commit
`da16c294f143d040f3bfc064411c4244156cefeb`, Copyright (c) 2026 Nathan Wu,
under the MIT license. Pal's Python host adapter and integration tests remain
in [Pal](https://github.com/OtakuNathan/Pal/tree/main/native/shell_runtime).
