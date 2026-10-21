"""Apply the narrow transport repair to an existing MagicSwitch installation."""
from pathlib import Path
import shutil
import sys

path = Path(sys.argv[1])
source = path.read_text()
changes = [
    ("ssh = ['ssh', '-T',", "ssh = ['ssh', '-4', '-T',"),
    ("self.blueutil = shutil.which('blueutil')", "self.blueutil = next((p for p in [shutil.which('blueutil'), '/opt/homebrew/bin/blueutil', '/usr/local/bin/blueutil'] if p and os.access(p, os.X_OK)), None)"),
    ("Bluetooth is off or unavailable. Turn it on first.", "Bluetooth is off or unavailable. If it is on, grant Bluetooth access to /usr/libexec/sshd-keygen-wrapper on this Mac for SSH commands, or to your terminal app for local commands."),
]
updated = source
for before, after in changes:
    if after in updated:
        continue
    if updated.count(before) != 1:
        raise SystemExit('Unexpected installed source; refusing to patch: ' + before)
    updated = updated.replace(before, after, 1)
compile(updated, str(path), 'exec')
if updated != source:
    backup = path.with_name(path.name + '.before-ipv4')
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(updated)
    print('Updated ' + str(path) + '; backup: ' + str(backup))
else:
    print('Already repaired: ' + str(path))
