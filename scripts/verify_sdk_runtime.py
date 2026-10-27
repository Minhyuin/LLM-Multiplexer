#!/usr/bin/env python3
"""Verify the installed ``lmfpd`` stack offline, against the pinned lock.

Run this with the interpreter that will execute fingerprint jobs (the release virtualenv, the
CI job, ``.portal-test-venv`` locally). It answers one question: does this environment score
with the pinned detector, without touching the network?

    python scripts/verify_sdk_runtime.py

Checks: the installed distribution and every installed module match ``lm-detector_lock.json``;
``lmfpd.load(<repo>/third_party/lm-detector/data)`` reads the pinned bank with sockets disabled;
challenge generation, ``build_request`` for all three wire protocols, and ``analyze`` produce
well-formed results; ``parse_numbers`` behaves as documented. Exit code 0 means every check
passed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare_fingerprint as prep  # noqa: E402 - scripts/ is a flat tool directory

SDK_SOURCE_PREFIX = "sdk/python/src/lmfpd/"
PROTOCOLS = {
    "responses": "responses",
    "chat_completions": "chat/completions",
    "messages": "messages",
}


class CheckFailed(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OfflineGuard:
    """Fail loudly if anything below tries to open a socket."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.attempts: list[str] = []

    def __enter__(self) -> "OfflineGuard":
        if not self.enabled:
            return self
        self._saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo)

        def refuse(name):
            def blocked(*args, **kwargs):
                self.attempts.append(f"{name}{args[:2]}")
                raise CheckFailed(f"network access attempted during offline verification: {name}{args[:2]}")

            return blocked

        socket.socket.connect = refuse("connect")
        socket.socket.connect_ex = refuse("connect_ex")
        socket.create_connection = refuse("create_connection")
        socket.getaddrinfo = refuse("getaddrinfo")
        return self

    def __exit__(self, *exc_info) -> None:
        if not self.enabled:
            return
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo = self._saved


def check_installed(lock: dict, checks: list[dict]) -> None:
    try:
        version = importlib.metadata.version(lock["sdk"]["distribution"])
    except importlib.metadata.PackageNotFoundError:
        raise CheckFailed(
            f"{lock['sdk']['distribution']} is not installed in this interpreter; "
            "install the wheel built by scripts/build_sdk_wheel.py"
        ) from None
    if version != lock["sdk"]["version"]:
        raise CheckFailed(f"installed {lock['sdk']['distribution']} {version}, lock pins {lock['sdk']['version']}")
    import lmfpd

    package_dir = Path(lmfpd.__file__).resolve().parent
    expected = {entry["path"][len(SDK_SOURCE_PREFIX):]: entry for entry in lock["files"] if entry["path"].startswith(SDK_SOURCE_PREFIX)}
    problems = []
    for name, entry in sorted(expected.items()):
        path = package_dir / name
        if not path.is_file():
            problems.append(f"missing {name}")
        elif prep.sha256_file(path) != entry["sha256"]:
            problems.append(f"{name} differs from the lock")
    installed = sorted(path.name for path in package_dir.iterdir() if path.is_file())
    unexpected = [name for name in installed if name not in expected]
    if unexpected:
        problems.append(f"unexpected installed modules: {unexpected}")
    if problems:
        raise CheckFailed("; ".join(problems))
    missing_exports = [name for name in ("load", "Detector", "build_request", "generate_challenges", "parse_numbers") if not hasattr(lmfpd, name)]
    if missing_exports:
        raise CheckFailed(f"lmfpd is missing public names: {missing_exports}")
    checks.append(
        {
            "check": "installed-distribution",
            "installed_version": version,
            "package_dir": str(package_dir),
            "modules_verified": len(expected),
        }
    )


def check_data(lock: dict, repo_root: Path, checks: list[dict]) -> object:
    import lmfpd

    data_dir = repo_root / lock["data_dir"]
    detector = lmfpd.load(str(data_dir))
    built_at = detector.built_at
    if not isinstance(built_at, str) or not built_at.strip():
        raise CheckFailed("the loaded bank reports no built_at")
    checks.append({"check": "load-local-bank", "data_dir": str(data_dir), "built_at": built_at})
    return detector


def check_challenges(checks: list[dict]) -> list[dict]:
    import lmfpd

    challenges = lmfpd.generate_challenges(3)
    if len(challenges) != 3:
        raise CheckFailed(f"generate_challenges(3) returned {len(challenges)}")
    ids = {challenge["id"] for challenge in challenges}
    if len(ids) != 3:
        raise CheckFailed(f"challenge ids are not unique: {sorted(ids)}")
    for challenge in challenges:
        count = challenge["expected_count"]
        if not isinstance(count, int) or not 1 <= count <= 355:
            raise CheckFailed(f"challenge {challenge['id']} asks for an implausible count {count!r}")
        if len(challenge["prompt"]) < 40:
            raise CheckFailed(f"challenge {challenge['id']} has a suspiciously short prompt")
    checks.append({"check": "generate-challenges", "ids": sorted(ids), "counts": [c["expected_count"] for c in challenges]})
    return challenges


def check_requests(challenges: list[dict], checks: list[dict]) -> None:
    import lmfpd

    prompt = challenges[0]["prompt"]
    for api, suffix in PROTOCOLS.items():
        request = lmfpd.build_request(
            {"api": api, "base_url": "http://127.0.0.1:9/v1", "api_key": "offline-check", "model": "offline-check"},
            prompt,
        )
        if not request["url"].endswith(suffix):
            raise CheckFailed(f"api={api} produced url {request['url']}")
        headers = {key.lower(): value for key, value in request["headers"].items()}
        if "offline-check" not in json.dumps(headers):
            raise CheckFailed(f"api={api} did not carry the api key")
        body = request["body"]
        if body.get("model") != "offline-check":
            raise CheckFailed(f"api={api} body has model {body.get('model')!r}")
        if not body.get("messages") and not body.get("input"):
            raise CheckFailed(f"api={api} body carries no prompt")
    checks.append({"check": "build-request", "protocols": sorted(PROTOCOLS)})


ANALYSIS_KEYS = (
    "decision",
    "prediction",
    "prediction_name",
    "family_prediction",
    "family_prediction_name",
    "results",
    "ranking_score",
    "probability",
    "probability_status",
    "probability_scope",
    "calibration",
    "used_outputs",
    "diagnostics",
    "sequence",
    "method",
    "model_version",
    "evidence",
)
DECISIONS = ("scored", "partial", "unscorable", "not_model")


def check_analysis(detector: object, challenges: list[dict], checks: list[dict]) -> None:
    """Score one round of synthetic ramps and hold the result to the documented shape.

    A synthetic list of consecutive integers is deliberately not a language-model reply, so
    the expected decision here is ``not_model`` or ``unscorable``: the check asserts the
    contract (keys, diagnostics, evidence, ranking fields when present) rather than a verdict
    that only a real model reply could produce.
    """
    outputs = [
        {"text": " ".join(str(index) for index in range(1, challenge["expected_count"] + 1)), "expected_count": challenge["expected_count"]}
        for challenge in challenges
    ]
    analysis = detector.analyze(outputs)
    missing = [key for key in ANALYSIS_KEYS if key not in analysis]
    if missing:
        raise CheckFailed(f"analysis is missing keys: {missing}")
    decision = analysis["decision"]
    if decision not in DECISIONS:
        raise CheckFailed(f"analysis decided {decision!r}, which is not one of {DECISIONS}")
    if analysis["used_outputs"] != 3:
        raise CheckFailed(f"analyze() used {analysis['used_outputs']} outputs instead of 3")
    diagnostics = analysis["diagnostics"]
    if len(diagnostics) != 3:
        raise CheckFailed(f"analysis carries {len(diagnostics)} diagnostics for 3 outputs")
    probability = analysis["probability"]
    results = analysis["results"]
    if decision in ("scored", "partial"):
        if not results:
            raise CheckFailed(f"{decision!r} analysis carries no candidates")
        top = results[0]
        for key in ("model", "display_name", "family", "family_name", "score"):
            if key not in top:
                raise CheckFailed(f"candidate is missing {key!r}")
        if top["model"] != analysis["prediction"]:
            raise CheckFailed("prediction does not name the top candidate")
        if not 0.0 <= float(top["score"]) <= 1.0:
            raise CheckFailed(f"candidate score {top['score']!r} is outside [0, 1]")
        if decision == "scored" and analysis["ranking_score"] is None:
            raise CheckFailed("a scored analysis carries no ranking_score")
        if probability is not None and not 0.0 <= float(probability) <= 1.0:
            raise CheckFailed(f"probability {probability!r} is outside [0, 1]")
    else:
        if results:
            raise CheckFailed(f"{decision!r} analysis must not carry candidates")
        if not str(analysis["evidence"].get("reason") or "").strip():
            raise CheckFailed(f"{decision!r} analysis carries no evidence reason")
    base = analysis["model_version"]["base_sha256"]
    if not isinstance(base, str) or len(base) != 64:
        raise CheckFailed(f"model_version.base_sha256 is not a sha256 hex string: {base!r}")
    repeat = detector.analyze(outputs)
    if repeat["decision"] != decision or [result["model"] for result in repeat["results"]] != [result["model"] for result in results]:
        raise CheckFailed("analyze() is not deterministic for identical outputs")
    checks.append(
        {
            "check": "analyze",
            "decision": decision,
            "prediction": analysis["prediction"],
            "probability": probability,
            "probability_status": analysis["probability_status"],
            "diagnostics_accepted": [bool(item["accepted"]) for item in diagnostics],
            "candidates": [result["model"] for result in results],
            "base_sha256": base,
        }
    )


def check_parse_numbers(checks: list[dict]) -> None:
    import lmfpd

    cases = {
        "1 2 3": [1, 2, 3],
        "reply: 7 8 9.": [7, 8, 9],
        "no digits here": [],
    }
    for text, expected in cases.items():
        got = lmfpd.parse_numbers(text)
        if got != expected:
            raise CheckFailed(f"parse_numbers({text!r}) returned {got!r}, expected {expected!r}")
    checks.append({"check": "parse-numbers", "cases": len(cases)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", help="portal checkout (default: the parent of scripts/)")
    parser.add_argument("--json", action="store_true", help="print a JSON report instead of log lines")
    parser.add_argument("--allow-network", action="store_true", help="do not disable sockets (debug only)")
    parser.add_argument("--skip-installed-hashes", action="store_true", help="skip module hashes against the lock")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else prep.repo_root_default()
    lock = prep.load_lock(prep.script_dir() / prep.LOCK_NAME)
    checks: list[dict] = []
    status = 0
    try:
        if not args.skip_installed_hashes:
            check_installed(lock, checks)
        with OfflineGuard(enabled=not args.allow_network) as guard:
            detector = check_data(lock, repo_root, checks)
            challenges = check_challenges(checks)
            check_requests(challenges, checks)
            check_analysis(detector, challenges, checks)
            check_parse_numbers(checks)
        if guard.attempts:
            raise CheckFailed(f"offline verification recorded socket attempts: {guard.attempts}")
    except CheckFailed as exc:
        status = 1
        print(f"FAIL: {exc}", file=sys.stderr)
    if args.json:
        print(json.dumps({"ok": status == 0, "checks": checks}, indent=2, ensure_ascii=False))
    elif status == 0:
        for check in checks:
            print(f"PASS  {check['check']}: " + ", ".join(f"{key}={value}" for key, value in check.items() if key != "check"))
        print(f"offline runtime verification passed ({lock['sdk']['distribution']} {lock['sdk']['version']}, commit {lock['upstream']['commit'][:12]})")
    return status


if __name__ == "__main__":
    sys.exit(main())
