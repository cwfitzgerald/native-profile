"""Symbolication via llvm-symbolizer (PDB on Windows, DWARF/dSYM elsewhere).

samply saves traces with raw module-relative addresses and does NOT bake in symbols
(even its --unstable-presymbolicate leaves Rust exes as `fun_<rva>` placeholders).
llvm-symbolizer + the debug info that the profile's `libs[].path` points at resolves
them fully, including inlined frames and file:line.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class Symbolizer:
    """Resolves module-relative addresses to (inlined) function-name chains."""

    def __init__(self, exe: str):
        self.exe = exe

    @classmethod
    def find(cls, override: str | None = None) -> "Symbolizer | None":
        cands: list[str] = []
        if override:
            cands.append(override)
        env = os.environ.get("LLVM_SYMBOLIZER")
        if env:
            cands.append(env)
        for name in ("llvm-symbolizer", "llvm-symbolizer-19", "llvm-symbolizer-18",
                     "llvm-symbolizer-17", "llvm-symbolizer-16", "llvm-symbolizer-15"):
            w = shutil.which(name)
            if w:
                cands.append(w)
        if os.name == "nt":
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            cands.append(str(Path(pf) / "LLVM" / "bin" / "llvm-symbolizer.exe"))

        seen = set()
        for c in cands:
            if not c or c in seen:
                continue
            seen.add(c)
            path = c if os.path.isfile(c) else shutil.which(c)
            if not path:
                continue
            try:
                subprocess.run([path, "--version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True)
            except Exception:
                continue
            return cls(path)
        return None

    def symbolize(self, obj_path: str, addrs: list[int]) -> dict[int, list[str]]:
        """Return {rva: [innermost_inline_name, ..., outermost]} for the given module.

        Unknown addresses map to an empty list. Addresses are treated as module-relative
        (RVAs), matching the Firefox profile's frameTable.address.
        """
        if not addrs:
            return {}
        stdin = "".join(f"0x{a:x}\n" for a in addrs)
        args = [
            self.exe,
            f"--obj={obj_path}",
            "--relative-address",
            "--output-style=JSON",
            "--functions=linkage",
            "--demangle",
            "--inlines",
        ]
        try:
            proc = subprocess.run(args, input=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        except Exception as e:  # pragma: no cover - environmental
            print(f"[native-profile] llvm-symbolizer failed for {obj_path}: {e}", file=sys.stderr)
            return {}
        out: dict[int, list[str]] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                addr = int(o["Address"], 16)
            except (KeyError, ValueError):
                continue
            names: list[str] = []
            for sym in o.get("Symbol", []):
                fn = (sym.get("FunctionName") or "").strip()
                if fn and fn != "??":
                    names.append(fn)
            out[addr] = names
        return out
