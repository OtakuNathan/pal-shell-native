# Third-party code

The build downloads checksum-verified upstream source archives. Flux Foundry and
Portal headers are compiled into the extension; libuv is linked statically.
Their LICENSE files are installed alongside the wheel in `pal_shell_native_licenses`.

| Dependency | Source revision | License |
| --- | --- | --- |
| [Flux Foundry](https://github.com/OtakuNathan/flux_foundry) | `a93d246981a49648949c489a1918f5ccb61ed15d` | MIT |
| [Portal](https://github.com/OtakuNathan/portal) | `d0510e49d587228cb760dbdef2ea60c20753400e` | Apache-2.0 |
| [libuv](https://github.com/libuv/libuv) | `v1.50.0` | See bundled libuv LICENSE (MIT and component notices) |
| [Pal file engine](https://github.com/OtakuNathan/Pal) | Exact revision and source hashes in `pal_shell_worker/_file_core/SOURCE.json` | MIT; bundled `_file_core/LICENSE` |

Portal includes the Python executor GIL lifetime fix upstream. The build uses
its headers directly without a local patch. Libuv scheduling uses Flux Foundry's
bounded executor directly.

Native shell sources were extracted from Pal commit
`da16c294f143d040f3bfc064411c4244156cefeb`, Copyright (c) 2026 Nathan Wu,
under the MIT license. Pal's Python host adapter and integration tests remain
in [Pal](https://github.com/OtakuNathan/Pal/tree/main/native/shell_runtime).
