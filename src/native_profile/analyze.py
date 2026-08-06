"""Aggregate a symbolicated profile into self/inclusive time by function, crate, and module."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .profile import is_system_path, sample_weights_us
from .symbolize import Symbolizer

_CRATE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)(?:::|\.\.)")

# Below this fraction of addresses resolved, treat the run as effectively unsymbolicated
# (e.g. wrong load bias, stripped/rebuilt binary) rather than reporting a false "yes".
_MIN_RESOLVE_RATE = 0.02

# Below this much total CPU time, a recorded profile is almost certainly noise rather
# than a real workload -- most commonly a criterion bench run without `--bench`.
_MIN_PLAUSIBLE_CPU_US = 1_000_000.0


def crate_of(name: str) -> str:
    if name.startswith("["):  # module pseudo-frame, e.g. [ntdll.dll]
        return name
    m = _CRATE_RE.search(name)
    if m:
        return m.group(1)
    return name.split("(")[0][:48]


@dataclass
class Row:
    name: str
    us: float
    pct: float
    crate: str = ""
    module: str = ""


@dataclass
class Analysis:
    total_us: float
    weight_mode: str
    interval_ms: float
    process: str
    threads: list[tuple[str, float]]           # (name, cpu_us) sorted desc
    self_rows: list[Row]
    incl_rows: list[Row]
    by_crate: list[Row]
    by_module: list[Row]
    callers: dict[str, list[tuple[str, float]]]  # fn name -> [(caller, us)]
    filter_pattern: str | None = None
    filter_self: list[Row] = field(default_factory=list)
    filter_incl: list[Row] = field(default_factory=list)
    filter_total_us: float = 0.0
    symbolicated: bool = True
    resolved_addrs: int = 0
    attempted_addrs: int = 0
    notes: list[str] = field(default_factory=list)


def _resolve_libs_to_symbolize(profile: dict, symbolicate_system: bool):
    """Return {global_lib_index: (lib_name, path)} for libs we should symbolicate."""
    libs = profile.get("libs", [])
    eligible = {}
    for i, lib in enumerate(libs):
        path = lib.get("path")
        if not path:
            continue
        if not symbolicate_system and is_system_path(path):
            continue
        if os.path.isfile(path):
            eligible[i] = (lib.get("name", f"lib{i}"), path)
    return eligible


def analyze(
    profile: dict,
    symbolizer: Symbolizer | None,
    *,
    symbolicate_system: bool = False,
    filter_pattern: str | None = None,
    top: int = 25,
) -> Analysis:
    interval_ms = float(profile.get("meta", {}).get("interval", 1.0))
    process = profile.get("meta", {}).get("product", "process")
    notes: list[str] = []

    # 1) Collect reachable addresses per global lib.
    eligible = _resolve_libs_to_symbolize(profile, symbolicate_system)
    addrs_by_lib: dict[int, set[int]] = defaultdict(set)
    for t in profile["threads"]:
        rt = t["resourceTable"]
        res_lib = rt["lib"]
        func_res = t["funcTable"]["resource"]
        frame_func = t["frameTable"]["func"]
        frame_addr = t["frameTable"]["address"]
        st_frame = t["stackTable"]["frame"]
        st_prefix = t["stackTable"]["prefix"]
        reach = set()
        for stk in t["samples"]["stack"]:
            cur = stk
            while cur is not None and cur not in reach:
                reach.add(cur)
                cur = st_prefix[cur]
        for stk in reach:
            fr = st_frame[stk]
            r = func_res[frame_func[fr]]
            if r is None or r < 0:
                continue
            lib = res_lib[r]
            if lib in eligible:
                a = frame_addr[fr]
                if a is not None and a >= 0:
                    addrs_by_lib[lib].add(a)

    # 2) Symbolicate each eligible lib once.
    sym_maps: dict[int, dict[int, list[str]]] = {}
    attempted_addrs = 0
    resolved_addrs = 0
    symbolicated = symbolizer is not None
    if symbolizer is not None:
        for lib_idx, (lib_name, path) in eligible.items():
            addrs = sorted(addrs_by_lib.get(lib_idx, ()))
            if not addrs:
                continue
            m = symbolizer.symbolize(path, addrs)
            sym_maps[lib_idx] = m
            attempted_addrs += len(addrs)
            resolved_addrs += sum(1 for a in addrs if m.get(a))
        resolve_rate = (resolved_addrs / attempted_addrs) if attempted_addrs else 1.0
        if attempted_addrs > 0 and resolve_rate < _MIN_RESOLVE_RATE:
            symbolicated = False
            notes.append(
                f"symbolication resolved only {resolved_addrs}/{attempted_addrs} addresses "
                f"({100 * resolve_rate:.1f}%) -- treating as unsymbolicated. Likely a wrong "
                "load-bias calculation, a stripped binary, or a binary that no longer matches "
                "the trace (rebuilt/deleted since recording)."
            )
    else:
        notes.append(
            "llvm-symbolizer not found: results are MODULE-LEVEL only. Install LLVM "
            "(so `llvm-symbolizer` is on PATH) or pass --llvm-symbolizer to get function names."
        )

    # Global name interning.
    names: list[str] = []
    name_id: dict[str, int] = {}
    crate_of_id: list[str] = []

    def intern(n: str) -> int:
        i = name_id.get(n)
        if i is None:
            i = len(names)
            name_id[n] = i
            names.append(n)
            crate_of_id.append(crate_of(n))
        return i

    self_us: dict[int, float] = defaultdict(float)
    incl_us: dict[int, float] = defaultdict(float)
    crate_us: dict[str, float] = defaultdict(float)
    module_us: dict[str, float] = defaultdict(float)
    total_us = 0.0
    thread_cpu: list[tuple[str, float]] = []

    # Cache per-thread frame -> (inner_id, inline_ids tuple, module) for the caller pass.
    per_thread_cache = []

    for t in profile["threads"]:
        sa = t["stringArray"]
        rt = t["resourceTable"]
        res_lib = rt["lib"]
        res_name = rt["name"]
        ft = t["funcTable"]
        func_res = ft["resource"]
        func_name = ft["name"]
        frame_func = t["frameTable"]["func"]
        frame_addr = t["frameTable"]["address"]
        st_frame = t["stackTable"]["frame"]
        st_prefix = t["stackTable"]["prefix"]
        nframes = t["frameTable"]["length"]

        frame_inner = [0] * nframes
        frame_inls: list[tuple[int, ...]] = [()] * nframes
        frame_mod = [""] * nframes
        for fr in range(nframes):
            fn = frame_func[fr]
            r = func_res[fn]
            lib = res_lib[r] if (r is not None and r >= 0) else None
            mod = (sa[res_name[r]] if (r is not None and r >= 0 and res_name[r] is not None and res_name[r] >= 0)
                   else "<unknown>")
            frame_mod[fr] = mod
            resolved = None
            if lib is not None and lib in sym_maps:
                a = frame_addr[fr]
                resolved = sym_maps[lib].get(a)
            if resolved:
                ids = tuple(intern(n) for n in resolved)
            else:
                # Fall back to the profile's own function name, else a module label.
                raw = sa[func_name[fn]] if func_name[fn] is not None and func_name[fn] >= 0 else ""
                if raw and not raw.startswith("0x"):
                    label = raw
                elif mod != "<unknown>":
                    label = f"[{mod}]"
                else:
                    label = "[unknown]"
                ids = (intern(label),)
            frame_inls[fr] = ids
            frame_inner[fr] = ids[0]

        weights = sample_weights_us(t, interval_ms)
        stacks = t["samples"]["stack"]
        tcpu = 0.0
        for i, stk in enumerate(stacks):
            w = weights[i]
            tcpu += w
            total_us += w
            if stk is None or w == 0.0:
                continue
            leaf = st_frame[stk]
            inner = frame_inner[leaf]
            self_us[inner] += w
            crate_us[crate_of_id[inner]] += w
            module_us[frame_mod[leaf]] += w
            seen: set[int] = set()
            cur = stk
            while cur is not None:
                for nid in frame_inls[st_frame[cur]]:
                    if nid not in seen:
                        seen.add(nid)
                        incl_us[nid] += w
                cur = st_prefix[cur]
        thread_cpu.append((t.get("name", "?"), tcpu))
        per_thread_cache.append((frame_inner, frame_inls, st_frame, st_prefix, stacks, weights))

    total = total_us or 1.0

    def rows(d: dict[int, float], n: int) -> list[Row]:
        out = []
        for nid, us in sorted(d.items(), key=lambda x: -x[1])[:n]:
            out.append(Row(names[nid], us, 100 * us / total, crate_of_id[nid]))
        return out

    self_rows = rows(self_us, top)
    incl_rows = rows(incl_us, top)
    by_crate = [Row(k, v, 100 * v / total, k) for k, v in
                sorted(crate_us.items(), key=lambda x: -x[1])[:top]]
    by_module = [Row(k, v, 100 * v / total, "", k) for k, v in
                 sorted(module_us.items(), key=lambda x: -x[1])[:top]]

    # Determine "interesting" targets for caller analysis: top self + filter matches.
    interesting: set[int] = {nid for nid, _ in sorted(self_us.items(), key=lambda x: -x[1])[:min(top, 12)]}
    filt = re.compile(filter_pattern) if filter_pattern else None
    filter_self: list[Row] = []
    filter_incl: list[Row] = []
    filter_total_us = 0.0
    if filt is not None:
        match_ids = [nid for nid in range(len(names)) if filt.search(names[nid])]
        fs = {nid: self_us[nid] for nid in match_ids if self_us.get(nid, 0) > 0}
        fi = {nid: incl_us[nid] for nid in match_ids if incl_us.get(nid, 0) > 0}
        filter_self = rows(fs, top)
        filter_incl = rows(fi, top)
        filter_total_us = sum(fs.values())
        interesting |= set(fs.keys())

    # Caller pass over cached frame arrays.
    callers_acc: dict[int, dict[int, float]] = {nid: defaultdict(float) for nid in interesting}
    for (frame_inner, frame_inls, st_frame, st_prefix, stacks, weights) in per_thread_cache:
        for i, stk in enumerate(stacks):
            w = weights[i]
            if stk is None or w == 0.0:
                continue
            leaf = st_frame[stk]
            inner = frame_inner[leaf]
            if inner not in callers_acc:
                continue
            caller = None
            # Prefer the next outer inlined frame at the leaf.
            for nid in frame_inls[leaf][1:]:
                if nid != inner:
                    caller = nid
                    break
            if caller is None:
                cur = st_prefix[stk]
                while cur is not None:
                    cand = frame_inner[st_frame[cur]]
                    if cand != inner:
                        caller = cand
                        break
                    cur = st_prefix[cur]
            callers_acc[inner][caller if caller is not None else -1] += w

    callers: dict[str, list[tuple[str, float]]] = {}
    for nid, d in callers_acc.items():
        top_callers = sorted(d.items(), key=lambda x: -x[1])[:8]
        callers[names[nid]] = [
            (("<root>" if cid == -1 else names[cid]), us) for cid, us in top_callers
        ]

    weight_mode = ("threadCPUDelta (CPU microseconds)"
                   if profile["threads"] and profile["threads"][0]["samples"].get("threadCPUDelta") is not None
                   else "sample count x interval")

    if total_us < _MIN_PLAUSIBLE_CPU_US:
        notes.append(
            f"only {total_us / 1e6:.3f}s of CPU time recorded across all samples -- "
            "implausibly short for a real workload. If this is a criterion benchmark, it "
            "most likely ran once as a *test* rather than benchmarking: criterion needs the "
            "`--bench` flag (`native-profile run --bench NAME`, or `--exe ... -- --bench`)."
        )

    return Analysis(
        total_us=total_us,
        weight_mode=weight_mode,
        interval_ms=interval_ms,
        process=process,
        threads=sorted(thread_cpu, key=lambda x: -x[1]),
        self_rows=self_rows,
        incl_rows=incl_rows,
        by_crate=by_crate,
        by_module=by_module,
        callers=callers,
        filter_pattern=filter_pattern,
        filter_self=filter_self,
        filter_incl=filter_incl,
        filter_total_us=filter_total_us,
        symbolicated=symbolicated,
        resolved_addrs=resolved_addrs,
        attempted_addrs=attempted_addrs,
        notes=notes,
    )
