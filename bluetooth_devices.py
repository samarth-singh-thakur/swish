#!/usr/bin/env python3
"""Release or connect the configured Magic accessories on this Mac only."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time


class BluetoothError(Exception):
    pass


def normalize(value):
    if not isinstance(value, str):
        raise BluetoothError('Bluetooth addresses must be strings.')
    value = value.replace(':', '-').lower()
    if not re.fullmatch(r'(?:[0-9a-f]{2}-){5}[0-9a-f]{2}', value):
        raise BluetoothError('Invalid Bluetooth address: ' + value)
    return value


def load_devices(path):
    try:
        data = json.loads(path.read_text())
        devices = {name: normalize(data[name]) for name in ('keyboard', 'trackpad')}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BluetoothError('Cannot read devices configuration at %s: %s' % (path, exc))
    if len(set(devices.values())) != 2:
        raise BluetoothError('Keyboard and trackpad must have different addresses.')
    return devices


@contextlib.contextmanager
def operation_lock():
    # Sharing the previous CLI's lock prevents an old handoff racing these commands.
    folder = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'magic-switch'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BluetoothError('Another Bluetooth operation is running on this Mac. '
                                 'Wait for that command to finish.')
        yield


class Bluetooth:
    def __init__(self):
        self.binary = next((p for p in [shutil.which('blueutil'),
            '/opt/homebrew/bin/blueutil', '/usr/local/bin/blueutil']
            if p and os.access(p, os.X_OK)), None)
        if not self.binary:
            raise BluetoothError('Install blueutil first: brew install blueutil')

    def run(self, *args, timeout=10, interactive=False):
        env = dict(os.environ)
        env.pop('BLUEUTIL_USE_SYSTEM_PROFILER', None)
        try:
            options = dict(text=True, timeout=timeout, env=env)
            if not interactive:
                options.update(capture_output=True, stdin=subprocess.DEVNULL)
            result = subprocess.run([self.binary, *args], **options)
        except subprocess.TimeoutExpired:
            raise BluetoothError('Timed out: blueutil ' + ' '.join(args))
        except OSError as exc:
            raise BluetoothError('Cannot run blueutil: ' + str(exc))
        if result.returncode:
            if result.returncode in (-6, 134) or 'abort signal' in (result.stderr or '').lower():
                raise BluetoothError('Bluetooth access was denied. In System Settings > '
                    'Privacy & Security > Bluetooth, enable your terminal app. '
                    'For commands run over SSH, enable /usr/libexec/sshd-keygen-wrapper.')
            raise BluetoothError((result.stderr or '').strip() or 'blueutil failed: ' + ' '.join(args))
        return (result.stdout or '').strip()

    def paired(self):
        try:
            data = json.loads(self.run('--paired', '--format', 'json'))
            if not isinstance(data, list):
                raise ValueError('expected a device list')
            return {normalize(d['address']) for d in data}
        except (ValueError, TypeError, KeyError) as exc:
            raise BluetoothError('Cannot read Bluetooth devices. Check Bluetooth privacy access: ' + str(exc))

    def connected(self, addr, timeout=5):
        result = self.run('--is-connected', addr, timeout=timeout)
        if result not in ('0', '1'):
            raise BluetoothError('Cannot read connection state for ' + addr)
        return result == '1'

    def preflight(self):
        if self.run('--power') != '1':
            raise BluetoothError('Bluetooth is off or inaccessible. If it is on, enable '
                'Bluetooth access for your terminal app, or for '
                '/usr/libexec/sshd-keygen-wrapper when running over SSH.')
        if '--unpair' not in self.run('--help'):
            raise BluetoothError('Update blueutil first: brew upgrade blueutil')
        self.paired()

    def missing_inputs(self, devices):
        # These IDs were read from this user's Magic Keyboard and Trackpad.
        # A Bluetooth ACL link alone does not prove the HID input is registered.
        products = {'keyboard': 0x0320, 'trackpad': 0x0324}
        try:
            result = subprocess.run(['/usr/bin/hidutil', 'list', '--ndjson'],
                capture_output=True, text=True, timeout=5, check=True)
            inputs = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise BluetoothError('Cannot verify macOS input registration: ' + str(exc))
        return [name for name in devices if not any(
            item.get('Transport') == 'Bluetooth' and
            item.get('ProductID') == products[name] and
            item.get('PrimaryUsagePage') in (1, 13)
            for item in inputs)]

    def release(self, addr):
        # Forget while connected so the accessory receives the link-key removal.
        changed = addr in self.paired()
        if changed:
            self.run('--unpair', addr)
        if self.connected(addr):
            changed = True
            self.run('--disconnect', addr)
        if self.connected(addr) or addr in self.paired():
            raise BluetoothError('Device did not release: ' + addr)
        return changed

    def connect(self, addr, timeout):
        if self.connected(addr):
            return
        deadline = time.monotonic() + timeout
        # A valid bond should be reused. Forgetting it before trying to connect
        # forces fresh pairing and can leave a working accessory stranded.
        if addr in self.paired():
            try:
                self.run('--connect', addr, timeout=min(15, timeout))
                if self.connected(addr):
                    return
            except BluetoothError:
                pass
            self.run('--unpair', addr)  # clear the bond only after reuse fails
        last_error = 'Accessory has not answered.'
        while time.monotonic() < deadline:
            try:
                remaining = deadline - time.monotonic()
                if self.connected(addr, timeout=max(0.1, min(5, remaining))):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if addr not in self.paired():
                    # Pairing can request a PIN or confirmation. Inherit the
                    # terminal so those prompts and responses are visible, and
                    # let this attempt use the remaining user-selected budget.
                    self.run('--pair', addr, timeout=remaining, interactive=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.run('--connect', addr, timeout=remaining)
                remaining = deadline - time.monotonic()
                if self.connected(addr, timeout=max(0.1, min(5, remaining))):
                    return
            except BluetoothError as exc:
                last_error = str(exc)
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise BluetoothError('%s: %s Power-cycle the accessory and retry. '
                             'Make sure it was released on the other Mac.' % (addr, last_error))


def execute(bt, devices, command, timeout, dry_run=False):
    bt.preflight()
    if dry_run or command == 'status':
        for name, addr in devices.items():
            print('%-9s %s  %s' % (name + ':', addr,
                  'connected' if bt.connected(addr) else 'disconnected'))
        if dry_run:
            print('Would %s these two accessories on this Mac. No Bluetooth changes made.' % command)
        return
    if command == 'release':
        errors = []
        for name, addr in devices.items():
            try:
                if bt.release(addr):
                    print('Released ' + name + '.', flush=True)
                else:
                    print(name.capitalize() + ' was already released on this Mac.', flush=True)
            except BluetoothError as exc:
                errors.append(name + ': ' + str(exc))
        if errors:
            raise BluetoothError('\n'.join(errors))
        verify_released(bt, devices)
        print('This Mac has released the selected accessories. Run ./connect on the destination Mac.')
        return
    print('Connecting on this Mac. Tap a key and touch the trackpad; '
          'turn them off/on if needed.', flush=True)
    errors = []
    # Serial pairing gives interactive PIN/confirmation requests exclusive use
    # of the terminal instead of having two pairers compete for the same input.
    for name, addr in devices.items():
        print('Connecting ' + name + '; respond to any pairing prompt below.', flush=True)
        try:
            bt.connect(addr, timeout)
        except BluetoothError as exc:
            errors.append(name + ': ' + str(exc))
    if errors:
        raise BluetoothError('\n'.join(errors))
    print('Checking that Bluetooth and macOS input connections stay ready...', flush=True)
    verify_connected(bt, devices)
    for name in devices:
        print('Connected ' + name + ' (input registered).', flush=True)
    print('Selected accessories are connected to this Mac.')


def verify_connected(bt, devices, seconds=5, input_timeout=10):
    deadline = time.monotonic() + input_timeout
    stable_since = None
    while True:
        missing = [name for name, addr in devices.items() if not bt.connected(addr)]
        if missing:
            raise BluetoothError(', '.join(missing) + ' disconnected during verification. '
                'Release on the other Mac, power-cycle the accessory, then retry. '
                'Use --device trackpad or --device keyboard to isolate it.')
        missing_inputs = bt.missing_inputs(devices)
        now = time.monotonic()
        if missing_inputs:
            stable_since = None
            if now >= deadline:
                raise BluetoothError(', '.join(missing_inputs) + ' has a Bluetooth link but no '
                    'macOS input registration. Turn it off/on and retry, or pair once by cable.')
        else:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= seconds:
                return
        time.sleep(0.3)


def verify_released(bt, devices, seconds=3):
    # macOS can briefly hide a device while unpairing, then reconnect it. A
    # single immediate snapshot therefore does not prove that release held.
    deadline = time.monotonic() + seconds
    while True:
        paired = bt.paired()
        reclaimed = [name for name, addr in devices.items()
                     if bt.connected(addr) or addr in paired]
        if reclaimed:
            raise BluetoothError(', '.join(reclaimed) + ' reconnected or remained paired after release. '
                'Quit Magic Switch and other automatic reconnect utilities on both Macs, then retry.')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.3, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('release', 'connect', 'status'))
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().with_name('devices.json'))
    parser.add_argument('--timeout', type=int, default=60, help='pairing retry window per accessory (default: 60 seconds)')
    parser.add_argument('--dry-run', action='store_true', help='read status without changing Bluetooth connections')
    parser.add_argument('--device', choices=('all', 'keyboard', 'trackpad'), default='all',
                        help='operate on one accessory or both (default: all)')
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 180:
        parser.error('--timeout must be between 5 and 180 seconds')
    try:
        print('Running on ' + socket.gethostname(), flush=True)
        devices = load_devices(args.config.expanduser())
        if args.device != 'all':
            devices = {args.device: devices[args.device]}
        with operation_lock():
            execute(Bluetooth(), devices, args.command, args.timeout, args.dry_run)
        return 0
    except (BluetoothError, OSError) as exc:
        print(args.command + ': ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInterrupted. Inspect status, then rerun ./connect on the destination Mac.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
