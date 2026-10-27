#!/usr/bin/env python3
"""Build the pinned ``lmfpd`` wheel from the locked upstream source and verify it.

The SDK is not published on PyPI (the project is named differently there), so every release
builds the wheel from the pinned commit and then proves that the wheel came from that commit:
each module's bytes are compared with the SHA-256 in ``lm-detector_lock.json``. That check is
what makes "install lmfpd from the wheel" a controlled option instead of a supply-chain bet.

Usage (CI and local)::

    python scripts/build_sdk_wheel.py --wheelhouse build/wheels

The wheelhouse receives ``lmfpd-<version>-py3-none-any.whl`` plus ``lm-detector-wheel.json``
with its size and SHA-256, which the deploy log and the release assets record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare_fingerprint as prep  # noqa: E402 - scripts/ is a flat tool directory

HATCHLING_PIN = "hatchling==1.27.0"
SDK_SOURCE_PREFIX = "sdk/python/src/lmfpd/"

# pip and the backend venv get a writable scratch directory inside --build-dir: some
# hosts (CI containers, locked-down Windows profiles) have no usable system temp dir.
EXTRA_ENV: dict[str, str] = {}


class BuildError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merged_env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in extra.items():
        if os.name == "nt":
            for name in [name for name in env if name.upper() == key.upper()]:
                del env[name]
        env[key] = value
    return env


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    merged = merged_env(EXTRA_ENV)
    if env:
        merged.update(env)
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
    )
    if proc.returncode != 0:
        tail = "\n".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-15:])
        raise BuildError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")
    return proc.stdout


def pick_python(explicit: str | None) -> str:
    if explicit and explicit != "auto":
        return explicit
    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return found
    raise BuildError("no python interpreter found on PATH")


def sdk_entries(lock: dict) -> list[dict]:
    entries = [entry for entry in lock["files"] if entry["path"].startswith(SDK_SOURCE_PREFIX)]
    if not entries:
        raise BuildError("the lock records no sdk/python/src/lmfpd files; run 'manifest' first")
    return entries


def verify_source(lock: dict, repo_root: Path) -> None:
    """Verify metadata, build inputs, modules and reference data before executing a backend."""
    problems = prep.verify_tree(lock, repo_root, require_gitlink=prep.is_git_worktree(repo_root))
    if problems:
        raise BuildError("source tree is not the pinned commit: " + "; ".join(problems))
    log(f"source tree matches the lock ({len(lock['files'])} files, including build metadata)")


def build_wheel(python: str, source: Path, wheelhouse: Path, build_dir: Path) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    for stale in wheelhouse.glob("lmfpd-*.whl"):
        stale.unlink()
    venv = build_dir / "backend-venv"
    if venv.exists():
        shutil.rmtree(venv)
    build_dir.mkdir(parents=True, exist_ok=True)
    log(f"creating build venv with {python}")
    run([python, "-m", "venv", str(venv)])
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    venv_python = bindir / ("python.exe" if os.name == "nt" else "python")
    run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q", HATCHLING_PIN])
    log(f"building lmfpd wheel from {source} (backend {HATCHLING_PIN}, no build isolation)")
    run(
        [
            str(venv_python),
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-input",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(source),
        ]
    )
    wheels = sorted(wheelhouse.glob("lmfpd-*.whl"))
    if len(wheels) != 1:
        raise BuildError(f"expected exactly one lmfpd wheel, found {[wheel.name for wheel in wheels]}")
    return wheels[0]


def verify_wheel(wheel: Path, lock: dict) -> None:
    """Prove the wheel's modules are the locked bytes and its metadata is the locked version."""
    version = lock["sdk"]["version"]
    if not wheel.name.startswith(f"lmfpd-{version}-"):
        raise BuildError(f"{wheel.name} does not carry the locked version {version}")
    expected = {entry["path"][len(SDK_SOURCE_PREFIX):]: entry for entry in sdk_entries(lock)}
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = [name for name in expected if f"lmfpd/{name}" not in names]
        if missing:
            raise BuildError(f"wheel is missing locked modules: {missing}")
        for name, entry in expected.items():
            body = archive.read(f"lmfpd/{name}")
            digest = hashlib.sha256(body).hexdigest()
            if digest != entry["sha256"]:
                raise BuildError(f"lmfpd/{name} in the wheel does not match the lock")
        extra = sorted(
            name
            for name in names
            if name.startswith("lmfpd/") and name[len("lmfpd/"):] not in expected
        )
        if extra:
            raise BuildError(f"wheel ships modules outside the lock: {extra}")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise BuildError(f"wheel has {len(metadata)} METADATA entries")
        text = archive.read(metadata[0]).decode("utf-8", "replace")
    for requirement in (f"Version: {version}", "Requires-Python: >=3.10", "Requires-Dist: httpx", "Requires-Dist: numpy", "Requires-Dist: zstandard"):
        if requirement not in text:
            raise BuildError(f"wheel metadata is missing {requirement!r}")
    log(f"wheel verified: {len(expected)} modules and metadata match the lock")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", help="portal checkout (default: the parent of scripts/)")
    parser.add_argument("--wheelhouse", required=True, help="directory that receives the wheel and its manifest")
    parser.add_argument("--build-dir", help="scratch directory for the backend venv (default: temporary)")
    parser.add_argument("--python", default="auto", help="interpreter used to create the build venv")
    parser.add_argument("--skip-source-check", action="store_true", help="accept a source tree that differs from the lock (debug only)")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else prep.repo_root_default()
    lock = prep.load_lock(prep.script_dir() / prep.LOCK_NAME)
    wheelhouse = Path(args.wheelhouse).resolve()
    created = None
    if args.build_dir:
        build_dir = Path(args.build_dir).resolve()
    else:
        try:
            build_dir = Path(tempfile.mkdtemp(prefix="lmfpd-build-"))
            created = build_dir
        except OSError as exc:
            print(f"error: no usable temporary directory ({exc}); pass --build-dir", file=sys.stderr)
            return 1
    build_dir.mkdir(parents=True, exist_ok=True)
    scratch = build_dir / "tmp"
    scratch.mkdir(exist_ok=True)
    EXTRA_ENV.update({"TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch)})
    try:
        if not args.skip_source_check:
            verify_source(lock, repo_root)
        source = prep.superproject(lock, repo_root) / lock["sdk"]["source_subdir"]
        wheel = build_wheel(pick_python(args.python), source, wheelhouse, build_dir)
        verify_wheel(wheel, lock)
        record = {
            "filename": wheel.name,
            "sha256": sha256_file(wheel),
            "size": wheel.stat().st_size,
            "sdk_version": lock["sdk"]["version"],
            "upstream_commit": lock["upstream"]["commit"],
            "upstream_repository": lock["upstream"]["repository"],
            "backend": HATCHLING_PIN,
            "built_at": utc_now(),
        }
        (wheelhouse / "lm-detector-wheel.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log(f"wheel {wheel.name} sha256={record['sha256']} size={record['size']}")
        return 0
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
