import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('swish', str(ROOT / 'switch'))
spec = importlib.util.spec_from_loader(loader.name, loader)
swish = importlib.util.module_from_spec(spec)
loader.exec_module(swish)
KEYBOARD = 'aa-bb-cc-dd-ee-01'
TRACKPAD = 'aa-bb-cc-dd-ee-02'
HEADPHONE = 'aa-bb-cc-dd-ee-03'
ADDRESSES = [KEYBOARD, TRACKPAD]

FAKE_BLUEUTIL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ['SWISH_TEST_STATE'])
state = json.loads(path.read_text())
host = os.environ.get('SWISH_TEST_HOST', 'local')
args = sys.argv[1:]
state['log'].append([host, args])
path.write_text(json.dumps(state))
if args == ['--help']:
    print('--unpair --pair --connect --disconnect')
elif args == ['--power']:
    print('0' if state.get('power_off') == host else '1')
elif args[:1] == ['--paired']:
    print(json.dumps([dict(address=a, name=d['name'], paired=True,
                          connected=d['connected'])
        for a, d in state[host].items() if d['paired']]))
else:
    op, addr = args
    device = state[host][addr]
    if op == '--is-connected':
        print(int(device['connected']))
    elif op == '--disconnect':
        device['connected'] = False
    elif op == '--unpair':
        device['paired'] = device['connected'] = False
    elif op == '--pair':
        if state.get('pair_fail') == host:
            print('simulated pairing failure', file=sys.stderr)
            sys.exit(1)
        device['paired'] = True
    elif op == '--connect':
        other = 'remote' if host == 'local' else 'local'
        if not device['paired'] or state[other][addr]['connected']:
            sys.exit(1)
        device['connected'] = True
    else:
        sys.exit(64)
    path.write_text(json.dumps(state))
'''

FAKE_SSH = r'''#!/usr/bin/env python3
import os, shlex, sys
if os.environ.get('SWISH_TEST_SSH_FAIL'):
    print('Connection refused', file=sys.stderr)
    sys.exit(255)
lexer = shlex.shlex(sys.argv[-1], posix=True, punctuation_chars=True)
lexer.whitespace_split = True
tokens = list(lexer)
source = tokens[tokens.index('-c') + 1]
os.environ['SWISH_TEST_HOST'] = 'remote'
os.environ['HOME'] = os.environ['SWISH_TEST_REMOTE_HOME']
prefix = 'import socket; socket.gethostname = lambda: "other-mac"; '
os.execv(sys.executable, [sys.executable, '-u', '-c', prefix + 'exec(' + repr(source) + ')'])
'''


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, source in [('blueutil', FAKE_BLUEUTIL), ('ssh', FAKE_SSH)]:
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        for host in ('local', 'remote'):
            (self.root / host).mkdir()
        self.state = self.root / 'state.json'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'peer': 'user@other-mac.local',
            'devices': dict(keyboard=KEYBOARD, trackpad=TRACKPAD)}))
        devices = {a: dict(name=n, paired=True, connected=True)
                   for a, n in [(KEYBOARD, 'Magic Keyboard'),
                                (TRACKPAD, 'Magic Trackpad'), (HEADPHONE, 'Headphones')]}
        remote_devices = {a: dict(d, connected=False) for a, d in devices.items()}
        self.state.write_text(json.dumps(dict(local=devices, remote=remote_devices, log=[])))
        self.env = dict(os.environ, HOME=str(self.root / 'local'),
            PATH=str(self.bin) + ':' + os.environ['PATH'],
            SWISH_TEST_STATE=str(self.state), SWISH_TEST_REMOTE_HOME=str(self.root / 'remote'))
        self.env.pop('SWISH_TEST_HOST', None)

    def run_cli(self, *args, success=True):
        result = subprocess.run([str(ROOT / 'switch'), '--config', str(self.config), *args],
            text=True, capture_output=True, env=self.env, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def read_state(self):
        return json.loads(self.state.read_text())

    def assert_no_mutation(self):
        self.assertFalse([args for _, args in self.read_state()['log']
            if args[0] in ('--unpair', '--pair', '--connect', '--disconnect')])

    def test_round_trip_and_unrelated_device_untouched(self):
        self.run_cli()
        state = self.read_state()
        self.assertTrue(all(state['remote'][a]['connected'] for a in ADDRESSES))
        self.assertFalse(any(state['local'][a]['connected'] for a in ADDRESSES))
        self.assertTrue(state['local'][HEADPHONE]['connected'])
        self.assertFalse(any(args[-1] == HEADPHONE for _, args in state['log']
                             if args[0] in ('--unpair', '--pair', '--connect', '--disconnect')))
        self.run_cli()
        state = self.read_state()
        self.assertTrue(all(state['local'][a]['connected'] for a in ADDRESSES))
        self.assertFalse(any(state['remote'][a]['connected'] for a in ADDRESSES))

    def test_dry_run_does_not_mutate(self):
        self.assertIn('Would move', self.run_cli('--dry-run').stdout)
        self.assert_no_mutation()

    def test_doctor_and_status_do_not_mutate(self):
        self.assertIn('OK:', self.run_cli('doctor').stdout)
        self.assertIn('this Mac', self.run_cli('status').stdout)
        self.assert_no_mutation()

    def test_unreachable_peer_does_not_disconnect(self):
        self.env['SWISH_TEST_SSH_FAIL'] = '1'
        result = self.run_cli(success=False)
        self.assertIn('Connection refused', result.stderr)
        self.assert_no_mutation()

    def test_peer_bluetooth_off_does_not_disconnect(self):
        state = self.read_state()
        state['power_off'] = 'remote'
        self.state.write_text(json.dumps(state))
        self.assertIn('Bluetooth is off', self.run_cli(success=False).stderr)
        self.assert_no_mutation()

    def test_already_at_explicit_destination_is_noop(self):
        self.assertIn('already connected', self.run_cli('--to', 'here').stdout)
        self.assert_no_mutation()

    def test_split_devices_require_explicit_destination(self):
        state = self.read_state()
        state['local'][TRACKPAD]['connected'] = False
        state['remote'][TRACKPAD]['connected'] = True
        self.state.write_text(json.dumps(state))
        self.assertIn('split', self.run_cli(success=False).stderr)
        self.assert_no_mutation()
        self.run_cli('--to', 'other')
        state = self.read_state()
        self.assertTrue(all(state['remote'][a]['connected'] for a in ADDRESSES))

    def test_init_explicit_addresses_and_no_overwrite(self):
        self.config.unlink()
        self.run_cli('init', '--peer', 'user@other.local', '--keyboard',
                     KEYBOARD.upper().replace('-', ':'), '--trackpad', TRACKPAD)
        self.assertEqual(json.loads(self.config.read_text())['devices']['keyboard'], KEYBOARD)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.run_cli('init', '--peer', 'new.local', '--keyboard', KEYBOARD,
                     '--trackpad', TRACKPAD, success=False)
        self.assertEqual(json.loads(self.config.read_text())['peer'], 'user@other.local')
        self.assert_no_mutation()

    def test_init_autodetect(self):
        self.config.unlink()
        self.run_cli('init', '--peer', 'other.local')
        self.assertEqual(json.loads(self.config.read_text())['devices'],
                         dict(keyboard=KEYBOARD, trackpad=TRACKPAD))
        self.assert_no_mutation()

    def test_lock_contention_fails_before_disconnect(self):
        with patch.dict(os.environ, self.env), swish.locked():
            self.assertIn('already running', self.run_cli(success=False).stderr)
        self.assert_no_mutation()

    def test_stale_record_removed_before_destination_pair(self):
        self.run_cli()
        ops = [args[0] for host, args in self.read_state()['log']
               if host == 'remote' and args[-1] == KEYBOARD]
        self.assertLess(ops.index('--unpair'), ops.index('--pair'))
        self.assertLess(ops.index('--pair'), ops.index('--connect'))


class FakeEndpoint:
    def __init__(self, connected, fail_claim=False):
        self.state = dict(zip(ADDRESSES, connected))
        self.fail_claim = fail_claim
        self.calls = []

    def call(self, op, **kwargs):
        self.calls.append(op)
        if op == 'snapshot':
            return dict(self.state)
        if op == 'claim' and self.fail_claim:
            # Simulate a partial successful connection before failure.
            self.state[kwargs['addresses'][0]] = True
            raise swish.Error('pairing failed')
        for addr in kwargs['addresses']:
            self.state[addr] = op == 'claim'
        return True


class RecoveryTests(unittest.TestCase):
    def test_failed_handoff_restores_both_source_connections(self):
        local, remote = FakeEndpoint([True, True]), FakeEndpoint([False, False], True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(swish.Error, 'Original connections restored'):
                swish.handoff(local, remote, ADDRESSES, 'other',
                              dict(local.state), dict(remote.state), 10)
        self.assertTrue(all(local.state.values()))
        self.assertFalse(any(remote.state.values()))

    def test_split_failure_restores_original_connections_on_both_macs(self):
        local, remote = FakeEndpoint([True, False]), FakeEndpoint([False, True], True)
        before_local, before_remote = dict(local.state), dict(remote.state)
        original_call = remote.call

        def fail_only_once(op, **kwargs):
            try:
                return original_call(op, **kwargs)
            finally:
                if op == 'claim':
                    remote.fail_claim = False

        remote.call = fail_only_once
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(swish.Error, 'Original connections restored'):
                swish.handoff(local, remote, ADDRESSES, 'other', before_local, before_remote, 10)
        self.assertEqual(local.state, before_local)
        self.assertEqual(remote.state, before_remote)

    def test_recovery_failure_is_reported(self):
        local, remote = FakeEndpoint([True, True], True), FakeEndpoint([False, False], True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(swish.Error, 'Recovery failed'):
                swish.handoff(local, remote, ADDRESSES, 'other',
                              dict(local.state), dict(remote.state), 10)

    def test_address_validation_rejects_command_text(self):
        for value in ['--power', 'aa; touch /tmp/test', 'wrong']:
            with self.assertRaises(swish.Error):
                swish.address(value)

    def test_peer_validation_rejects_ssh_options(self):
        for value in ['-oProxyCommand=evil', 'host; evil', 'user@host bad']:
            with self.assertRaises(Exception):
                swish.peer_address(value)


if __name__ == '__main__':
    unittest.main()
