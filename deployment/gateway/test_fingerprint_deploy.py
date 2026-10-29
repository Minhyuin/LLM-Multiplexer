#!/usr/bin/env python3
"""Exercise the actual gateway with disposable files and mocked host boundaries.

No GitHub, model, systemd, database or production filesystem is contacted.
The fake symlink boundary records targets so this also runs in Git Bash on Windows.
Run with Python 3.10+ and bash on PATH.
"""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().with_name("deploy.sh")

MOCK = r'''#!/usr/bin/env bash
set -eu
name="${0##*/}"
emit() { printf '%s\n' "$*" >> "$CASE_ROOT/events"; }
target() { command cat "$CASE_ROOT/live-target"; }
case "$name" in
  git) printf '0000000000000000000000000000000000000002\trefs/tags/v1.9.2\n' ;;
  curl)
    out=""; prev=""
    for arg in "$@"; do [ "$prev" != -o ] || out="$arg"; prev="$arg"; done
    if [ -n "$out" ] && [ "$out" != /dev/null ]; then printf archive > "$out"; emit download
    elif [ "$SCENARIO" = health_failure ] && [ "$(target)" = "$CASE_ROOT/releases/v1.9.2" ]; then printf 503
    else printf 200; fi ;;
  tar)
    dest=""; prev=""
    for arg in "$@"; do [ "$prev" != -C ] || dest="$arg"; prev="$arg"; done
    [ -n "$dest" ]; command cp -R "$CASE_ROOT/fixture/." "$dest/"; emit extract ;;
  python3|python)
    args="$*"; emit "python:$args"
    case "$args" in
      *prepare_fingerprint.py*bootstrap*) [ "$SCENARIO" != bootstrap_failure ] ;;
      *prepare_fingerprint.py*verify*) [ "$SCENARIO" != hash_failure ] ;;
      *build_sdk_wheel.py*)
        [ "$SCENARIO" != build_failure ] || exit 1
        dest=""; prev=""
        for arg in "$@"; do [ "$prev" != --wheelhouse ] || dest="$arg"; prev="$arg"; done
        printf wheel > "$dest/lmfpd-0.2.0-py3-none-any.whl" ;;
      -\ *)
        # Gateway reads the reviewed wheel manifest through Python.
        sha="$(sha256sum "$CASE_ROOT/torch/wheels-v1.9.2/lmfpd-0.2.0-py3-none-any.whl" | awk '{print $1}')"
        printf 'lmfpd-0.2.0-py3-none-any.whl %s 0.2.0\n' "$sha" ;;
      *'-m pip wheel'*) [ "$SCENARIO" != pip_failure ] ;;
      '-m venv '*)
        dest="${@: -1}"; mkdir -p "$dest/bin"
        command cp "$CASE_ROOT/bin/python3" "$dest/bin/python"; chmod +x "$dest/bin/python" ;;
      *'migrate --check'*) [ "$SCENARIO" != pending_migration ] ;;
      *collectstatic*) [ "$SCENARIO" != static_failure ] ;;
      *verify_sdk_runtime.py*) [ "$SCENARIO" != runtime_failure ] ;;
      *) : ;;
    esac
    case "$args" in
      *build_sdk_wheel.py*) printf '{}' > "$dest/lm-detector-wheel.json" ;;
    esac ;;
  systemctl)
    cmd="$1"; unit="${@: -1}"; emit "systemctl:$cmd:$unit:$(target)"
    case "$cmd" in
      cat) : ;;
      daemon-reload) : ;;
      enable) printf enabled > "$CASE_ROOT/worker-state" ;;
      stop)
        if [ "$unit" = fp-worker ]; then
          [ "$SCENARIO" != stop_failure ] || exit 1
          printf stopped > "$CASE_ROOT/worker-state"
        fi ;;
      start|restart)
        if [ "$unit" = fp-worker ]; then
          if [ "$SCENARIO" = worker_failure ] && [ "$(target)" = "$CASE_ROOT/releases/v1.9.2" ]; then exit 1; fi
          printf active > "$CASE_ROOT/worker-state"
        fi ;;
      is-active) [ "$(command cat "$CASE_ROOT/worker-state")" = active ] ;;
    esac ;;
  sed) "$REAL_SED" "$@" ;;
  cmp) exit 1 ;;
  install)
    dest="${@: -1}"; mkdir -p "$(dirname "$dest")"; printf 'unit\n' > "$dest"; emit "install:${dest##*/}" ;;
  readlink)
    if [ "${@: -1}" = "$CASE_ROOT/app" ]; then target
    else command cat "${@: -1}" 2>/dev/null; fi ;;
  ln)
    dest="${@: -1}"; src="${@: -2:1}"
    if [ "$dest" = "$CASE_ROOT/app.new" ]; then printf '%s' "$src" > "$dest"
    else :; fi ;;
  mv)
    dest="${@: -1}"; src="${@: -2:1}"
    if [ "$dest" = "$CASE_ROOT/app" ]; then
      command cp "$src" "$CASE_ROOT/live-target"; command rm "$src"; emit "flip:$(target)"
    else "$REAL_MV" "$@"; fi ;;
  pg_dump) emit unexpected-pg-dump; exit 77 ;;
  sleep) : ;;
  *) printf 'unexpected mock %s\n' "$name" >&2; exit 91 ;;
esac
'''


def native_shell_path(path):
    value = str(path.resolve()).replace('\\', '/')
    if os.name == 'nt':
        return '/' + value[0].lower() + value[2:]
    return value


SCENARIOS = ('bootstrap_failure', 'hash_failure', 'build_failure', 'pip_failure',
             'runtime_failure', 'pending_migration', 'static_failure',
             'stop_failure', 'worker_failure', 'health_failure', 'disabled', 'success')


def prepare_case(root, scenario, *, enabled=True, lock=True, old_feature=True):
    """Create fixtures only; never start a shell or modify an existing fixture."""
    root.mkdir(parents=True, exist_ok=False)
    shell_root = native_shell_path(root)
    for folder in ('bin', 'gateway', 'state', 'logs', 'torch', 'shared', 'app',
                   'releases/v1.9.1', 'fixture/scripts',
                   'fixture/deployment/gateway', 'systemd'):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / 'gateway/deploy.sh').write_bytes(SOURCE.read_bytes())
    (root / 'fixture/deployment/gateway/pioneer-portal-fingerprint.service').write_text(
        '[Service]\nUser=@WORKER_USER@\nGroup=@WORKER_GROUP@\n', encoding='utf-8')
    for command in ('git', 'curl', 'tar', 'python3', 'systemctl', 'readlink', 'ln', 'mv', 'install', 'pg_dump', 'sleep'):
        executable = root / 'bin' / command
        executable.write_text(MOCK, encoding='utf-8', newline='\n')
        executable.chmod(0o755)
    (root / 'shared/.env').write_text('FINGERPRINT_ENABLED=' + ('true' if enabled else 'false') + '\n')
    (root / 'state/last_deployed_tag').write_text('v1.9.1\n')
    (root / 'live-target').write_text(shell_root + '/releases/v1.9.1')
    (root / 'worker-state').write_text('active' if enabled else 'stopped')
    for name in ('manage.py', 'requirements.txt'):
        (root / 'fixture' / name).write_text('# disposable test fixture\n')
    if lock:
        for name in ('lm-detector_lock.json', 'prepare_fingerprint.py', 'build_sdk_wheel.py'):
            (root / 'fixture/scripts' / name).write_text('{}\n')
    if enabled or scenario != 'disabled':
        command = root / 'fixture/fingerprints/management/commands/run_fingerprint_worker.py'
        command.parent.mkdir(parents=True)
        command.touch()
    if old_feature:
        command = root / 'releases/v1.9.1/fingerprints/management/commands/run_fingerprint_worker.py'
        command.parent.mkdir(parents=True)
        command.touch()
    config = {
        'DEPLOY_REPO_URL': 'https://github.com/test/portal.git', 'DEPLOY_BRANCH': 'main',
        'DEPLOY_TAG_PATTERN': 'v*', 'DEPLOY_TAG_REGEX': r'^v[0-9]+\.[0-9]+\.[0-9]+$',
        'APP_LINK': shell_root + '/app', 'RELEASES_DIR': shell_root + '/releases',
        'SHARED_DIR': shell_root + '/shared', 'STATE_DIR': shell_root + '/state',
        'LOG_DIR': shell_root + '/logs', 'TORCH_DIR': shell_root + '/torch',
        'LOG_FILE': shell_root + '/logs/deploy.log', 'LOCK_FILE': shell_root + '/deploy.lock',
        'ENV_FILE': shell_root + '/shared/.env', 'SERVICE': 'portal', 'WORKER_SERVICE': 'fp-worker',
        'WORKER_USER': 'fixture', 'WORKER_GROUP': 'fixture',
        'SYSTEMD_DIR': shell_root + '/systemd',
        'HEALTH_CHECKS': 'http://127.0.0.1:9/health/:200', 'HEALTH_RETRIES': '1',
        'HEALTH_DELAY': '0', 'EXTRA_HEALTH_CHECK': '', 'HEALTH_HOST': '', 'KEEP_RELEASES': '10',
        'ALLOW_AUTO_MIGRATE': '0', 'SKIP_DB_BACKUP': '0', 'DRY_RUN': '0', 'GIT_TIMEOUT': '10',
    }
    (root / 'gateway/deploy.env').write_text('\n'.join(f"{key}='{value}'" for key, value in config.items()) + '\n', encoding='utf-8', newline='\n')
    # The shell owns child-shell execution; Python only prepares/checks files.
    launch = '\n'.join([
        '#!/usr/bin/env bash', 'set -u',
        'export REAL_MV="$(command -v mv)"',
        'export REAL_SED="$(command -v sed)"',
        'export CASE_ROOT=' + shlex.quote(shell_root),
        'export SCENARIO=' + shlex.quote(scenario),
        'export PATH="$CASE_ROOT/bin:$PATH"', 'export MSYS_NO_PATHCONV=1',
        'bash "$CASE_ROOT/gateway/deploy.sh" > "$CASE_ROOT/output.log" 2>&1',
        'rc=$?', 'printf "%s\\n" "$rc" > "$CASE_ROOT/exit-code"',
        'printf "%s: exit %s\\n" "$SCENARIO" "$rc"', 'exit 0', '',
    ])
    (root / 'run.sh').write_text(launch, encoding='utf-8', newline='\n')


def collect_case(root):
    events = (root / 'events').read_text().splitlines() if (root / 'events').exists() else []
    failure = (root / 'state/LAST_FAILURE').read_text() if (root / 'state/LAST_FAILURE').exists() else ''
    return {'code': int((root / 'exit-code').read_text()), 'events': events,
            'live': (root / 'live-target').read_text(),
            'tag': (root / 'state/last_deployed_tag').read_text().strip(), 'failure': failure,
            'output': (root / 'output.log').read_text(encoding='utf-8', errors='replace'),
            'root': native_shell_path(root)}


class GatewayFailureTests(unittest.TestCase):
    maxDiff = None
    fixture_root = None

    def run_case(self, scenario, *, enabled=True, lock=True, old_feature=True):
        if self.fixture_root is not None:
            return collect_case(self.fixture_root / scenario)
        with tempfile.TemporaryDirectory(prefix='fp-gateway-') as tmp:
            root = Path(tmp).resolve() / scenario
            prepare_case(root, scenario, enabled=enabled, lock=lock, old_feature=old_feature)
            subprocess.run(['bash', native_shell_path(root / 'run.sh')], check=True, timeout=30)
            return collect_case(root)

    def assert_preserved(self, result):
        self.assertNotEqual(result['code'], 0, result['output'])
        self.assertEqual(result['tag'], 'v1.9.1', result['output'])
        self.assertEqual(result['live'], result['root'] + '/releases/v1.9.1', result['output'])
        self.assertFalse(any(event.startswith('flip:') for event in result['events']), result['output'])

    def test_staging_failures_preserve_live_and_do_not_stop_worker(self):
        for scenario in ('bootstrap_failure', 'hash_failure', 'build_failure', 'pip_failure',
                         'runtime_failure', 'pending_migration', 'static_failure'):
            with self.subTest(scenario=scenario):
                result = self.run_case(scenario)
                self.assert_preserved(result)
                self.assertFalse(any(event.startswith('systemctl:stop:') for event in result['events']), result['output'])

    def test_worker_unit_is_installed_even_when_feature_is_off(self):
        result = self.run_case('success', enabled=False)
        self.assertEqual(result['code'], 0, result['output'])
        self.assertTrue(any(event.startswith('install:fp-worker.service') for event in result['events']), result['output'])
        self.assertTrue(any(event.startswith('systemctl:enable:fp-worker') for event in result['events']), result['output'])
        self.assertTrue(any(event.startswith('systemctl:start:fp-worker:') for event in result['events']), result['output'])

    def test_stop_failure_never_flips_code(self):
        result = self.run_case('stop_failure')
        self.assert_preserved(result)
        self.assertIn('cannot stop fp-worker', result['output'])

    def test_worker_and_health_failures_roll_back_in_order(self):
        for scenario in ('worker_failure', 'health_failure'):
            with self.subTest(scenario=scenario):
                result = self.run_case(scenario)
                self.assertEqual(result['code'], 1, result['output'])
                self.assertEqual(result['live'], result['root'] + '/releases/v1.9.1', result['output'])
                self.assertEqual(result['tag'], 'v1.9.1')
                self.assertTrue(result['failure'])
                events = result['events']
                flips = [index for index, event in enumerate(events) if event.startswith('flip:')]
                self.assertEqual(len(flips), 2, result['output'])
                for index in flips:
                    self.assertTrue(any(event.startswith('systemctl:stop:fp-worker:') for event in events[:index]), result['output'])
                self.assertTrue(any(event.startswith('systemctl:stop:fp-worker:') for event in events[flips[0] + 1:flips[1]]), result['output'])
                self.assertTrue(any(event.startswith('systemctl:restart:fp-worker:') for event in events[flips[1] + 1:]), result['output'])

    def test_disabled_feature_can_deploy_old_nonworker_release(self):
        result = self.run_case('disabled', enabled=False, lock=False, old_feature=False)
        self.assertEqual(result['code'], 0, result['output'])
        self.assertEqual(result['tag'], 'v1.9.2')
        self.assertFalse(any(event.startswith(('systemctl:start:fp-worker:', 'systemctl:restart:fp-worker:')) for event in result['events']))

    def test_success_installs_exact_wheel_and_stops_before_flip(self):
        result = self.run_case('success')
        self.assertEqual(result['code'], 0, result['output'])
        self.assertEqual(result['tag'], 'v1.9.2')
        events = result['events']
        flip = next(i for i, event in enumerate(events) if event.startswith('flip:'))
        self.assertTrue(any(event.startswith('systemctl:stop:fp-worker:') for event in events[:flip]))
        self.assertTrue(any(event.startswith('systemctl:start:fp-worker:') for event in events[flip:]))
        self.assertTrue(any('--no-index --no-deps' in event and 'lmfpd-0.2.0-py3-none-any.whl' in event for event in events))
        self.assertFalse(any('pip wheel' in event and 'lmfpd==' in event for event in events))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--prepare':
        bundle = Path(sys.argv[2]).resolve()
        bundle.mkdir(parents=True, exist_ok=False)
        commands = ['#!/usr/bin/env bash', 'set -eu']
        for scenario in SCENARIOS:
            enabled = scenario != 'disabled'
            prepare_case(bundle / scenario, scenario, enabled=enabled,
                         lock=enabled, old_feature=enabled)
            commands.append('bash ' + shlex.quote(native_shell_path(bundle / scenario / 'run.sh')))
        (bundle / 'run-all.sh').write_text('\n'.join(commands) + '\n', encoding='utf-8', newline='\n')
        print('Prepared:', bundle / 'run-all.sh')
    else:
        if len(sys.argv) == 3 and sys.argv[1] == '--check':
            GatewayFailureTests.fixture_root = Path(sys.argv[2]).resolve()
            sys.argv = sys.argv[:1]
        program = unittest.main(verbosity=2, exit=False)
        if GatewayFailureTests.fixture_root is not None:
            records = {name: collect_case(GatewayFailureTests.fixture_root / name) for name in SCENARIOS}
            (GatewayFailureTests.fixture_root / 'evidence.json').write_text(
                json.dumps({'passed': program.result.wasSuccessful(), 'scenarios': records}, indent=2),
                encoding='utf-8')
        sys.exit(0 if program.result.wasSuccessful() else 1)
