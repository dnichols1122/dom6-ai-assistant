/*
 * trn_trace.js — Frida script for dom6_amd64 .trn serializer tracing
 *
 * Hooks the known serializer sub-functions and write primitives to build a
 * complete structural map of the .trn file as it is written.
 *
 * Inject while the game is running (no restart needed):
 *   uv run frida -p $(pgrep -f dom6_amd64) -l src/hooks/trn_trace.js \
 *     --no-pause 2>/dev/null | tee /tmp/trn_trace.log
 *
 * Then end a turn in-game.  Ctrl-C after the turn screen appears.
 * The log goes to /tmp/trn_trace.log.
 *
 * Known serializer RVAs (from disassembly, prior session):
 *   0x3448c0  main serializer        (nation_id:rdi, FILE*:rsi)
 *   0x34b2a0  file header writer     (FILE*:rdi)
 *   0x34c5e0  nation block writer    (nation_id:rdi, slot:rsi, FILE*:rdx, flag:rcx)
 *   0x34c370  province serializer    (province_idx:rdi, FILE*:rsi)
 *   0x34dbb0  nation econ block      (nation_id:rdi, FILE*:rsi)
 *   0x34e220  commander/entity       (ptr:rdi, FILE*:rsi)
 *   0x54c940  XOR string writer      (char*:rdi, stream:rsi)
 *   0x54ce10  int16 write primitive  (value:rdi, stream:rsi)
 *   0x54d240  byte  write primitive  (value:rdi, stream:rsi)
 */

"use strict";

// ---------------------------------------------------------------------------
// Module base
// ---------------------------------------------------------------------------

const DOM6_MODULE = "dom6_amd64";
const _mod = Process.findModuleByName(DOM6_MODULE);
if (!_mod) {
    console.error("[trn_trace] ERROR: dom6_amd64 module not found");
    throw new Error("module not found");
}
const base = _mod.base;
console.log("[trn_trace] base address: " + base);

function rva(offset) {
    return base.add(offset);
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let tracing      = false;   // are we inside the main serializer?
let bytePos      = 0;       // running byte offset in the output buffer
let section      = "?";     // current high-level section name
let outputStream = null;    // FILE* used by the serializer (captured at entry)

// Stack of (section_name, start_pos) for nesting
const sectionStack = [];

function pushSection(name) {
    sectionStack.push({ name: section, pos: bytePos });
    section = name;
}

function popSection() {
    const size = bytePos - sectionStack[sectionStack.length - 1].pos;
    const prev = sectionStack.pop();
    console.log(JSON.stringify({
        type: "section_end",
        name: section,
        start: "0x" + (bytePos - size).toString(16).padStart(6, "0"),
        end:   "0x" + bytePos.toString(16).padStart(6, "0"),
        size:  size,
    }));
    section = prev ? prev.name : "?";
}

function emit(obj) {
    obj.pos = "0x" + bytePos.toString(16).padStart(6, "0");
    console.log(JSON.stringify(obj));
}

// ---------------------------------------------------------------------------
// Main serializer gate
// ---------------------------------------------------------------------------

Interceptor.attach(rva(0x3448c0), {
    onEnter(args) {
        tracing = true;
        bytePos = 0;
        sectionStack.length = 0;
        section = "root";
        outputStream = args[1];   // FILE* passed to serializer — all fwrites use this
        const nationId = args[0].toInt32();
        emit({ type: "serializer_start", nation_id: nationId,
               stream: outputStream.toString() });
    },
    onLeave(_) {
        emit({ type: "serializer_end", total_bytes: bytePos });
        tracing = false;
        outputStream = null;
    }
});

// ---------------------------------------------------------------------------
// High-level section probes
// ---------------------------------------------------------------------------

Interceptor.attach(rva(0x34b2a0), {
    onEnter(_) {
        if (!tracing) return;
        pushSection("HEADER");
        emit({ type: "section_start", name: "HEADER" });
    },
    onLeave(_) {
        if (!tracing) return;
        popSection();
    }
});

Interceptor.attach(rva(0x34c5e0), {
    onEnter(args) {
        if (!tracing) return;
        const nationId = args[0].toInt32();
        const slot     = args[1].toInt32();
        const flag     = args[3].toInt32();
        const name     = "NATION_BLOCK[" + nationId + ",slot=" + slot + ",flag=" + flag + "]";
        pushSection(name);
        emit({ type: "section_start", name, nation_id: nationId, slot, flag });
    },
    onLeave(_) {
        if (!tracing) return;
        popSection();
    }
});

Interceptor.attach(rva(0x34c370), {
    onEnter(args) {
        if (!tracing) return;
        const idx  = args[0].toInt32();
        const name = "PROVINCE[" + idx + "]";
        pushSection(name);
        emit({ type: "section_start", name, province_idx: idx });
    },
    onLeave(_) {
        if (!tracing) return;
        popSection();
    }
});

Interceptor.attach(rva(0x34dbb0), {
    onEnter(args) {
        if (!tracing) return;
        const nationId = args[0].toInt32();
        const name     = "NATION_ECON[" + nationId + "]";
        pushSection(name);
        emit({ type: "section_start", name, nation_id: nationId });
    },
    onLeave(_) {
        if (!tracing) return;
        popSection();
    }
});

Interceptor.attach(rva(0x34e220), {
    onEnter(args) {
        if (!tracing) return;
        // First arg is a pointer to the entity struct — log its address;
        // we'll decode the struct fields later once we know the layout.
        const ptr  = args[0];
        const name = "COMMANDER[ptr=" + ptr + "]";
        pushSection(name);
        emit({ type: "section_start", name, ptr: ptr.toString() });
    },
    onLeave(_) {
        if (!tracing) return;
        popSection();
    }
});

// ---------------------------------------------------------------------------
// Write primitives — update byte counter + log values
// ---------------------------------------------------------------------------

// XOR string writer: rdi = raw (pre-XOR) C string pointer.
// bytePos is NOT updated here — the writer calls fwrite internally for each
// byte, so the fwrite hook below tracks the position automatically.
Interceptor.attach(rva(0x54c940), {
    onEnter(args) {
        if (!tracing) return;
        try {
            const s = args[0].readCString() || "";
            emit({ type: "xstr", section, value: s, len: s.length + 1 });
        } catch (_) {
            emit({ type: "xstr", section, value: "<err>", len: 0 });
        }
    }
});

// int16 writer: rdi = value.  bytePos tracked via fwrite hook.
Interceptor.attach(rva(0x54ce10), {
    onEnter(args) {
        if (!tracing) return;
        const v = args[0].toInt32() & 0xFFFF;
        const signed = v >= 0x8000 ? v - 0x10000 : v;
        emit({ type: "i16", section, value: signed, raw: "0x" + v.toString(16) });
    }
});

// byte writer: rdi = value.  bytePos tracked via fwrite hook.
Interceptor.attach(rva(0x54d240), {
    onEnter(args) {
        if (!tracing) return;
        const v = args[0].toInt32() & 0xFF;
        emit({ type: "u8", section, value: v, raw: "0x" + v.toString(16) });
    }
});

// fwrite PLT stub (0xbc380 = fwrite@GLIBC_2.2.5).
// The serializer calls fwrite directly for int32 values and bulk data arrays.
// fwrite(const void *ptr, size_t size, size_t nmemb, FILE *stream)
//   rdi=ptr  rsi=size  rdx=count  rcx=stream
// We only count calls to the serializer's output stream, not other file I/O.
Interceptor.attach(rva(0xbc380), {
    onEnter(args) {
        if (!tracing || !outputStream) return;
        const stream = args[3];
        if (!stream.equals(outputStream)) return;
        const size    = args[1].toUInt32();
        const count   = args[2].toUInt32();
        const nbytes  = size * count;
        // Read up to 16 bytes of the buffer for the log
        let preview = "";
        try {
            const buf = args[0].readByteArray(Math.min(nbytes, 16));
            preview = Array.from(new Uint8Array(buf))
                          .map(b => b.toString(16).padStart(2, "0"))
                          .join(" ");
        } catch(_) {}
        emit({ type: "fwrite", section, size, count, nbytes, preview });
        bytePos += nbytes;
    }
});

// ---------------------------------------------------------------------------
// Done
// ---------------------------------------------------------------------------

console.log("[trn_trace] All probes attached. End a turn to capture.");
