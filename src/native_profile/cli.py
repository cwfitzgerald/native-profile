"""Command-line interface for native-profile."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analyze import analyze
from .cargo import (
    BuildSelector,
    CargoError,
    build_and_locate,
    ensure_profiling_profile,
    find_cargo,
    looks_like_bench_binary,
)
from .profile import load_profile
from .report import to_json, to_markdown
from .samply import SamplyError, find_samply, record
from .symbolize import Symbolizer


def _default_out_dir(manifest_path: Path | None, exe: Path | None) -> Path:
    if manifest_path is not None:
        return manifest_path.parent / "target" / "native-profile"
    if exe is not None:
        return exe.parent / "native-profile-out"
    return Path.cwd() / "native-profile-out"


def _run_analysis(trace: Path, args, out_dir: Path) -> int:
    profile = load_profile(trace)
    symbolizer = Symbolizer.find(args.llvm_symbolizer)
    a = analyze(
        profile,
        symbolizer,
        symbolicate_system=args.symbolicate_system,
        filter_pattern=args.filter,
        top=args.top,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "analysis.json"
    md_path = out_dir / "analysis.md"
    json_path.write_text(json.dumps(to_json(a), indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(a), encoding="utf-8")

    print(f"\n[native-profile] total CPU {a.total_us/1e6:.2f}s ({a.weight_mode})")
    if not a.symbolicated:
        detail = ""
        if a.attempted_addrs:
            rate = 100 * a.resolved_addrs / a.attempted_addrs
            detail = f" ({a.resolved_addrs}/{a.attempted_addrs} addresses, {rate:.1f}%)"
        print(f"[native-profile] WARNING: not symbolicated (module-level only){detail}. "
              "Install llvm-symbolizer for function names, or see analysis notes for why "
              "resolution failed.")
    for n in a.notes:
        print(f"[native-profile] note: {n}")
    print("[native-profile] top self-time:")
    for r in a.self_rows[:8]:
        print(f"    {r.us/1e6:7.3f}s {r.pct:5.1f}%  {r.name[:88]}")
    if a.filter_pattern is not None:
        pct = 100 * a.filter_total_us / (a.total_us or 1)
        print(f"[native-profile] filter '{a.filter_pattern}': "
              f"{a.filter_total_us/1e6:.3f}s ({pct:.1f}%) self-time matched")
    print(f"\n[native-profile] wrote:\n    {json_path}\n    {md_path}")
    print(f"[native-profile] open the trace interactively with: samply load \"{trace}\"")
    return 0


def cmd_ensure_profile(args) -> int:
    manifest = Path(args.manifest_path)
    changed, notes = ensure_profiling_profile(manifest, args.profile)
    for n in notes:
        print(f"[native-profile] {n}")
    print(f"[native-profile] {'updated' if changed else 'no change to'} {manifest}")
    return 0


def cmd_analyze(args) -> int:
    trace = Path(args.trace)
    if not trace.is_file():
        print(f"[native-profile] trace not found: {trace}", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out else trace.parent
    return _run_analysis(trace, args, out_dir)


def cmd_run(args) -> int:
    manifest = Path(args.manifest_path).resolve() if args.manifest_path else None
    exe: Path | None = Path(args.exe).resolve() if args.exe else None

    if exe is None:
        # Cargo path: ensure profile, then build.
        if manifest is None:
            manifest = Path.cwd() / "Cargo.toml"
        if not manifest.is_file():
            print(f"[native-profile] no Cargo.toml at {manifest}; pass --manifest-path or --exe.",
                  file=sys.stderr)
            return 2
        if not args.no_ensure:
            changed, notes = ensure_profiling_profile(manifest, args.profile)
            for n in notes:
                print(f"[native-profile] {n}")
        cargo = find_cargo(args.cargo)
        selector = BuildSelector(
            example=args.example,
            bin=args.bin,
            bench=args.bench,
            package=args.package,
            features=args.features,
            no_default_features=args.no_default_features,
            extra=args.cargo_arg or [],
        )
        exe = build_and_locate(cargo, manifest, selector, args.profile)
        print(f"[native-profile] built {exe}")
        if args.bench and "--bench" not in args.prog_args:
            # A criterion (or libtest) harness only benchmarks with --bench; without it
            # it runs each benchmark once as a test and exits. We know this is a bench
            # target, so add the flag rather than silently recording a meaningless profile.
            args.prog_args = [*args.prog_args, "--bench"]
            print("[native-profile] appended --bench to program args (criterion/libtest "
                  "harnesses only benchmark with this flag; otherwise they run once as a test)")
    elif looks_like_bench_binary(exe) and "--bench" not in args.prog_args:
        print(f"[native-profile] WARNING: {exe} looks like a cargo bench/test artifact "
              "(under target/*/deps/). If it's a criterion benchmark, it needs --bench "
              "or it will run each benchmark once as a test and produce a meaningless "
              "profile. Pass it after --, e.g.: ... --exe <path> -- --bench")

    out_dir = Path(args.out) if args.out else _default_out_dir(manifest, exe)
    samply = find_samply(args.samply)
    trace = record(
        samply, exe, args.prog_args, out_dir,
        duration=args.duration, rate=args.rate,
        extra=(["--per-cpu-threads"] if args.per_cpu_threads else None),
    )
    print(f"[native-profile] trace: {trace}")
    if args.no_analyze:
        return 0
    return _run_analysis(trace, args, out_dir)


def _add_analysis_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--filter", help="regex; report a focused section for matching function names "
                                    r"(e.g. '^(wgpu|naga|ash|d3d12)')")
    p.add_argument("--top", type=int, default=25, help="number of rows per table (default 25)")
    p.add_argument("--llvm-symbolizer", help="path to llvm-symbolizer (else auto-detected)")
    p.add_argument("--symbolicate-system", action="store_true",
                   help="also symbolicate OS/system modules (usually lack local debug info)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="native-profile",
        description="Sampled CPU profiling of native binaries, symbolicated and digested for LLMs.",
    )
    p.add_argument("--version", action="version", version=f"native-profile {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pe = sub.add_parser("ensure-profile", help="ensure a debug-enabled cargo profile exists")
    pe.add_argument("--manifest-path", default="Cargo.toml")
    pe.add_argument("--profile", default="profiling")
    pe.set_defaults(func=cmd_ensure_profile)

    pr = sub.add_parser("run", help="ensure-profile -> build -> record -> analyze (the main entry)")
    pr.add_argument("--manifest-path", help="path to Cargo.toml (default ./Cargo.toml)")
    pr.add_argument("--profile", default="profiling", help="cargo profile to build (default 'profiling')")
    pr.add_argument("--example", help="cargo --example NAME")
    pr.add_argument("--bin", help="cargo --bin NAME")
    pr.add_argument("--bench", help="cargo --bench NAME; automatically appends --bench to the "
                                     "profiled binary's args (criterion/libtest only benchmark "
                                     "with that flag, else they run once as a test)")
    pr.add_argument("--package", "-p", help="cargo --package NAME")
    pr.add_argument("--features", help="cargo --features LIST")
    pr.add_argument("--no-default-features", action="store_true")
    pr.add_argument("--cargo-arg", action="append", help="extra raw arg passed to cargo build (repeatable)")
    pr.add_argument("--exe", help="profile this prebuilt binary directly; skips ensure-profile + build")
    pr.add_argument("--no-ensure", action="store_true", help="do not modify Cargo.toml")
    pr.add_argument("--duration", "-d", type=float, help="stop recording after N seconds (for long-running apps)")
    pr.add_argument("--rate", "-r", type=int, default=1000, help="sampling rate in Hz (default 1000)")
    pr.add_argument("--per-cpu-threads", action="store_true", help="samply --per-cpu-threads")
    pr.add_argument("--out", help="output dir for trace + analysis (default under target/native-profile)")
    pr.add_argument("--no-analyze", action="store_true", help="record only; skip analysis")
    pr.add_argument("--cargo", help="path to cargo")
    pr.add_argument("--samply", help="path to samply")
    _add_analysis_opts(pr)
    pr.set_defaults(func=cmd_run)

    pa = sub.add_parser("analyze", help="symbolicate + digest an existing trace (.json or .json.gz)")
    pa.add_argument("trace", help="path to a samply/Firefox-format profile")
    pa.add_argument("--out", help="output dir (default alongside the trace)")
    _add_analysis_opts(pa)
    pa.set_defaults(func=cmd_analyze)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Split program args (after `--`) so they are not parsed as our options.
    prog_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        prog_args = argv[i + 1:]
        argv = argv[:i]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.prog_args = prog_args
    try:
        return args.func(args)
    except (CargoError, SamplyError) as e:
        print(f"[native-profile] error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
