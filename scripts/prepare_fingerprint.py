#!/usr/bin/env python3
"""Prepare the pinned lm-detector dependency for the v1.9.2 fingerprint subsystem.

The upstream repository is roughly 1.9 GB, almost all of it research material that the
runtime never reads. This script is the single auditable entry point for materialising it
and it touches nothing that is not named in ``lm-detector_lock.json``: the lock lists every
file the feature needs, with its byte size and SHA-256, plus the exact upstream commit.

Subcommands::

    manifest    regenerate the lock's file list from a checked-out tree at the pinned commit
    checkout    materialise the pin: sparse git checkout in a git worktree, otherwise the
                whitelist-only HTTPS bootstrap (deploy tarballs have no .git)
    bootstrap   force the whitelist-only HTTPS bootstrap (no git required at all)
    verify      fail closed unless every locked file matches and nothing extra is present

Every mode is idempotent and offline-safe once the files exist. Exit code 0 means the tree
matches the lock; any doubt (missing file, size or hash mismatch, unexpected extra file,
gitlink pointing elsewhere) is exit code 1 with a one-line reason on stderr.

Workflow for an upstream version bump: edit ``upstream.commit`` (and ``sdk.version``) in the
lock, run ``checkout``, run ``manifest`` to refresh sizes, hashes and sparse patterns, then
``verify``. Rebuilding the SDK wheel and installing it is the job of ``build_sdk_wheel.py``.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LOCK_SCHEMA = "lm-detector-lock/1"
LOCK_NAME = "lm-detector_lock.json"
LOCK_PATHS = ("submodule_path", "data_dir", "portal_version", "upstream", "sdk", "files", "sparse_patterns")

# What the runtime actually needs. ``manifest`` expands this list into the lock's file
# entries; ``checkout``/``bootstrap`` only ever write these paths.
MANIFEST_SPEC: tuple[tuple[str, str], ...] = (
    ("license", "LICENSE"),
    ("sdk", "sdk/python/pyproject.toml"),
    ("sdk", "sdk/python/README.md"),
    ("sdk", "sdk/python/src/lmfpd/**"),
    ("reference-data", "data/unified_bank.json"),
    ("reference-data", "data/shared_detector.bin"),
)

IGNORED_NAMES = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store"})
IGNORED_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".dist-info", ".part")

HTTP_TIMEOUT = 180.0
HTTP_ATTEMPTS = 3


class Failure(Exception):
    """A checked precondition failed; the message is the whole report."""


def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def run(cmd: list[str], cwd: Path | str | None = None, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.pop("GIT_ASKPASS", None)
    try:
        proc = subprocess.run(
            [str(part) for part in cmd],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        raise Failure(f"cannot run {cmd[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise Failure(f"command failed ({proc.returncode}): {' '.join(str(part) for part in cmd)}\n" + "\n".join(detail[-8:]))
    return proc


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(cwd), *args], check=check).stdout.strip()


def git_blob(cwd: Path, commit: str, relpath: str) -> bytes:
    """Read exact committed bytes; checkout newline conversions must not enter a lock."""
    proc = subprocess.run(["git", "-C", str(cwd), "cat-file", "blob", f"{commit}:{relpath}"],
                          capture_output=True)
    if proc.returncode:
        raise Failure(f"cannot read committed blob {commit}:{relpath}: "
                      + proc.stderr.decode("utf-8", errors="replace").strip())
    return proc.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_relpath(raw: str) -> str:
    """Reject anything that could escape the submodule directory."""
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw or not path.parts:
        raise Failure(f"lock contains an unsafe path: {raw!r}")
    return path.as_posix()


def load_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Failure(f"lock file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise Failure(f"lock file is not valid JSON ({path}): {exc}") from None
    if not isinstance(lock, dict) or lock.get("schema") != LOCK_SCHEMA:
        raise Failure(f"lock file has an unexpected schema: {path}")
    for key in LOCK_PATHS:
        if key not in lock:
            raise Failure(f"lock file is missing {key!r}: {path}")
    for entry in lock["files"]:
        check_relpath(entry["path"])
        if not isinstance(entry.get("size"), int) or not isinstance(entry.get("sha256"), str):
            raise Failure(f"lock entry for {entry['path']!r} needs an integer size and a sha256")
    return lock


def save_lock(path: Path, lock: dict) -> None:
    path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def repo_root_default() -> Path:
    return script_dir().parent


def lock_file(repo_root: Path) -> Path:
    return script_dir() / LOCK_NAME


def is_git_worktree(path: Path) -> bool:
    return (path / ".git").exists()


def superproject(lock: dict, repo_root: Path) -> Path:
    return repo_root / lock["submodule_path"]


def resolve_root(args: argparse.Namespace) -> Path:
    root = Path(args.repo_root).resolve() if args.repo_root else repo_root_default()
    if not (root / "manage.py").is_file():
        raise Failure(f"{root} does not look like the portal repository (no manage.py)")
    return root


def sparse_checkout_dir(sub: Path) -> Path:
    gitdir = Path(git(sub, "rev-parse", "--absolute-git-dir"))
    if not gitdir.is_absolute():
        gitdir = sub / gitdir
    return gitdir / "info" / "sparse-checkout"


def apply_sparse(sub: Path, patterns: list[str]) -> None:
    if not patterns:
        raise Failure("lock lists no sparse patterns; refusing to materialise an empty whitelist")
    target = sparse_checkout_dir(sub)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    # Hashes describe committed bytes, not platform-dependent checkout filters.
    git(sub, "config", "core.autocrlf", "false")
    git(sub, "config", "core.eol", "lf")
    git(sub, "config", "core.sparseCheckout", "true")
    git(sub, "config", "core.sparseCheckoutCone", "false")
    git(sub, "read-tree", "-mu", "HEAD")


def bounded_git_checkout(sub: Path, lock: dict) -> None:
    """Clone the pin without blobs outside the whitelist and without a full worktree."""
    commit = lock["upstream"]["commit"]
    repository = lock["upstream"]["repository"]
    sub.parent.mkdir(parents=True, exist_ok=True)
    if sub.exists() and any(sub.iterdir()):
        if not (sub / ".git").exists():
            raise Failure(f"{sub} exists but is not a git checkout; move it away and re-run")
    else:
        sub.mkdir(parents=True, exist_ok=True)
        git(sub, "init", "-q")
    patterns = list(lock["sparse_patterns"])
    # Hashes describe committed bytes, not platform-dependent checkout filters.
    git(sub, "config", "core.autocrlf", "false")
    git(sub, "config", "core.eol", "lf")
    git(sub, "config", "core.sparseCheckout", "true")
    git(sub, "config", "core.sparseCheckoutCone", "false")
    sparse_checkout_dir(sub).parent.mkdir(parents=True, exist_ok=True)
    sparse_checkout_dir(sub).write_text("\n".join(patterns) + "\n", encoding="utf-8")
    if git(sub, "remote", "get-url", "origin", check=False) != repository:
        if git(sub, "remote", "get-url", "origin", check=False):
            git(sub, "remote", "set-url", "origin", repository)
        else:
            git(sub, "remote", "add", "origin", repository)
    log(f"fetching {commit[:12]} from {repository} (blobless, depth 1)")
    git(sub, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit)
    git(sub, "checkout", "-q", "--detach", commit)
    git(sub, "read-tree", "-mu", "HEAD")
    head = git(sub, "rev-parse", "HEAD")
    if head != commit:
        raise Failure(f"submodule HEAD is {head}, expected {commit}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        raise Failure(f"unexpected redirect to {newurl} (refusing to follow)")


def _opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(_NoRedirect)
    opener.addheaders = [("User-Agent", "llm-multiplexer-lm-detector-bootstrap/1")]
    return opener


def raw_url(lock: dict, relpath: str) -> str:
    repository = lock["upstream"]["repository"]
    if "github.com/" not in repository:
        raise Failure(f"the HTTPS bootstrap needs a github.com repository URL, got {repository!r}")
    slug = repository.split("github.com/", 1)[1].removesuffix(".git")
    return f"https://raw.githubusercontent.com/{slug}/{lock['upstream']['commit']}/{relpath}"


def download(lock: dict, entry: dict, target: Path) -> None:
    """Fetch one locked file and only replace ``target`` once its size and hash match."""
    relpath = entry["path"]
    url = raw_url(lock, relpath)
    opener = _opener()
    last: Exception | None = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            with opener.open(url, timeout=HTTP_TIMEOUT) as response:
                if response.status != 200:
                    raise Failure(f"HTTP {response.status} for {relpath}")
                body = response.read()
        except (urllib.error.URLError, TimeoutError, OSError, Failure) as exc:
            last = exc
            if attempt < HTTP_ATTEMPTS:
                time.sleep(2 * attempt)
            continue
        if len(body) != entry["size"]:
            raise Failure(f"{relpath}: got {len(body)} bytes, lock says {entry['size']}")
        digest = sha256_bytes(body)
        if digest != entry["sha256"]:
            raise Failure(f"{relpath}: sha256 {digest} does not match the lock")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.part"
        temp.write_bytes(body)
        os.replace(temp, target)
        log(f"  fetched {relpath} ({entry['size']} bytes)")
        return
    raise Failure(f"could not download {relpath} after {HTTP_ATTEMPTS} attempts: {last}")


def bootstrap(lock: dict, repo_root: Path) -> int:
    """Write only the locked whitelist; needs neither git nor a full tarball."""
    sub = superproject(lock, repo_root)
    if (sub / ".git").exists():
        raise Failure(f"{sub} is a git checkout; run checkout/verify instead of bootstrap")
    sub.mkdir(parents=True, exist_ok=True)
    written = 0
    for entry in lock["files"]:
        target = sub / check_relpath(entry["path"])
        if target.is_file() and target.stat().st_size == entry["size"] and sha256_file(target) == entry["sha256"]:
            continue
        download(lock, entry, target)
        written += 1
    log(f"bootstrap complete: {written} file(s) written, {len(lock['files']) - written} already current")
    return written


def verify_tree(lock: dict, repo_root: Path, *, require_gitlink: bool) -> list[str]:
    """Return a list of problems; an empty list means the tree matches the lock exactly."""
    problems: list[str] = []
    sub = superproject(lock, repo_root)
    if not sub.is_dir():
        return [f"{lock['submodule_path']} is missing; run checkout"]
    expected = {entry["path"]: entry for entry in lock["files"]}
    for relpath, entry in sorted(expected.items()):
        path = sub / relpath
        if not path.is_file():
            problems.append(f"missing file: {relpath}")
            continue
        size = path.stat().st_size
        if size != entry["size"]:
            problems.append(f"{relpath}: {size} bytes, lock says {entry['size']}")
            continue
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            problems.append(f"{relpath}: sha256 {digest} does not match the lock")
    extras: list[str] = []
    for path in sorted(sub.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_NAMES for part in path.parts):
            continue
        if path.name.endswith(IGNORED_SUFFIXES) or any(part.endswith(".egg-info") for part in path.parts):
            continue
        relpath = path.relative_to(sub).as_posix()
        if relpath not in expected:
            extras.append(relpath)
    if extras:
        shown = "\n".join(f"  extra file: {relpath}" for relpath in extras[:25])
        tail = "" if len(extras) <= 25 else f"\n  ... and {len(extras) - 25} more"
        problems.append(f"{len(extras)} file(s) outside the locked whitelist in {lock['submodule_path']}:\n{shown}{tail}")
    commit = lock["upstream"]["commit"]
    if (sub / ".git").exists():
        head = git(sub, "rev-parse", "HEAD")
        if head != commit:
            problems.append(f"submodule HEAD {head} is not the pinned {commit}")
    if is_git_worktree(repo_root):
        problems.extend(check_gitmodules(lock, repo_root))
        listing = run(["git", "-C", str(repo_root), "ls-files", "-s", "--", lock["submodule_path"]], check=False)
        if listing.returncode != 0:
            problems.append(f"cannot read the git index: {(listing.stderr or '').strip()}")
        elif listing.stdout.strip():
            mode, sha, _stage, _path = listing.stdout.split(None, 3)
            if mode != "160000":
                problems.append(f"gitlink for {lock['submodule_path']} has mode {mode}, expected 160000")
            elif sha != commit:
                problems.append(f"gitlink points at {sha}, lock pins {commit}")
        elif require_gitlink:
            problems.append(
                f"no gitlink for {lock['submodule_path']} in the index; stage it with "
                f"'git add .gitmodules {lock['submodule_path']}'"
            )
    elif require_gitlink:
        problems.append("require-gitlink was requested but the repository is not a git worktree")
    return problems


def check_gitmodules(lock: dict, repo_root: Path) -> list[str]:
    path = repo_root / ".gitmodules"
    if not path.is_file():
        return [".gitmodules is missing although the tree is a git worktree"]
    parser = configparser.ConfigParser()
    parser.read_string(path.read_text(encoding="utf-8"))
    for section in parser.sections():
        if not section.startswith('submodule "') or not parser.has_option(section, "path"):
            continue
        if parser.get(section, "path").strip() != lock["submodule_path"]:
            continue
        url = parser.get(section, "url", fallback="").strip()
        if url != lock["upstream"]["repository"]:
            return [f".gitmodules url is {url!r}, lock says {lock['upstream']['repository']!r}"]
        branch = parser.get(section, "branch", fallback="").strip()
        if branch:
            return [f".gitmodules must not pin a branch (found {branch!r}); the lock pins a commit"]
        return []
    return [f".gitmodules does not declare {lock['submodule_path']}"]


class FingerprintDependencyError(RuntimeError):
    """The pinned detector tree is missing or does not match the lock.

    Raised by :func:`verify_files` before anything imports ``lmfpd``: a fingerprint job must
    fail loudly instead of scoring against a partial or substituted tree.
    """


def verify_files(base_dir: Path | str, *, strict_extras: bool = True) -> dict[str, object]:
    """Check ``base_dir/third_party/lm-detector`` against the lock; return where the data lives.

    Pure reads and hashing: no network, no git, no writes. The fingerprint worker calls this
    with ``settings.BASE_DIR`` right before ``lmfpd.load(...)``, so a release that shipped a
    truncated tarball or a modified file fails with every mismatch named instead of producing
    a silent verdict. Raises :class:`FingerprintDependencyError` on any difference.
    """
    root = Path(base_dir)
    if not root.is_dir():
        raise FingerprintDependencyError(f"BASE_DIR is not a directory: {root}")
    lock = load_lock(script_dir() / LOCK_NAME)
    problems = verify_tree(lock, root, require_gitlink=False)
    if not strict_extras:
        problems = [problem for problem in problems if "outside the locked whitelist" not in problem]
    if problems:
        raise FingerprintDependencyError(" | ".join(problems))
    return {
        "data_dir": str(root / lock["data_dir"]),
        "commit": lock["upstream"]["commit"],
        "sdk_version": lock["sdk"]["version"],
        "files": len(lock["files"]),
        "bytes": sum(entry["size"] for entry in lock["files"]),
    }


def describe(lock: dict) -> None:
    total = sum(entry["size"] for entry in lock["files"])
    log(
        f"lock {LOCK_NAME}: upstream {lock['upstream']['commit'][:12]} "
        f"(lmfpd {lock['sdk']['version']}), {len(lock['files'])} files, {total / 1048576:.1f} MiB"
    )


def cmd_verify(args: argparse.Namespace) -> int:
    repo_root = resolve_root(args)
    lock = load_lock(lock_file(repo_root))
    describe(lock)
    problems = verify_tree(lock, repo_root, require_gitlink=args.require_gitlink)
    if problems:
        for problem in problems:
            print(f"verify: {problem}", file=sys.stderr)
        return 1
    log(f"verified {len(lock['files'])} file(s) at {lock['upstream']['commit'][:12]}")
    return 0


def cmd_checkout(args: argparse.Namespace) -> int:
    repo_root = resolve_root(args)
    lock = load_lock(lock_file(repo_root))
    describe(lock)
    sub = superproject(lock, repo_root)
    if (sub / ".git").exists():
        head = git(sub, "rev-parse", "HEAD")
        if head != lock["upstream"]["commit"]:
            raise Failure(
                f"{sub} is at {head}, but the lock pins {lock['upstream']['commit']}. "
                "Remove the submodule directory (or re-run with --force) to re-materialise it."
            )
        apply_sparse(sub, list(lock["sparse_patterns"]))
        log(f"sparse checkout refreshed in {lock['submodule_path']}")
    elif is_git_worktree(repo_root):
        bounded_git_checkout(sub, lock)
        log(f"bounded git checkout materialised in {lock['submodule_path']}")
    else:
        bootstrap(lock, repo_root)
    if not lock["files"]:
        log("checkout: the lock has no file entries yet; run 'manifest' to record them")
        return 0
    problems = verify_tree(lock, repo_root, require_gitlink=False)
    for problem in problems:
        print(f"checkout: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    repo_root = resolve_root(args)
    lock = load_lock(lock_file(repo_root))
    describe(lock)
    bootstrap(lock, repo_root)
    if not lock["files"]:
        log("bootstrap: the lock has no file entries yet; run 'manifest' to record them")
        return 0
    problems = verify_tree(lock, repo_root, require_gitlink=False)
    for problem in problems:
        print(f"bootstrap: {problem}", file=sys.stderr)
    return 1 if problems else 0


def expand_spec(sub: Path) -> list[tuple[str, str]]:
    """Expand the whitelist from committed paths, excluding local caches and edits."""
    committed = set(git(sub, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    entries: list[tuple[str, str]] = []
    for role, spec in MANIFEST_SPEC:
        if spec.endswith("/**"):
            prefix = spec[:-3] + "/"
            matches = sorted(path for path in committed if path.startswith(prefix))
        else:
            matches = [spec] if spec in committed else []
        if not matches:
            raise Failure(f"manifest spec {spec!r} matches no committed file")
        entries.extend((role, path) for path in matches)
    return entries


def cmd_init(args: argparse.Namespace) -> int:
    """Write a skeleton lock (metadata + spec-derived sparse patterns, no file entries yet)."""
    repo_root = resolve_root(args)
    path = lock_file(repo_root)
    if path.exists() and not args.force:
        raise Failure(f"{path} already exists; pass --force to overwrite it")
    patterns: list[str] = []
    for _role, spec in MANIFEST_SPEC:
        if spec.endswith("/**"):
            patterns.append("/" + spec[:-3] + "/")
        else:
            patterns.append("/" + spec)
    lock = {
        "schema": LOCK_SCHEMA,
        "portal_version": args.portal_version,
        "submodule_path": args.submodule_path,
        "data_dir": f"{args.submodule_path}/data",
        "upstream": {
            "repository": args.repository,
            "commit": args.commit,
            "tag": None,
            "license": args.license or "unverified",
        },
        "sdk": {
            "distribution": "lmfpd",
            "version": args.sdk_version,
            "source_subdir": "sdk/python",
            "requires_python": ">=3.10",
        },
        "files": [],
        "sparse_patterns": patterns,
        "generated_by": "scripts/prepare_fingerprint.py init",
        "generated_at": utc_now(),
    }
    save_lock(path, lock)
    log(f"skeleton lock written to {path}; next: checkout, then manifest, then verify")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    repo_root = resolve_root(args)
    path = lock_file(repo_root)
    lock = load_lock(path)
    sub = superproject(lock, repo_root)
    if not (sub / ".git").exists():
        raise Failure(f"{lock['submodule_path']} is not a git checkout; manifest needs one at the pinned commit")
    head = git(sub, "rev-parse", "HEAD")
    if head != lock["upstream"]["commit"]:
        raise Failure(f"checked-out commit {head} is not the pinned {lock['upstream']['commit']}; run checkout first")
    files = []
    for role, relpath in expand_spec(sub):
        body = git_blob(sub, head, relpath)
        files.append({"path": relpath, "role": role, "size": len(body), "sha256": sha256_bytes(body)})
    lock["files"] = files
    lock["sparse_patterns"] = ["/" + entry["path"] for entry in files]
    if args.timestamp:
        lock["generated_at"] = utc_now()
    save_lock(path, lock)
    total = sum(entry["size"] for entry in files)
    log(f"manifest written: {len(files)} file(s), {total / 1048576:.1f} MiB -> {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", help="portal checkout (default: two levels above this script)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a skeleton lock for a fresh pin")
    init.add_argument("--commit", required=True, help="upstream commit SHA-1 to pin")
    init.add_argument("--sdk-version", required=True, help="lmfpd version at that commit")
    init.add_argument("--portal-version", default="1.9.2", help="portal release this lock belongs to")
    init.add_argument("--repository", default="https://github.com/Ikaleio/lm-detector.git")
    init.add_argument("--submodule-path", default="third_party/lm-detector")
    init.add_argument("--license", default="MIT")
    init.add_argument("--force", action="store_true", help="overwrite an existing lock")
    init.set_defaults(func=cmd_init)

    manifest = sub.add_parser("manifest", help="regenerate the lock's file list from the checked-out tree")
    manifest.add_argument("--timestamp", action="store_true", help="also refresh generated_at")
    manifest.set_defaults(func=cmd_manifest)

    checkout = sub.add_parser("checkout", help="materialise the pinned tree (sparse git or HTTPS bootstrap)")
    checkout.set_defaults(func=cmd_checkout)

    bootstrap_cmd = sub.add_parser("bootstrap", help="force the whitelist-only HTTPS bootstrap")
    bootstrap_cmd.set_defaults(func=cmd_bootstrap)

    verify = sub.add_parser("verify", help="fail closed unless the tree matches the lock")
    verify.add_argument("--require-gitlink", action="store_true", help="also require a staged/committed gitlink at the pin")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
