---
name: native-profile
description: >
  End-to-end sampled CPU profiling of native binaries (Rust/C/C++) via samply +
  llvm-symbolizer, digested into JSON + Markdown for iteration. Use this skill when the
  user wants to: profile a native binary, find CPU hotspots, see where time is spent,
  optimize a Rust program/example/bin, record a samply/Firefox profile, symbolicate a
  trace, analyze a .json.gz profile, get a flamegraph-style breakdown, investigate
  self/inclusive time by function or crate, or answer "why is this slow" / "what's the
  bottleneck" for a compiled program (e.g. bevy, wgpu, a CLI tool, a benchmark). Also use
  when handed an existing Firefox-format profile (prof.json / *.json.gz) to parse and
  attribute. DO NOT use for: browser/JS profiling interpretation, GPU frame debugging
  (use renderdoc), or memory profiling.
---

# native-profile

Sampled CPU profiling of a native binary end-to-end, for iterating on optimizations:
ensures a debug-enabled cargo profile, builds, records with `samply`, symbolicates with
`llvm-symbolizer` (saved samply traces contain only raw addresses, no function names),
and writes an `analysis.json` + `analysis.md` digest you can read directly.

## Invocation

Use `native-profile` directly if it is on PATH. Otherwise run it straight from the public
git repository (no checkout needed):

```sh
uvx --from git+https://github.com/cwfitzgerald/native-profile native-profile <subcommand> ...
```

Optional one-time install: `uv tool install --from git+https://github.com/cwfitzgerald/native-profile native-profile`.

## Workflow

1. **Record + analyze in one shot** (cargo target):

   ```sh
   native-profile run --manifest-path <proj>/Cargo.toml --example <name> --duration 10 \
       --filter '<regex>'
   ```

   - Target selection: `--example`, `--bin`, `--bench`, `--package`/`-p`; also
     `--features`, `--no-default-features`, and repeatable `--cargo-arg` for any other
     cargo build flag. `--manifest-path` defaults to `./Cargo.toml`.
   - **Programs that never exit on their own (games, servers) need `--duration N`**; a
     benchmark that exits by itself does not.
   - Args for the profiled program go after `--`: `... --bin foo -- --iterations 1000`.
   - Prebuilt binary (skips cargo entirely): `native-profile run --exe <path> -- <args>`.
   - `--rate` (Hz, default 1000): raise for runs shorter than a few seconds.
   - Side effect: `run` edits `Cargo.toml` (formatting-preserving) to guarantee
     `[profile.profiling]` with `inherits = "release"`, `debug = true`, `strip = false`.
     `--no-ensure` skips the edit; `--profile NAME` uses a different cargo profile.
     Never profile a stripped or debug-less build — you get addresses with no names.

   > **`--bench` is mandatory for criterion benchmarks — do not skip this.** A
   > criterion (or libtest) harness only *benchmarks* when it receives `--bench`;
   > without it, it runs each benchmark exactly once as a test ("Testing foo/bar" /
   > "Success") and exits in well under a second. The recording still "succeeds" and
   > produces a profile — it just measures nothing. This is the single most common way
   > to waste a profiling run on this tool.
   >
   > - Building from source: use the `--bench NAME` target selector (not `--bin`/
   >   `--example`). `native-profile` builds it and **automatically appends `--bench`**
   >   to the profiled binary's args — no extra flag needed.
   > - Prebuilt bench binary via `--exe`: you must add it yourself —
   >   `native-profile run --exe <path/to/bench-binary> -- --bench`. The tool warns
   >   loudly if `--exe` points at something under `target/*/deps/` (the raw,
   >   un-renamed location cargo leaves bench/test artifacts in) without `--bench` in
   >   the passthrough args, but it does not auto-add the flag for `--exe` since it
   >   can't know the harness for certain.

2. **Read the digest.** Output lands under `<proj>/target/native-profile/` (with
   `--exe`: `<exe-dir>/native-profile-out/`; override with `--out`): the trace
   `<name>.json.gz`, `analysis.json`, and `analysis.md`; exact paths are printed at the
   end. `Read` `analysis.md` for the human view; use `analysis.json` for structured data
   or full function names (the markdown truncates long generic names).

3. **Iterate.** Change code, re-run, compare. To re-focus an existing trace on a
   different subsystem without re-recording:

   ```sh
   native-profile analyze <trace>.json.gz --filter '^bevy_ecs'
   ```

   Writes next to the trace unless `--out` is given.

4. **Interactive view for the user** (optional): `samply load <trace>.json.gz` opens the
   Firefox Profiler UI in a browser.

## Interpreting the output

- Times are **CPU time** (samply's `threadCPUDelta`, µs, clamped to wall clock), summed
  across all threads. When CPU deltas are missing the tool falls back to
  sample-count × interval; the digest's `weight_mode` says which was used.
- **self time** = CPU spent in the function itself, attributed to the **innermost
  inlined frame** — this is where to optimize. **inclusive time** = the function plus
  everything it calls — use it to find expensive call trees / entry points.
- **by crate / by module** rollups show which library dominates. A `[module.dll]` row is
  an OS/driver module with no local symbols (expected for system DLLs).
- **callers**: for each hot (and filter-matched) function, the immediate callers of its
  self-time — who is driving the cost. `<root>` means the stack starts there.
- **`--filter REGEX`** (Python regex, searched anywhere in the demangled name — anchor
  with `^` for a crate) adds a focused section: matched self + inclusive tables and a
  headline total. The headline is the **sum of matched self time** — CPU actually
  executing inside the subsystem, not merely passing through it. Answers "how much time
  is in <subsystem>?". Examples:
  - wgpu/graphics: `'^(wgpu|naga|ash|d3d12|gpu_alloc|gpu_descriptor|metal|glow)'`
  - one crate: `'^bevy_ecs'`; allocation: `'(alloc|dealloc|malloc|free)'`
- Tables show `--top` rows (default 25).
- **"Symbolicated: yes/NO"** in the digest carries a resolved/attempted address count,
  e.g. `yes (1491/1518 addresses, 98.2%)`. Trust the count, not just the yes/no: it's
  the fraction of reachable addresses in local (non-system) libraries that
  llvm-symbolizer actually resolved to a name. **"not symbolicated (module-level
  only)"** means either `llvm-symbolizer` was not found — install LLVM or point at it
  with `--llvm-symbolizer` / the `LLVM_SYMBOLIZER` env var, then re-run `analyze` on the
  same trace, no re-record needed (`rustup component add llvm-tools` does **not**
  provide `llvm-symbolizer`) — or the resolve rate came back near zero despite a
  symbolizer running, which the tool downgrades to NO rather than reporting a false
  "yes"; check `notes` in `analysis.json`/`analysis.md` for the reason (mismatched/
  rebuilt/stripped binary, or a load-bias bug).

## Gotchas

- The output dir must be writable (samply drops a `<name>.kernel.etl` sidecar there).
- Optimized builds inline heavily: a hot function's self time may land in an unexpected
  leaf. The callers section and inclusive time disambiguate.
- Symbolication reads debug info from the binary paths recorded inside the trace
  (`libs[].path`). If the binary was rebuilt since recording, symbols can be silently
  wrong; if it was deleted (`cargo clean`) or the trace came from another machine,
  results degrade to module-level. Re-record rather than analyzing stale traces.
- Don't read the trace JSON expecting names, and don't use samply's
  `--unstable-presymbolicate` (it leaves Rust functions as `fun_<rva>`) — always go
  through `analyze`.
- System modules are skipped during symbolication by default; `--symbolicate-system`
  exists but rarely helps (OS DLLs lack local debug info).
- A digest note fires when total CPU time is under ~1s — implausibly short for a real
  workload, and almost always means a criterion bench ran once as a test (see the
  `--bench` warning above). If you see it, re-record with `--bench`.
- On macOS, samply's addresses are RVAs but Mach-O's `__TEXT` segment loads at a
  non-zero `vmaddr` (conventionally `0x100000000`, read from the binary's load
  commands rather than assumed); the tool adds that bias before calling
  `llvm-symbolizer`. Handled internally — but it is why the resolve-rate count is worth
  reading: a wrong bias misses every lookup while every other signal looks healthy.
