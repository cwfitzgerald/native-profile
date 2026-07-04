"""Cargo integration: ensure the `profiling` profile exists, build, and locate the binary."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit


class CargoError(RuntimeError):
    pass


def find_cargo(override: str | None = None) -> str:
    cand = override or shutil.which("cargo")
    if not cand:
        raise CargoError(
            "`cargo` not found on PATH. Install Rust from https://rustup.rs or pass --cargo."
        )
    return cand


def _strip_is_stripping(value) -> bool:
    # `strip` may be a bool or one of "none"/"debuginfo"/"symbols".
    if value is True:
        return True
    if isinstance(value, str) and value.lower() in ("debuginfo", "symbols"):
        return True
    return False


def ensure_profiling_profile(
    manifest_path: Path, profile: str = "profiling"
) -> tuple[bool, list[str]]:
    """Ensure `[profile.<profile>]` exists with debug info that is not stripped.

    Returns (changed, notes). Preserves existing formatting/comments via tomlkit.
    Only touches the target profile table; never rewrites unrelated content.
    """
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        raise CargoError(f"Cargo.toml not found at {manifest_path}")

    text = manifest_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)
    notes: list[str] = []
    changed = False

    profiles = doc.get("profile")
    prof = profiles.get(profile) if profiles is not None else None

    if prof is None:
        # Create a fresh profile. Custom profiles require `inherits`.
        table = tomlkit.table()
        table["inherits"] = "release"
        table["debug"] = True
        # release commonly strips; be explicit so symbols survive.
        table["strip"] = False
        if profiles is None:
            doc["profile"] = tomlkit.table(is_super_table=True)
            profiles = doc["profile"]
        profiles[profile] = table
        changed = True
        notes.append(
            f"added [profile.{profile}] (inherits=\"release\", debug=true, strip=false)"
        )
    else:
        dbg = prof.get("debug")
        if not (dbg is True or (isinstance(dbg, int) and dbg >= 1) or dbg in ("full", "line-tables-only", "limited")):
            prof["debug"] = True
            changed = True
            notes.append(f"set [profile.{profile}].debug = true (was {dbg!r})")
        if _strip_is_stripping(prof.get("strip")):
            prof["strip"] = False
            changed = True
            notes.append(
                f"set [profile.{profile}].strip = false (was {prof.get('strip')!r}; stripping removes symbols)"
            )
        if not notes:
            notes.append(f"[profile.{profile}] already has debug info; no change needed")

    if changed:
        manifest_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return changed, notes


@dataclass
class BuildSelector:
    example: str | None = None
    bin: str | None = None
    package: str | None = None
    features: str | None = None
    no_default_features: bool = False
    extra: list[str] = field(default_factory=list)

    def to_cargo_args(self) -> list[str]:
        args: list[str] = []
        if self.example:
            args += ["--example", self.example]
        if self.bin:
            args += ["--bin", self.bin]
        if self.package:
            args += ["--package", self.package]
        if self.features:
            args += ["--features", self.features]
        if self.no_default_features:
            args += ["--no-default-features"]
        args += self.extra
        return args

    def matches(self, target_names: list[str], kinds: list[str]) -> bool:
        want = self.example or self.bin
        if want is None:
            return True
        if want not in target_names:
            return False
        if self.example and "example" not in kinds:
            return False
        if self.bin and "bin" not in kinds:
            return False
        return True


def build_and_locate(
    cargo: str,
    manifest_path: Path,
    selector: BuildSelector,
    profile: str = "profiling",
) -> Path:
    """Run `cargo build` for the given profile/selector and return the built executable path."""
    args = [
        cargo,
        "build",
        "--profile",
        profile,
        "--manifest-path",
        str(manifest_path),
        *selector.to_cargo_args(),
        "--message-format=json-render-diagnostics",
    ]
    print(f"[native-profile] building: {' '.join(args)}", file=sys.stderr)
    proc = subprocess.run(args, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise CargoError(f"cargo build failed (exit {proc.returncode}); see diagnostics above.")

    executables: list[Path] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-artifact":
            continue
        exe = msg.get("executable")
        if not exe:
            continue
        target = msg.get("target", {})
        names = [target.get("name", "")]
        kinds = target.get("kind", [])
        if selector.matches(names, kinds):
            executables.append(Path(exe))

    if not executables:
        raise CargoError(
            "cargo produced no matching executable. Check your --example/--bin/--package selector."
        )
    # The last matching artifact is the freshly linked target.
    return executables[-1]
