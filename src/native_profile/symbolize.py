"""Symbolication via llvm-symbolizer (PDB on Windows, DWARF/dSYM elsewhere).

samply saves traces with raw module-relative addresses (RVAs) and does NOT bake in
symbols (even its --unstable-presymbolicate leaves Rust exes as `fun_<rva>`
placeholders). llvm-symbolizer + the debug info that the profile's `libs[].path`
points at resolves them fully, including inlined frames and file:line.

`--relative-address` tells llvm-symbolizer to treat the input as relative to the
image's load address. For ELF (non-zero first PT_LOAD vaddr) and PE (ImageBase) that
lines up with samply's RVAs directly. For Mach-O it does not: `--relative-address`
is a no-op there (verified empirically), and the address that actually resolves is
`rva + __TEXT.vmaddr` treated as absolute — vmaddr is conventionally 0x100000000 for
64-bit executables but is not guaranteed, so it is read from the binary's load
commands rather than hardcoded.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_MH_MAGIC_64 = 0xFEEDFACF
_MH_MAGIC_32 = 0xFEEDFACE
_FAT_MAGIC = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_LC_SEGMENT = 0x1
_LC_SEGMENT_64 = 0x19


def _macho_text_vmaddr_at(data: bytes, base: int) -> int | None:
    """Parse one Mach-O thin slice's load commands for __TEXT's vmaddr."""
    if base + 4 > len(data):
        return None
    (magic,) = struct.unpack_from("<I", data, base)
    if magic == _MH_MAGIC_64:
        # mach_header_64: magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved
        _, _, _, _, ncmds, _, _, _ = struct.unpack_from("<8I", data, base)
        off = base + 32
    elif magic == _MH_MAGIC_32:
        _, _, _, _, ncmds, _, _ = struct.unpack_from("<7I", data, base)
        off = base + 28
    else:
        return None
    for _ in range(ncmds):
        if off + 8 > len(data):
            return None
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == _LC_SEGMENT_64:
            segname = data[off + 8:off + 24].rstrip(b"\x00")
            if segname == b"__TEXT":
                (vmaddr,) = struct.unpack_from("<Q", data, off + 24)
                return vmaddr
        elif cmd == _LC_SEGMENT:
            segname = data[off + 8:off + 24].rstrip(b"\x00")
            if segname == b"__TEXT":
                (vmaddr,) = struct.unpack_from("<I", data, off + 24)
                return vmaddr
        if cmdsize == 0:
            return None
        off += cmdsize
    return None


def _macho_text_vmaddr(path: str) -> int | None:
    """Return the `__TEXT` segment's `vmaddr` for a Mach-O file, or None if not Mach-O.

    Handles both thin binaries and fat (universal) archives, parsed directly from the
    load commands (no `otool`/new dependency required). For a fat binary, the first
    slice that parses successfully wins -- 64-bit executables conventionally share the
    same `__TEXT` vmaddr across architectures.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(4)
            if len(data) < 4:
                return None
            (magic,) = struct.unpack("<I", data)
            if magic in (_MH_MAGIC_64, _MH_MAGIC_32):
                f.seek(0)
                full = f.read()
                return _macho_text_vmaddr_at(full, 0)
            if magic in (_FAT_MAGIC, _FAT_MAGIC_64):
                f.seek(0)
                full = f.read()
                # fat_header + fat_arch(_64) are big-endian regardless of host.
                (_, nfat_arch) = struct.unpack_from(">2I", full, 0)
                is64 = magic == _FAT_MAGIC_64
                arch_size = 32 if is64 else 20
                arch_off = 8
                for i in range(nfat_arch):
                    o = arch_off + i * arch_size
                    if is64:
                        _, _, offset, _, _, _ = struct.unpack_from(">2i2Q2I", full, o)
                    else:
                        _, _, offset, _, _ = struct.unpack_from(">2i3I", full, o)
                    vmaddr = _macho_text_vmaddr_at(full, offset)
                    if vmaddr is not None:
                        return vmaddr
                return None
            return None
    except OSError:
        return None


@lru_cache(maxsize=None)
def _load_bias(path: str) -> int:
    """Address to add to a samply RVA before handing it to llvm-symbolizer.

    0 for ELF/PE (where `--relative-address` already does the right thing against
    samply's RVAs); the `__TEXT` vmaddr for Mach-O.
    """
    vmaddr = _macho_text_vmaddr(path)
    return vmaddr or 0


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

        Unknown addresses map to an empty list. `addrs` are module-relative (RVAs),
        matching the Firefox profile's frameTable.address; each is biased by
        `_load_bias(obj_path)` before being handed to llvm-symbolizer (see module
        docstring) and the response is translated back to the original RVA.
        """
        if not addrs:
            return {}
        bias = _load_bias(obj_path)
        stdin = "".join(f"0x{a + bias:x}\n" for a in addrs)
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
            out[addr - bias] = names
        return out
