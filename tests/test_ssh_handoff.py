import contextlib
import copy
import io
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_switch as original_tests

import ssh_handoff as ssh

DEVICES = {'keyboard': 'aa-bb-cc-dd-ee-01', 'trackpad': 'aa-bb-cc-dd-ee-02'}


def snapshot(host, connected):
    return {'host': host, 'devices': dict(DEVICES),
            'connected': dict(zip(DEVICES, connected))}


class FakeEndpoint:
    def __init__(self, state, events, label):
        self.state = copy.deepcopy(state)
        self.events = events
        self.label = label
        self.directory = Path('/example/' + label)
        self.device = 'all'
        self.probe_error = None
        self.run_error = None
        self.drop_after_connect = False

    def probe(self):
        if self.probe_error:
            raise ssh.HandoffError(self.probe_error)
        return copy.deepcopy(self.state)

    def run(self, command, timeout):
        self.events.append((self.label, command, timeout))
        if self.run_error:
            raise ssh.HandoffError(self.run_error)
        for name in self.state['connected']:
            self.state['connected'][name] = command == 'connect' and not self.drop_after_connect


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.here = FakeEndpoint(snapshot('air', [True, True]), self.events, 'air')
        self.there = FakeEndpoint(snapshot('work', [False, False]), self.events, 'work')

    def transfer(self, destination):
        with contextlib.redirect_stdout(io.StringIO()):
            ssh.transfer(self.here, self.there, destination, 90)

    def test_push_calls_release_then_connect(self):
        self.transfer('other')
        self.assertEqual(self.events, [('air', 'release', 90), ('work', 'connect', 90)])
        self.assertTrue(all(self.there.state['connected'].values()))

    def test_pull_calls_remote_release_then_local_connect(self):
        self.here.state['connected'] = dict.fromkeys(DEVICES, False)
        self.there.state['connected'] = dict.fromkeys(DEVICES, True)
        self.transfer('here')
        self.assertEqual(self.events, [('work', 'release', 90), ('air', 'connect', 90)])

    def test_preflight_ssh_failure_never_releases_anything(self):
        self.there.probe_error = 'SSH authentication failed'
        with self.assertRaisesRegex(ssh.HandoffError, 'authentication failed'):
            self.transfer('other')
        self.assertEqual(self.events, [])

    def test_different_accessory_configs_never_release(self):
        self.there.state['devices']['trackpad'] = '00-11-22-33-44-55'
        with self.assertRaisesRegex(ssh.HandoffError, 'different accessory'):
            self.transfer('other')
        self.assertEqual(self.events, [])

    def test_source_release_failure_never_starts_pairing(self):
        self.here.run_error = 'release failed'
        with self.assertRaisesRegex(ssh.HandoffError, 'release failed'):
            self.transfer('other')
        self.assertEqual(self.events, [('air', 'release', 90)])

    def test_connect_failure_prints_concrete_resume_command(self):
        self.there.run_error = 'pairing timed out'
        with self.assertRaisesRegex(ssh.HandoffError, '/example/work/connect --device all --timeout 90'):
            self.transfer('other')
        self.assertFalse(any(self.here.state['connected'].values()))
        self.assertEqual(len(self.events), 2)

    def test_dropped_connection_is_not_reported_complete(self):
        self.there.drop_after_connect = True
        with self.assertRaisesRegex(ssh.HandoffError, 'lost a connection'):
            self.transfer('other')

    def test_current_destination_is_noop(self):
        self.transfer('here')
        self.assertEqual(self.events, [])

    def test_default_route_toggles_actual_owner(self):
        self.assertEqual(ssh.route(self.here.state, self.there.state), 'other')
        self.here.state['connected'] = dict.fromkeys(DEVICES, False)
        self.there.state['connected'] = dict.fromkeys(DEVICES, True)
        self.assertEqual(ssh.route(self.here.state, self.there.state), 'here')

    def test_split_devices_need_explicit_destination(self):
        self.here.state['connected']['trackpad'] = False
        self.there.state['connected']['trackpad'] = True
        with self.assertRaisesRegex(ssh.HandoffError, 'split or disconnected'):
            ssh.route(self.here.state, self.there.state)
        self.assertEqual(ssh.route(self.here.state, self.there.state, 'here'), 'here')

    def test_same_mac_is_rejected(self):
        self.there.state['host'] = 'air'
        with self.assertRaisesRegex(ssh.HandoffError, 'points to this Mac'):
            self.transfer('other')
        self.assertEqual(self.events, [])

    def test_interactive_remote_pairing_uses_tty_and_quotes_paths(self):
        endpoint = ssh.Endpoint('/Users/example/a folder', 'trackpad', {'peer': 'example@work.local'})
        with patch.object(ssh.sys.stdin, 'isatty', return_value=True), patch.object(
                ssh.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            endpoint.run('connect', 90)
        args = run.call_args.args[0]
        self.assertIn('-tt', args)
        self.assertIn('-4', args)
        self.assertEqual(shlex.split(args[-1]),
                         ['/Users/example/a folder/connect', '--device', 'trackpad', '--timeout', '90'])
        self.assertNotIn('capture_output', run.call_args.kwargs)

    def test_peer_injection_is_rejected(self):
        for peer in ('-oProxyCommand=bad', 'user@host; command', 'user@host bad'):
            with self.assertRaises(ssh.HandoffError):
                ssh.peer_address(peer)

    def test_master_can_prompt_once_and_later_commands_reuse_session(self):
        config = {'peer': 'user@air.local', '_control_path': '/tmp/swish-test/ssh'}
        master = ssh.ssh_args(config, master=True, allow_password=True)
        followup = ssh.ssh_args(config)
        self.assertIn('BatchMode=no', master)
        self.assertIn('ControlMaster=yes', master)
        self.assertIn('BatchMode=yes', followup)
        self.assertIn('ControlMaster=no', followup)
        self.assertIn('ControlPath=/tmp/swish-test/ssh', followup)

    def test_config_override_keeps_original_file_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'handoff.json'
            config = {'peer': 'user@work.local', 'remote_dir': '/Users/user/swish'}
            path.write_text(json.dumps(config))
            loaded = ssh.load_config(path, 'user@192.168.1.2')
            self.assertEqual(loaded['peer'], 'user@192.168.1.2')
            self.assertEqual(json.loads(path.read_text()), config)


class SSHCommandIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated Bluetooth/SSH simulator from the original CLI tests.
        self.fixture = original_tests.CLIIntegrationTests('test_dry_run_does_not_mutate')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        root = self.fixture.root
        project = Path(ssh.__file__).resolve().parent
        self.local = root / 'air-swish'
        self.remote = root / 'work swish'
        for folder in (self.local, self.remote):
            folder.mkdir()
            for name in ('handoff', 'ssh_handoff.py', 'release', 'connect', 'bluetooth_devices.py'):
                shutil.copy2(project / name, folder / name)
            (folder / 'devices.json').write_text(json.dumps({
                'keyboard': 'aa-bb-cc-dd-ee-01', 'trackpad': 'aa-bb-cc-dd-ee-02'}))
        (self.local / 'handoff.json').write_text(json.dumps({
            'peer': 'user@other.local', 'remote_dir': str(self.remote)}))
        # The general fake SSH must also pass the probe's positional arguments.
        fake = original_tests.FAKE_SSH.replace(
            "source = tokens[tokens.index('-c') + 1]",
            "source = tokens[tokens.index('-c') + 1]\n"
            "script_args = tokens[tokens.index('-c') + 2:]\n"
            "script_args = script_args[:script_args.index(';')] if ';' in script_args else script_args")
        fake = fake.replace("repr(source) + ')'])", "repr(source) + ')', *script_args])")
        (self.fixture.bin / 'ssh').write_text(fake)

    def run_cli(self, *args):
        return subprocess.run([str(self.local / 'handoff'), *args],
            env=self.fixture.env, capture_output=True, text=True, timeout=30)

    def test_dry_run_probes_real_manual_helpers_and_releases_remote_lock(self):
        for _ in range(2):
            result = self.run_cli('--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Would release on', result.stdout)
            self.assertIn('No Bluetooth changes made', result.stdout)
        self.fixture.assert_no_mutation()

    def test_status_reads_both_hosts_without_mutations(self):
        result = self.run_cli('--status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('here=connected  other=disconnected', result.stdout)
        self.fixture.assert_no_mutation()

    def test_ssh_failure_exits_without_local_release(self):
        self.fixture.env['SWISH_TEST_SSH_FAIL'] = '1'
        result = self.run_cli('--to', 'other')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Connection refused', result.stderr)
        self.fixture.assert_no_mutation()


if __name__ == '__main__':
    unittest.main()
