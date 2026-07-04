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

Use `native-profile` directly if it is on PATH. Otherwise run it from this repo (the
directory containing this SKILL.md):

```sh
uvx --from <path-to-this-repo> native-profile <subcommand> ...
```

Optional one-time install: `uv tool install --from <path-to-this-repo> native-profile`.

## Workflow

1. **Record + analyze in one shot** (cargo target):

   ```sh
   native-profile run --manifest-path <proj>/Cargo.toml --example <name> --duration 10 \
       --filter '<regex>'
   ```

   - Target selection: `--example`, `--bin`, `--package`/`-p`; also `--features`,
     `--no-default-features`, and repeatable `--cargo-arg` for any other cargo build
     flag. `--manifest-path` defaults to `./Cargo.toml`.
   - **Programs that never exit on their own (games, servers) need `--duration N`**; a
     benchmark that exits by itself does not.
   - Args for the profiled program go after `--`: `... --bin foo -- --iterations 1000`.
   - Prebuilt binary (skips cargo entirely): `native-profile run --exe <path> -- <args>`.
   - `--rate` (Hz, default 1000): raise for runs shorter than a few seconds.
   - Side effect: `run` edits `Cargo.toml` (formatting-preserving) to guarantee
     `[profile.profiling]` with `inherits = "release"`, `debug = true`, `strip = false`.
     `--no-ensure` skips the edit; `--profile NAME` uses a different cargo profile.
     Never profile a stripped or debug-less build — you get addresses with no names.

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
- **"not symbolicated (module-level only)"** = `llvm-symbolizer` was not found. Install
  LLVM or point at it with `--llvm-symbolizer` / the `LLVM_SYMBOLIZER` env var, then
  re-run `analyze` on the same trace — no re-record needed. (`rustup component add
llvm-tools` does **not** provide `llvm-symbolizer`.)

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
