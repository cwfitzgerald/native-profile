"""Load and lightly model a Firefox-format profile (as saved by samply)."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path


def load_profile(path: Path) -> dict:
    """Load a profile that may be gzipped (.json.gz) or plain JSON."""
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip magic
        data = gzip.decompress(data)
    return json.loads(data)


_WIN_SYS = None


def is_system_path(p: str | None) -> bool:
    """Heuristic: does this path live in an OS directory (no local debug info)?"""
    if not p:
        return True  # unknown path -> can't symbolicate anyway
    global _WIN_SYS
    lp = p.replace("\\", "/").lower()
    if os.name == "nt":
        if _WIN_SYS is None:
            root = os.environ.get("SystemRoot", r"C:\Windows").replace("\\", "/").lower()
            _WIN_SYS = root
        return lp.startswith(_WIN_SYS)
    return lp.startswith(("/usr/", "/lib/", "/lib64/", "/system/"))


def sample_weights_us(thread: dict, interval_ms: float) -> list[float]:
    """Per-sample CPU weight in microseconds.

    Prefers threadCPUDelta (µs of real CPU between samples), clamped to the wall-clock
    delta so a large first-sample spike cannot dominate. Falls back to uniform interval
    weighting when CPU deltas are unavailable.
    """
    samples = thread["samples"]
    n = samples["length"]
    cpud = samples.get("threadCPUDelta")
    tds = samples.get("timeDeltas")
    if cpud is not None:
        out = []
        for i in range(n):
            w = cpud[i] or 0.0
            if tds is not None:
                cap = (tds[i] or 0.0) * 1000.0  # ms -> µs
                if cap > 0 and w > cap:
                    w = cap
            out.append(float(w))
        return out
    # Fallback: each sample represents `interval_ms` of wall time.
    w = interval_ms * 1000.0
    weights = samples.get("weight")
    if weights is not None:
        return [float((weights[i] or 1)) * w for i in range(n)]
    return [w] * n
