#!/usr/bin/env python3
"""Coordinate the working release/connect commands over SSH."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shlex
import socket
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parent
MARKER = 'SWISH_JSON:'

PROBE = r'''
import json, os, socket, sys
from pathlib import Path
try:
    folder = Path(sys.argv[1])
    selected = sys.argv[2]
    for name in ('release', 'connect'):
        if not os.access(folder / name, os.X_OK):
            raise RuntimeError('Missing executable: ' + str(folder / name))
    sys.path.insert(0, str(folder))
    import bluetooth_devices as manual
    with manual.operation_lock():
        devices = manual.load_devices(folder / 'devices.json')
        if selected != 'all':
            devices = {selected: devices[selected]}
        bt = manual.Bluetooth()
        bt.preflight()
        result = {'host': socket.gethostname(), 'devices': devices,
                  'connected': {name: bt.connected(addr) for name, addr in devices.items()}}
    print('SWISH_JSON:' + json.dumps(result), flush=True)
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
'''

REMOTE_LOCK = r'''
import fcntl, sys
from pathlib import Path
folder = Path.home() / '.cache' / 'swish-ssh'
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
with (folder / 'handoff.lock').open('a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('BUSY', flush=True)
        sys.exit(1)
    print('READY', flush=True)
    sys.stdin.read()
'''


class HandoffError(Exception):
    pass


def peer_address(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*', value):
        raise HandoffError('Use an SSH alias or user@hostname/IP without spaces or SSH options.')
    return value


def load_config(path, peer_override=None):
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            raise HandoffError('handoff.json must contain a JSON object.')
        config['peer'] = peer_address(peer_override or config['peer'])
        remote = config['remote_dir']
        if not isinstance(remote, str) or not remote.startswith('/') or '\x00' in remote:
            raise HandoffError('remote_dir must be an absolute path on the other Mac.')
        identity = config.get('identity_file')
        if identity:
            identity = Path(identity).expanduser()
            if not identity.is_file():
                raise HandoffError('SSH key is missing: ' + str(identity))
            config['identity_file'] = str(identity)
        return config
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HandoffError('Cannot read handoff configuration %s: %s' % (path, exc))


def ssh_args(config, interactive=False, master=False, allow_password=False):
    args = ['ssh', '-4', '-tt' if interactive else '-T',
            '-o', 'BatchMode=no' if allow_password else 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=2']
    if config.get('_control_path'):
        args += ['-o', 'ControlPath=' + config['_control_path'],
                 '-o', 'ControlMaster=' + ('yes' if master else 'no'),
                 '-o', 'ControlPersist=no']
    if config.get('identity_file'):
        args += ['-i', config['identity_file'], '-o', 'IdentitiesOnly=yes']
    return args + ['--', peer_address(config['peer'])]


def python_command(source, *args):
    # The source and paths are shell-quoted independently. No user value becomes
    # executable shell source, including when a folder name contains spaces.
    invocation = shlex.join(['-u', '-c', source, *map(str, args)])
    return ('for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; '
            'do if [ -x "$p" ]; then exec "$p" ' + invocation +
            '; fi; done; echo "Python 3 is missing" >&2; exit 1')


@contextlib.contextmanager
def local_lock():
    folder = Path.home() / '.cache' / 'swish-ssh'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'handoff.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HandoffError('Another SSH handoff is running on this Mac.')
        yield


class RemoteLock:
    """A persistent SSH channel holds the peer lock until this handoff ends."""
    def __init__(self, config):
        self.config = config
        self.process = None
        self.errors = None
        self.control_directory = None

    def __enter__(self):
        self.errors = tempfile.TemporaryFile(mode='w+t')
        try:
            self.control_directory = tempfile.TemporaryDirectory(prefix='swish-ssh-', dir='/tmp')
            self.config['_control_path'] = str(Path(self.control_directory.name) / 'ssh')
            allow_password = sys.stdin.isatty()
            print('Opening SSH to ' + self.config['peer'] + '...', flush=True)
            self.process = subprocess.Popen(
                ssh_args(self.config, master=True, allow_password=allow_password) + [python_command(REMOTE_LOCK)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
                text=True, bufsize=1)
            if not select.select([self.process.stdout], [], [], 90 if allow_password else 20)[0]:
                raise HandoffError('SSH did not respond. Check the peer address and hotspot connectivity.')
            line = self.process.stdout.readline().strip()
            if line != 'READY':
                self.errors.seek(0)
                detail = self.errors.read().strip()
                if line == 'BUSY':
                    raise HandoffError('Another SSH handoff is running on the other Mac.')
                raise HandoffError('SSH setup failed: ' + (detail or line or 'connection closed'))
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process:
            with contextlib.suppress(BrokenPipeError, OSError):
                self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
        if self.errors:
            self.errors.close()
        self.config.pop('_control_path', None)
        if self.control_directory:
            self.control_directory.cleanup()

    def __exit__(self, *args):
        self.close()


class Endpoint:
    def __init__(self, directory, device='all', config=None):
        self.directory = Path(directory)
        self.device = device
        self.config = config

    def probe(self):
        if self.config:
            args = ssh_args(self.config) + [python_command(PROBE, self.directory, self.device)]
        else:
            args = [sys.executable, '-u', '-c', PROBE, str(self.directory), self.device]
        result = subprocess.run(args, text=True, capture_output=True, timeout=45)
        if result.returncode:
            raise HandoffError(result.stderr.strip() or 'Preflight failed on ' + str(self.directory))
        lines = [line[len(MARKER):] for line in result.stdout.splitlines() if line.startswith(MARKER)]
        try:
            value = json.loads(lines[-1])
            if not value['devices'] or set(value['devices']) != set(value['connected']):
                raise ValueError('invalid device snapshot')
            if any(type(v) is not bool for v in value['connected'].values()):
                raise ValueError('invalid connection state')
            return value
        except (ValueError, KeyError, TypeError, IndexError):
            raise HandoffError('Could not read device status from ' + str(self.directory))

    def run(self, command, timeout):
        args = [str(self.directory / command), '--device', self.device]
        if command == 'connect':
            args += ['--timeout', str(timeout)]
        if self.config:
            args = ssh_args(self.config, interactive=sys.stdin.isatty()) + [shlex.join(args)]
        # Inherit the terminal so remote/local pairing prompts remain interactive.
        result = subprocess.run(args, timeout=(timeout * (2 if self.device == 'all' else 1)) + 90)
        if result.returncode:
            raise HandoffError('%s failed (exit %s).' % (command, result.returncode))


def route(here, there, requested=None):
    if here['host'] == there['host']:
        raise HandoffError('The SSH peer points to this Mac.')
    if here['devices'] != there['devices']:
        raise HandoffError('The two Macs have different accessory addresses in devices.json.')
    local, remote = here['connected'], there['connected']
    if any(local[name] and remote[name] for name in local):
        raise HandoffError('Both Macs report the same accessory connected. Check status before switching.')
    if requested:
        return requested
    if all(local.values()) and not any(remote.values()):
        return 'other'
    if all(remote.values()) and not any(local.values()):
        return 'here'
    raise HandoffError('Accessories are split or disconnected. Choose --to here or --to other.')


def transfer(local, remote, destination, timeout):
    source, target = (local, remote) if destination == 'other' else (remote, local)
    # Refresh BOTH preflights before release, including authentication and paths.
    here, there = local.probe(), remote.probe()
    route(here, there, destination)
    chosen, opposite = (here, there) if destination == 'here' else (there, here)
    if all(chosen['connected'].values()) and not any(opposite['connected'].values()):
        print('Selected accessories are already connected on ' + chosen['host'] + '.')
        return
    print('Moving accessories to ' + chosen['host'] + '. Power-cycle each device when its connect prompt appears.', flush=True)
    source.run('release', timeout)
    try:
        target.run('connect', timeout)
        after = target.probe()
        if not all(after['connected'].values()):
            raise HandoffError('Destination lost a connection after connect finished.')
    except (HandoffError, subprocess.TimeoutExpired) as exc:
        raise HandoffError(str(exc) + '\nThe source was released. On the destination Mac, run ' +
            shlex.join([str(target.directory / 'connect'), '--device', target.device,
                        '--timeout', str(timeout)]) + ' to resume pairing.')
    print('Handoff complete: ' + chosen['host'] + '.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--to', choices=('here', 'other'), help='explicit destination; default toggles from observed connections')
    parser.add_argument('--device', choices=('all', 'keyboard', 'trackpad'), default='all')
    parser.add_argument('--timeout', type=int, default=90, help='connection timeout per accessory (default: 90 seconds)')
    parser.add_argument('--peer', help='override the SSH peer, e.g. samarth@172.20.10.3')
    parser.add_argument('--config', type=Path, default=BASE / 'handoff.json')
    parser.add_argument('--status', action='store_true', help='read both Macs without switching')
    parser.add_argument('--dry-run', action='store_true', help='check setup and show the route without changing Bluetooth')
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 180:
        parser.error('--timeout must be between 5 and 180 seconds')
    try:
        config = load_config(args.config.expanduser(), args.peer)
        local = Endpoint(BASE, args.device)
        remote = Endpoint(config['remote_dir'], args.device, config)
        with local_lock(), RemoteLock(config):
            here, there = local.probe(), remote.probe()
            print('This Mac: %s | Peer: %s' % (here['host'], there['host']), flush=True)
            if args.status:
                if here['devices'] != there['devices']:
                    raise HandoffError('The two Macs have different accessory addresses in devices.json.')
                for name in here['devices']:
                    print('%-9s here=%s  other=%s' % (name,
                        'connected' if here['connected'][name] else 'disconnected',
                        'connected' if there['connected'][name] else 'disconnected'))
                return 0
            destination = route(here, there, args.to)
            if args.dry_run:
                print('Would release on %s, then connect on %s. No Bluetooth changes made.' %
                    ((here['host'], there['host']) if destination == 'other' else (there['host'], here['host'])))
                return 0
            transfer(local, remote, destination, args.timeout)
        return 0
    except (HandoffError, OSError, subprocess.TimeoutExpired) as exc:
        print('handoff: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nhandoff: interrupted. Check ./handoff --status, then run ./connect on your destination Mac.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
