# native-profile

> Vibe coded.

End-to-end **sampled CPU profiling of native binaries**, designed so an LLM (or a
human) can record a trace and iterate on optimizations without fighting tooling.

It wraps three tools into one pipeline:

1. **cargo** — ensures a `profiling` build profile with `debug = true` (unstripped), then builds your target.
2. **[samply](https://github.com/mstange/samply)** — records a Firefox-format sampling profile.
3. **[llvm-symbolizer](https://llvm.org/docs/CommandGuide/llvm-symbolizer.html)** — resolves the raw addresses (samply does *not* bake symbols into saved traces) using the debug info the profile already points at.

…and produces a compact **`analysis.json`** + **`analysis.md`** digest: self/inclusive
time by function, rollups by crate and module, caller trees for the hottest functions,
and an optional `--filter` regex to focus on a subsystem (e.g. `wgpu`).

## Requirements

- `cargo` (for the build path) — <https://rustup.rs>
- `samply` — `cargo install samply` or `cargo binstall samply`
- `llvm-symbolizer` — ships with a full LLVM install (Windows: `C:\Program Files\LLVM\bin`;
  Linux: `llvm` package; macOS: `brew install llvm`). Without it the tool still runs but
  reports **module-level** results only. (Note: `rustup component add llvm-tools` does *not*
  include `llvm-symbolizer`.)
- On Windows, samply uses ETW; recording may require an **Administrator** terminal.

## Usage

Run without installing via `uvx`:

```sh
# Build the profiling profile, record a cargo example, and digest it:
uvx --from /path/to/native-profile native-profile run \
    --manifest-path /path/to/project/Cargo.toml \
    --example my_example --duration 10 \
    --filter '^(wgpu|naga|ash|d3d12)'

# Re-analyze an existing trace with a different filter (no re-recording):
uvx --from /path/to/native-profile native-profile analyze trace.json.gz --filter '^bevy_ecs'

# Profile an arbitrary prebuilt binary (skips cargo):
uvx --from /path/to/native-profile native-profile run --exe ./target/release/mytool -- --some arg
```

Or install once for a short command:

```sh
uv tool install --from /path/to/native-profile native-profile
native-profile run --example my_example -d 10
```

### Subcommands

- `ensure-profile` — only guarantee `[profile.profiling]` (debug on, strip off) in `Cargo.toml`.
- `run` — `ensure-profile` → `cargo build --profile profiling` → `samply record` → `analyze`. The main entry point.
- `analyze` — symbolicate + digest an existing `.json`/`.json.gz` trace.

Program arguments for the profiled binary go after `--`.

## Output

`analyze`/`run` write `analysis.json` and `analysis.md` next to the trace (or to `--out`)
and print a short summary. Times are **CPU time** (from samply's `threadCPUDelta`, in µs,
clamped to wall-clock), aggregated across all threads.

See [`SKILL.md`](SKILL.md) for the agent-facing workflow and interpretation guide.
