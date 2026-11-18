"""Generate native records and Portal declarations from the packaged wire contract."""
import json
from pathlib import Path
import sys


def codec(name):
    return f"w::blob<{int(name[4:])}>" if name.startswith("blob") else f"w::{name}"


def generate(schema):
    yield "#pragma once"
    yield '#include "portal/rpc/typed.h"'
    yield "namespace w = portal::wire;"
    yield "namespace rpc = portal::typed_rpc;"
    for name, (identifier, fields) in schema["records"].items():
        yield f"struct Local{name} {{"
        for field, kind in fields.items():
            yield f"    {codec(kind)}::value_type {field}{{}};"
        yield "};"
        yield f"#define LOCAL_FIELDS_{name}(X,T) " + " ".join(
            f"X(T,{i},{field},{codec(kind)})" for i, (field, kind) in enumerate(fields.items(), 1)
        )
        yield f"PORTAL_WIRE_RECORD(Local{name}, {identifier}, 1, LOCAL_FIELDS_{name});"
        yield f"#undef LOCAL_FIELDS_{name}"
    def method_codec(name):
        return f"w::record<Local{name}>" if name in schema["records"] else codec(name)
    for name, (identifier, result, args) in schema["methods"].items():
        codecs = ", ".join(map(method_codec, [result, *args]))
        yield f"using LocalRpc{name} = rpc::method<{identifier}, 1, {codecs}>;"


if __name__ == "__main__":
    source, target = map(Path, sys.argv[1:])
    target.write_text("\n".join(generate(json.loads(source.read_text()))) + "\n")
