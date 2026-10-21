import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bluetooth_devices as manual

KEYBOARD = 'aa-bb-cc-dd-ee-01'
TRACKPAD = 'aa-bb-cc-dd-ee-02'
HEADPHONE = '90-7a-58-dc-f9-ff'
DEVICES = dict(keyboard=KEYBOARD, trackpad=TRACKPAD)


class SimulatedBluetooth(manual.Bluetooth):
    def __init__(self, connected=True, paired=True):
        self.states = {a: dict(paired=paired, connected=connected)
                       for a in (KEYBOARD, TRACKPAD, HEADPHONE)}
        self.calls = []
        self.lock = threading.Lock()
        self.power = True
        self.fail_pair = set()
        self.fail_release = set()
        self.pair_failures_remaining = 0
        self.fail_first_connect = set()
        self.requests = []

    def missing_inputs(self, devices):
        return [name for name, addr in devices.items() if not self.states[addr]['connected']]

    def run(self, *args, timeout=10, interactive=False):
        with self.lock:
            self.calls.append(args)
            self.requests.append((args, timeout, interactive))
            op = args[0]
            if op == '--power':
                return str(int(self.power))
            if op == '--help':
                return '--unpair --pair --connect --disconnect'
            if op == '--paired':
                return json.dumps([dict(address=a) for a, d in self.states.items() if d['paired']])
            addr = args[1]
            state = self.states[addr]
            if op == '--is-connected':
                return str(int(state['connected']))
            if op == '--unpair':
                if addr in self.fail_release:
                    raise manual.BluetoothError('simulated release failure')
                state.update(connected=False, paired=False)
            if op == '--pair':
                if addr in self.fail_pair or self.pair_failures_remaining:
                    self.pair_failures_remaining = max(0, self.pair_failures_remaining - 1)
                    raise manual.BluetoothError('simulated pairing failure')
                state['paired'] = True
            if op == '--connect':
                if addr in self.fail_first_connect:
                    self.fail_first_connect.remove(addr)
                    raise manual.BluetoothError('stale pairing')
                if not state['paired']:
                    raise manual.BluetoothError('not paired')
                state['connected'] = True
            if op == '--disconnect':
                state['connected'] = False
            return ''


class ManualCommandsTests(unittest.TestCase):
    def execute(self, bt, command, **kwargs):
        clock = (patch.object(manual.time, 'monotonic', side_effect=[0, 4])
                 if command == 'release' else contextlib.nullcontext())
        verify = manual.verify_connected
        with contextlib.redirect_stdout(io.StringIO()), clock, patch.object(
                manual, 'verify_connected', side_effect=lambda bt, devices: verify(bt, devices, seconds=0)):
            manual.execute(bt, DEVICES, command, 0.1, **kwargs)

    def mutations(self, bt):
        return [c for c in bt.calls if c[0] in ('--unpair', '--pair', '--connect', '--disconnect')]

    def test_release_unpairs_only_configured_devices(self):
        bt = SimulatedBluetooth()
        self.execute(bt, 'release')
        self.assertFalse(bt.states[KEYBOARD]['paired'])
        self.assertFalse(bt.states[TRACKPAD]['paired'])
        self.assertTrue(bt.states[HEADPHONE]['connected'])
        self.assertEqual(self.mutations(bt), [('--unpair', KEYBOARD), ('--unpair', TRACKPAD)])

    def test_release_already_released_is_noop(self):
        bt = SimulatedBluetooth(connected=False, paired=False)
        self.execute(bt, 'release')
        self.assertEqual(self.mutations(bt), [])

    def test_release_reports_already_released_instead_of_a_new_handoff(self):
        bt = SimulatedBluetooth(connected=False, paired=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(manual.time, 'monotonic', side_effect=[0, 4]):
            manual.execute(bt, DEVICES, 'release', timeout=1)
        self.assertIn('Keyboard was already released on this Mac.', output.getvalue())
        self.assertIn('Trackpad was already released on this Mac.', output.getvalue())

    def test_release_catches_a_device_that_reconnects_after_the_first_check(self):
        bt = SimulatedBluetooth(connected=False, paired=False)
        original_connected = bt.connected
        checks = 0

        def reconnect(addr, **kwargs):
            nonlocal checks
            checks += 1
            if checks > 2:
                bt.states[TRACKPAD].update(connected=True, paired=True)
            return original_connected(addr, **kwargs)

        bt.connected = reconnect
        with patch.object(manual.time, 'sleep', return_value=None):
            with self.assertRaisesRegex(manual.BluetoothError, 'trackpad reconnected'):
                manual.verify_released(bt, DEVICES)

    def test_connect_clears_stale_pairings_and_verifies_both(self):
        bt = SimulatedBluetooth(connected=False)
        bt.fail_first_connect.update(DEVICES.values())
        self.execute(bt, 'connect')
        for addr in DEVICES.values():
            self.assertTrue(bt.states[addr]['connected'])
            calls = [c[0] for c in bt.calls if c[-1] == addr]
            self.assertLess(calls.index('--unpair'), calls.index('--pair'))
            self.assertLess(calls.index('--connect'), calls.index('--unpair'))
            self.assertIn('--connect', calls[calls.index('--pair') + 1:])
        self.assertFalse(any(c[-1] == HEADPHONE for c in self.mutations(bt)))

    def test_valid_existing_pairings_are_preserved(self):
        bt = SimulatedBluetooth(connected=False)
        self.execute(bt, 'connect')
        self.assertEqual(self.mutations(bt), [('--connect', KEYBOARD), ('--connect', TRACKPAD)])

    def test_pairing_uses_full_remaining_budget_and_visible_prompts(self):
        bt = SimulatedBluetooth(connected=False, paired=False)
        bt.connect(KEYBOARD, timeout=90)
        pair = [request for request in bt.requests if request[0][0] == '--pair']
        self.assertEqual(len(pair), 1)
        self.assertGreater(pair[0][1], 80)
        self.assertTrue(pair[0][2])

    def test_interactive_pairing_inherits_terminal(self):
        bt = manual.Bluetooth.__new__(manual.Bluetooth)
        bt.binary = '/fake/blueutil'
        with patch.object(manual.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=None, stderr=None)) as run:
            bt.run('--pair', KEYBOARD, timeout=90, interactive=True)
        options = run.call_args.kwargs
        self.assertNotIn('stdin', options)
        self.assertNotIn('capture_output', options)
        self.assertEqual(options['timeout'], 90)

    def test_connect_already_connected_does_not_unpair(self):
        bt = SimulatedBluetooth()
        self.execute(bt, 'connect')
        self.assertEqual(self.mutations(bt), [])

    def test_connection_drop_cannot_print_success(self):
        bt = SimulatedBluetooth()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
                manual, 'verify_connected', side_effect=manual.BluetoothError('trackpad disconnected')):
            with self.assertRaisesRegex(manual.BluetoothError, 'trackpad disconnected'):
                manual.execute(bt, DEVICES, 'connect', 1)
        self.assertNotIn('Connected keyboard', output.getvalue())
        self.assertNotIn('Connected trackpad', output.getvalue())

    def test_missing_input_registration_cannot_pass(self):
        bt = SimulatedBluetooth()
        bt.missing_inputs = lambda devices: ['trackpad']
        with self.assertRaisesRegex(manual.BluetoothError, 'no macOS input registration'):
            manual.verify_connected(bt, DEVICES, seconds=0, input_timeout=0)

    def test_trackpad_only_does_not_touch_keyboard(self):
        bt = SimulatedBluetooth(connected=False)
        verify = manual.verify_connected
        with contextlib.redirect_stdout(io.StringIO()), patch.object(
                manual, 'verify_connected', side_effect=lambda bt, devices: verify(bt, devices, seconds=0)):
            manual.execute(bt, {'trackpad': TRACKPAD}, 'connect', 1)
        self.assertTrue(bt.states[TRACKPAD]['connected'])
        self.assertFalse(bt.states[KEYBOARD]['connected'])
        self.assertFalse(any(c[-1] == KEYBOARD for c in bt.calls if len(c) > 1))

    def test_dry_run_never_mutates(self):
        for command in ('release', 'connect'):
            bt = SimulatedBluetooth()
            self.execute(bt, command, dry_run=True)
            self.assertEqual(self.mutations(bt), [])

    def test_bluetooth_off_fails_before_mutation(self):
        bt = SimulatedBluetooth()
        bt.power = False
        with self.assertRaisesRegex(manual.BluetoothError, 'off or inaccessible'):
            self.execute(bt, 'release')
        self.assertEqual(self.mutations(bt), [])

    def test_release_failure_still_attempts_other_accessory(self):
        bt = SimulatedBluetooth()
        bt.fail_release.add(KEYBOARD)
        with self.assertRaisesRegex(manual.BluetoothError, 'keyboard:'):
            self.execute(bt, 'release')
        self.assertTrue(bt.states[KEYBOARD]['connected'])
        self.assertFalse(bt.states[TRACKPAD]['connected'])

    def test_pairing_failure_does_not_block_other_accessory(self):
        bt = SimulatedBluetooth(connected=False, paired=False)
        bt.fail_pair.add(KEYBOARD)
        with self.assertRaisesRegex(manual.BluetoothError, 'keyboard:'):
            self.execute(bt, 'connect')
        self.assertTrue(bt.states[TRACKPAD]['connected'])
        self.assertFalse(bt.states[KEYBOARD]['connected'])

    def test_retry_after_temporary_pairing_failure(self):
        bt = SimulatedBluetooth(connected=False, paired=False)
        bt.pair_failures_remaining = 1
        with patch.object(manual.time, 'sleep', return_value=None):
            bt.connect(KEYBOARD, timeout=1)
        self.assertTrue(bt.states[KEYBOARD]['connected'])
        self.assertEqual(bt.calls.count(('--pair', KEYBOARD)), 2)

    def test_lock_is_released_on_exit(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', XDG_CACHE_HOME=folder):
            with manual.operation_lock():
                with self.assertRaisesRegex(manual.BluetoothError, 'operation is running'):
                    with manual.operation_lock():
                        pass
            with manual.operation_lock():
                pass

    def test_config_rejects_duplicate_or_malformed_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'devices.json'
            for data in [dict(keyboard=KEYBOARD, trackpad=KEYBOARD),
                         dict(keyboard='--power', trackpad=TRACKPAD)]:
                path.write_text(json.dumps(data))
                with self.assertRaises(manual.BluetoothError):
                    manual.load_devices(path)


if __name__ == '__main__':
    unittest.main()
