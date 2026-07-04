"""samply integration: record a native binary to a Firefox-format trace."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


class SamplyError(RuntimeError):
    pass


def find_samply(override: str | None = None) -> str:
    cand = override or shutil.which("samply")
    if not cand:
        raise SamplyError(
            "`samply` not found on PATH. Install it with `cargo install samply` "
            "(or `cargo binstall samply`), or pass --samply."
        )
    return cand


def record(
    samply: str,
    exe: Path,
    prog_args: list[str],
    out_dir: Path,
    *,
    trace_name: str | None = None,
    duration: float | None = None,
    rate: int | None = None,
    extra: list[str] | None = None,
) -> Path:
    """Record `exe prog_args` with samply and return the path to the saved trace (.json.gz).

    samply writes a `<stem>.kernel.etl` sidecar next to the output on Windows and
    panics if it cannot be deleted, so `out_dir` must be writable.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = trace_name or exe.stem
    trace = out_dir / f"{stem}.json.gz"

    args = [samply, "record", "--save-only", "-n", "-o", str(trace)]
    if duration is not None:
        args += ["-d", str(duration)]
    if rate is not None:
        args += ["-r", str(rate)]
    if extra:
        args += extra
    args += ["--", str(exe), *prog_args]

    print(f"[native-profile] recording: {' '.join(args)}", file=sys.stderr)
    proc = subprocess.run(args)
    if proc.returncode != 0:
        raise SamplyError(
            f"samply exited with {proc.returncode}. On Windows, ETW sampling may require "
            "running the terminal as Administrator. Ensure the output dir is writable."
        )
    if not trace.is_file():
        raise SamplyError(f"samply did not produce a trace at {trace}")
    return trace
