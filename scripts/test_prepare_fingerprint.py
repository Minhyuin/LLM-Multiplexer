#!/usr/bin/env python3
"""Regression tests for the manifest produced by ``scripts/prepare_fingerprint.py``.

The lock hashes must describe *committed* bytes.  A Windows checkout with
``core.autocrlf=true`` rewrites every text file to CRLF in the worktree, so any
manifest built from the worktree disagrees with the LF bytes that
``raw.githubusercontent.com`` (bootstrap) and a Linux sparse checkout
(checkout) materialise.  These tests build a throw-away git repository that
reproduces exactly that shape and assert the lock still records LF hashes.

Everything runs offline against a synthetic local repository.

Usage::

    python scripts/test_prepare_fingerprint.py
    python scripts/test_prepare_fingerprint.py -v
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
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import prepare_fingerprint as prep  # noqa: E402  (path set up above)

SUBMODULE_PATH = "third_party/lm-detector"

# Committed content.  Written as bytes so no platform newline translation can
# sneak into the fixture: these are the canonical LF bytes the lock must hash.
TEXT_FILES: dict[str, bytes] = {
    "LICENSE": b"MIT License\n\nCopyright (c) 2026 lm-detector contributors\n",
    "sdk/python/pyproject.toml": b'[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n',
    "sdk/python/README.md": b"# lmfpd\n\nSmall SDK.\n\n- one\n- two\n",
    "sdk/python/src/lmfpd/__init__.py": b'"""lmfpd."""\n\n__version__ = "0.2.0"\n',
    "sdk/python/src/lmfpd/_types.py": b"from dataclasses import dataclass\n\n\n@dataclass\nclass Probe:\n    value: float\n",
    "sdk/python/src/lmfpd/py.typed": b"",
    "data/unified_bank.json": b'{\n  "version": 1,\n  "bank": [\n    1,\n    2,\n    3\n  ]\n}\n',
}

# Deliberately binary: NUL bytes make git treat the file as binary, so no
# end-of-line filter ever touches it.  The embedded CRLF pairs must survive
# byte for byte.
BINARY_PATH = "data/shared_detector.bin"
BINARY_FILE = b"\x00\x01\x02\r\n\xff\xfe\x00\r\n\r\npayload\r\n"

ALL_FILES: dict[str, bytes] = {**TEXT_FILES, BINARY_PATH: BINARY_FILE}

# Never part of the manifest: untracked scratch files and build caches that a
# worktree glob would happily pick up.
UNTRACKED = {
    "sdk/python/src/lmfpd/scratch.py": b"# local experiment\n",
    "sdk/python/src/lmfpd/__pycache__/junk.cpython-311.pyc": b"\x00cache\x00",
    "notes.md": b"# not whitelisted\n",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ManifestFixture(unittest.TestCase):
    """Builds a portal checkout with a pinned submodule and a CRLF worktree."""

    def setUp(self) -> None:
        if shutil.which("git") is None:  # pragma: no cover - git is a hard requirement
            self.skipTest("git is not installed")

        self.tmp = Path(tempfile.mkdtemp(prefix="prepare-fingerprint-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Isolate from the developer's global/system git config: the fixture
        # sets the filters it wants explicitly, per repository.
        self.env = dict(os.environ)
        self.env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig-global"),
                "GIT_AUTHOR_NAME": "Fingerprint Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Fingerprint Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "file",
            }
        )
        # prepare_fingerprint.git() spawns git with the inherited environment,
        # so patch os.environ for the whole test to keep it isolated too.
        env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.portal = self.tmp / "portal"
        self.scripts = self.portal / "scripts"
        self.scripts.mkdir(parents=True)
        (self.portal / "manage.py").write_bytes(b'#!/usr/bin/env python3\nprint("portal")\n')
        self.lock_path = self.scripts / prep.LOCK_NAME

        self.sub = self.portal / SUBMODULE_PATH
        self.commit = self._build_submodule()
        self._write_skeleton_lock()

        # The lock lives beside the script, not under --repo-root; redirect it
        # into the fixture so the real scripts/lm-detector_lock.json is never
        # read or written by a test run.
        script_dir_patch = mock.patch.object(prep, "script_dir", lambda: self.scripts)
        script_dir_patch.start()
        self.addCleanup(script_dir_patch.stop)

        self._materialise_crlf_worktree()

    # -- fixture helpers ---------------------------------------------------

    def _build_submodule(self) -> str:
        self.sub.mkdir(parents=True)
        for relpath, body in ALL_FILES.items():
            target = self.sub / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        self.git(self.sub, "init", "-q")
        self.git(self.sub, "add", "-A")
        self.git(self.sub, "commit", "-q", "-m", "pin lm-detector")
        return self.git(self.sub, "rev-parse", "HEAD")

    def _write_skeleton_lock(self) -> None:
        skeleton = {
            "schema": prep.LOCK_SCHEMA,
            "portal_version": "1.9.2",
            "submodule_path": SUBMODULE_PATH,
            "data_dir": f"{SUBMODULE_PATH}/data",
            "upstream": {
                "repository": "https://example.invalid/lm-detector.git",
                "commit": self.commit,
                "tag": None,
                "license": "MIT",
            },
            "sdk": {
                "distribution": "lmfpd",
                "version": "0.2.0",
                "source_subdir": "sdk/python",
                "requires_python": ">=3.10",
            },
            "files": [],
            "sparse_patterns": [],
            "generated_by": "scripts/test_prepare_fingerprint.py",
            "generated_at": "2026-01-01T00:00:00Z",
        }
        self.lock_path.write_bytes(json.dumps(skeleton, indent=2).encode("utf-8"))

    def _materialise_crlf_worktree(self) -> None:
        """Reproduce a Windows checkout: LF in the object database, CRLF on disk."""
        self.git(self.sub, "config", "core.autocrlf", "true")
        for relpath in TEXT_FILES:
            (self.sub / relpath).unlink()
        self.git(self.sub, "checkout", "-f", "--", ".")

    def git(self, cwd: Path, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )
        if check and proc.returncode:
            self.fail(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def write_manifest(self) -> dict:
        args = argparse.Namespace(repo_root=str(self.portal), timestamp=False)
        self.assertEqual(prep.cmd_manifest(args), 0)
        return json.loads(self.lock_path.read_bytes().decode("utf-8"))

    def entries(self, lock: dict) -> dict[str, dict]:
        return {entry["path"]: entry for entry in lock["files"]}

    # -- tests -------------------------------------------------------------

    def test_fixture_worktree_really_is_crlf(self) -> None:
        """Guard: without this the CRLF assertions below would prove nothing."""
        on_disk = (self.sub / "LICENSE").read_bytes()
        self.assertIn(b"\r\n", on_disk)
        self.assertEqual(on_disk, TEXT_FILES["LICENSE"].replace(b"\n", b"\r\n"))
        # The object database still holds canonical LF bytes.
        blob = subprocess.run(
            ["git", "-C", str(self.sub), "cat-file", "blob", f"{self.commit}:LICENSE"],
            capture_output=True,
            env=self.env,
        ).stdout
        self.assertEqual(blob, TEXT_FILES["LICENSE"])

    def test_manifest_hashes_committed_lf_bytes_not_the_crlf_worktree(self) -> None:
        lock = self.write_manifest()
        entries = self.entries(lock)
        for relpath, body in TEXT_FILES.items():
            self.assertIn(relpath, entries)
            self.assertEqual(entries[relpath]["size"], len(body), relpath)
            self.assertEqual(entries[relpath]["sha256"], sha256(body), relpath)
            if b"\n" in body:  # an empty file is filter-proof, nothing to compare
                crlf = body.replace(b"\n", b"\r\n")
                self.assertNotEqual(entries[relpath]["sha256"], sha256(crlf), relpath)

    def test_manifest_ignores_dirty_worktree_edits(self) -> None:
        dirty = self.sub / "data/unified_bank.json"
        dirty.write_bytes(dirty.read_bytes() + b'\n// local edit, never committed\n')
        self.assertNotEqual(dirty.read_bytes(), TEXT_FILES["data/unified_bank.json"])

        entries = self.entries(self.write_manifest())

        self.assertEqual(entries["data/unified_bank.json"]["sha256"],
                         sha256(TEXT_FILES["data/unified_bank.json"]))
        self.assertNotEqual(entries["data/unified_bank.json"]["sha256"],
                            sha256(dirty.read_bytes()))

    def test_manifest_excludes_untracked_files_and_caches(self) -> None:
        for relpath, body in UNTRACKED.items():
            target = self.sub / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

        lock = self.write_manifest()
        paths = set(self.entries(lock))

        self.assertEqual(paths, set(ALL_FILES))
        for relpath in UNTRACKED:
            self.assertNotIn(relpath, paths)
        self.assertFalse([p for p in paths if "__pycache__" in p])

        # Every entry is backed by an explicit sparse pattern (cone mode off),
        # in manifest order rather than sorted order.
        self.assertEqual(lock["sparse_patterns"],
                         ["/" + entry["path"] for entry in lock["files"]])

    def test_manifest_records_the_binary_blob_byte_exact(self) -> None:
        on_disk = (self.sub / BINARY_PATH).read_bytes()
        # git must not run an end-of-line filter over binary content.
        self.assertEqual(on_disk, BINARY_FILE)

        entries = self.entries(self.write_manifest())

        self.assertEqual(entries[BINARY_PATH]["size"], len(BINARY_FILE))
        self.assertEqual(entries[BINARY_PATH]["sha256"], sha256(BINARY_FILE))

    def test_manifest_uses_committed_paths_even_when_absent_from_the_worktree(self) -> None:
        (self.sub / BINARY_PATH).unlink()

        entries = self.entries(self.write_manifest())

        self.assertEqual(entries[BINARY_PATH]["sha256"], sha256(BINARY_FILE))

    def test_manifest_matches_the_object_database_for_every_entry(self) -> None:
        entries = self.entries(self.write_manifest())
        for relpath, entry in entries.items():
            blob = subprocess.run(
                ["git", "-C", str(self.sub), "cat-file", "blob", f"{self.commit}:{relpath}"],
                capture_output=True,
                env=self.env,
            )
            self.assertEqual(blob.returncode, 0, relpath)
            self.assertEqual(entry["size"], len(blob.stdout), relpath)
            self.assertEqual(entry["sha256"], sha256(blob.stdout), relpath)

    def test_apply_sparse_pins_no_end_of_line_conversion(self) -> None:
        self.git(self.sub, "config", "core.autocrlf", "true")
        patterns = ["/" + relpath for relpath in ALL_FILES]

        prep.apply_sparse(self.sub, patterns)

        self.assertEqual(self.git(self.sub, "config", "--get", "core.autocrlf"), "false")
        self.assertEqual(self.git(self.sub, "config", "--get", "core.eol"), "lf")
        sparse = self.git(self.sub, "rev-parse", "--absolute-git-dir")
        patterns_file = Path(sparse) / "info" / "sparse-checkout"
        self.assertEqual(patterns_file.read_text(encoding="utf-8").split(),
                         [p for p in patterns])

    def test_bounded_checkout_materialises_lf_bytes(self) -> None:
        origin = self.tmp / "origin.git"
        proc = subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.sub), str(origin)],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        lock = self.write_manifest()
        lock["upstream"]["repository"] = origin.as_uri()
        fresh = self.portal / "third_party" / "lm-detector-fresh"

        prep.bounded_git_checkout(fresh, lock)

        self.assertEqual(self.git(fresh, "config", "--get", "core.autocrlf"), "false")
        self.assertEqual(self.git(fresh, "config", "--get", "core.eol"), "lf")
        self.assertEqual(self.git(fresh, "rev-parse", "HEAD"), self.commit)
        for relpath, body in TEXT_FILES.items():
            self.assertEqual((fresh / relpath).read_bytes(), body, relpath)
        self.assertEqual((fresh / BINARY_PATH).read_bytes(), BINARY_FILE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
