"""Render an Analysis to compact JSON and a Markdown report."""
from __future__ import annotations

import json

from .analyze import Analysis, Row


def _s(us: float) -> float:
    return round(us / 1e6, 4)


def _row_json(r: Row) -> dict:
    d = {"name": r.name, "self_s": _s(r.us), "pct": round(r.pct, 2)}
    if r.crate:
        d["crate"] = r.crate
    if r.module:
        d["module"] = r.module
    return d


def to_json(a: Analysis) -> dict:
    return {
        "process": a.process,
        "total_cpu_s": _s(a.total_us),
        "weight_mode": a.weight_mode,
        "symbolicated": a.symbolicated,
        "resolved_addrs": a.resolved_addrs,
        "attempted_addrs": a.attempted_addrs,
        "notes": a.notes,
        "threads": [{"name": n, "cpu_s": _s(us)} for n, us in a.threads[:20]],
        "self_by_function": [_row_json(r) for r in a.self_rows],
        "inclusive_by_function": [_row_json(r) for r in a.incl_rows],
        "by_crate": [{"crate": r.name, "self_s": _s(r.us), "pct": round(r.pct, 2)} for r in a.by_crate],
        "by_module": [{"module": r.name, "self_s": _s(r.us), "pct": round(r.pct, 2)} for r in a.by_module],
        "callers": {
            fn: [{"caller": c, "self_s": _s(us)} for c, us in cs]
            for fn, cs in a.callers.items()
        },
        "filter": None if a.filter_pattern is None else {
            "pattern": a.filter_pattern,
            "matched_self_total_s": _s(a.filter_total_us),
            "matched_self_pct": round(100 * a.filter_total_us / (a.total_us or 1), 2),
            "self_by_function": [_row_json(r) for r in a.filter_self],
            "inclusive_by_function": [_row_json(r) for r in a.filter_incl],
        },
    }


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _fn_rows(rows: list[Row]) -> list[list[str]]:
    return [[f"{_s(r.us):.3f}", f"{r.pct:.1f}%", f"`{r.name[:96]}`"] for r in rows]


def to_markdown(a: Analysis) -> str:
    out: list[str] = []
    out.append(f"# Profile digest: {a.process}\n")
    out.append(f"- **Total CPU** (all threads): **{_s(a.total_us):.2f} s**")
    out.append(f"- **Weight**: {a.weight_mode}")
    resolve_detail = ""
    if a.attempted_addrs:
        rate = 100 * a.resolved_addrs / a.attempted_addrs
        resolve_detail = f" ({a.resolved_addrs}/{a.attempted_addrs} addresses, {rate:.1f}%)"
    out.append(f"- **Symbolicated**: {'yes' if a.symbolicated else 'NO (module-level only)'}{resolve_detail}")
    if a.notes:
        for n in a.notes:
            out.append(f"- ⚠️ {n}")
    out.append("")

    if a.filter_pattern is not None:
        pct = 100 * a.filter_total_us / (a.total_us or 1)
        out.append(f"## Filter `{a.filter_pattern}`\n")
        out.append(f"Matched functions account for **{_s(a.filter_total_us):.3f} s "
                   f"({pct:.1f}%)** of total CPU (self time).\n")
        if a.filter_self:
            out.append("**Self time (matched):**\n")
            out.append(_table(["self (s)", "%", "function"], _fn_rows(a.filter_self)))
            out.append("")
        if a.filter_incl:
            out.append("**Inclusive time (matched):**\n")
            out.append(_table(["incl (s)", "%", "function"], _fn_rows(a.filter_incl)))
            out.append("")

    out.append("## Top self-time functions\n")
    out.append(_table(["self (s)", "%", "function"], _fn_rows(a.self_rows)))
    out.append("")

    out.append("## Top inclusive-time functions\n")
    out.append(_table(["incl (s)", "%", "function"], _fn_rows(a.incl_rows)))
    out.append("")

    out.append("## Self time by crate\n")
    out.append(_table(["self (s)", "%", "crate"],
                      [[f"{_s(r.us):.3f}", f"{r.pct:.1f}%", f"`{r.name}`"] for r in a.by_crate]))
    out.append("")

    out.append("## Self time by module\n")
    out.append(_table(["self (s)", "%", "module"],
                      [[f"{_s(r.us):.3f}", f"{r.pct:.1f}%", f"`{r.name}`"] for r in a.by_module]))
    out.append("")

    out.append("## Threads by CPU\n")
    out.append(_table(["cpu (s)", "thread"],
                      [[f"{_s(us):.3f}", f"`{n}`"] for n, us in a.threads[:15]]))
    out.append("")

    out.append("## Callers of hot / matched functions\n")
    out.append("_Immediate caller of each function's self-time samples._\n")
    for fn, cs in a.callers.items():
        if not cs:
            continue
        out.append(f"**`{fn[:96]}`**")
        for c, us in cs:
            out.append(f"- {_s(us):.3f}s ← `{c[:90]}`")
        out.append("")

    return "\n".join(out)
