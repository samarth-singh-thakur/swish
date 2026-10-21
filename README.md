# Swish

Move a Magic Keyboard and Magic Trackpad between two Macs from your terminal.

```sh
switch here     # bring both accessories to this Mac
switch send     # send both accessories to the other Mac
switch status   # show connections on both Macs
```

Swish releases the accessories on their current Mac, then pairs and connects them
on the destination over SSH. A physical power-cycle or pairing prompt can still
be required. In the setup this was tested with, both accessories needed to be
turned off and on during a transfer.

## Requirements

- Two awake Macs with Bluetooth enabled and network access between them.
- Python 3.9+ and [blueutil](https://github.com/toy/blueutil) on both Macs.
- SSH access to the other Mac for the account running Swish.
- Bluetooth permission for your terminal and the remote SSH session.

On both Macs:

```sh
brew install blueutil python
```

## Install on both Macs

Clone the repository, then install the current scripts outside the Desktop folder
so remote commands do not need access to Desktop files:

```sh
git clone https://github.com/samarth-singh-thakur/swish.git
cd swish
mkdir -p "$HOME/Applications/swish" "$HOME/.local/bin"
cp release connect bluetooth_devices.py handoff ssh_handoff.py "$HOME/Applications/swish/"
cp ssh-work-package/switch "$HOME/Applications/swish/switch"
chmod +x "$HOME/Applications/swish/"{release,connect,handoff,switch}
```

For a first installation, create local configurations from the examples:

```sh
cp -n devices.example.json "$HOME/Applications/swish/devices.json"
cp -n handoff.example.json "$HOME/Applications/swish/handoff.json"
```

Edit those two JSON files on each Mac:

- `devices.json`: replace the example addresses with your keyboard and trackpad
  addresses. Use the same accessory addresses on both Macs. Read addresses with
  `blueutil --paired` on the Mac currently paired with the accessories.
- `handoff.json`: set `peer` to the other Mac's SSH account and hostname, and
  `remote_dir` to the absolute Swish installation path on that other Mac.

For example, if the other Mac's login name is `alex`:

```json
{
  "peer": "alex@Other-Mac.local",
  "remote_dir": "/Users/alex/Applications/swish"
}
```

An optional `identity_file` property selects an existing SSH key. Otherwise Swish
uses your normal SSH configuration and agent. No SSH password is stored. Local
configuration files are ignored by Git; the repository contains examples only.

Install the launcher in your PATH:

```sh
ln -s "$HOME/Applications/swish/switch" "$HOME/.local/bin/switch"
```

If `switch` already exists there, inspect it before replacing it. Ensure
`$HOME/.local/bin` is in your shell's PATH. For zsh, add this to `~/.zshrc` if needed:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Start a new terminal or run `source ~/.zshrc` and `rehash`. Use `command -v switch`
to check which launcher your terminal finds.

## SSH and Bluetooth access

On the Mac accepting SSH, enable **System Settings → General → Sharing → Remote
Login** for your account. Verify the host fingerprint with a normal SSH login
before running Swish:

```sh
ssh yourname@Other-Mac.local hostname
```

An interactive handoff can prompt for your SSH password once and reuses the
connection for the remaining steps. Key-based authentication also works. Swish
uses IPv4 and normal SSH host verification.

On each Mac, allow Bluetooth access for your terminal under **System Settings →
Privacy & Security → Bluetooth**. SSH sessions also need Bluetooth access; macOS
may attribute this to `/usr/libexec/sshd-keygen-wrapper`.

Only one SSH direction is needed if you always start commands on the same Mac:
`switch here` and `switch send` both use that Mac's outgoing SSH connection. To
start commands on either Mac, configure SSH access in both directions.

Quit other automatic accessory switching apps while using Swish; their reconnect
features can reclaim an accessory immediately after release.

## Use

```sh
switch here
switch send
switch status
switch                    # toggle based on current connections
switch send --dry-run     # check setup and direction without changing Bluetooth
switch here --device trackpad
switch here --timeout 90
```

`here` always means the Mac running the command. In an SSH shell, that is the
remote Mac. The handoff prints both hostnames so you can check the direction.

Tap a key and touch the trackpad during connection. Turn each accessory off and
on when needed, and respond to pairing prompts. The timeout applies per accessory.
If pairing fails, the command prints a manual command to resume connection on the
destination. A cable or built-in inputs can be used for recovery.

The `handoff` script checks SSH, Bluetooth access, device addresses, and script
paths before releasing accessories. Locks on both Macs prevent opposing handoffs.
The destination must report both a Bluetooth connection and registered input
devices before connection is reported as successful. Input registration currently
checks the tested Magic Keyboard (product ID `0x0320`) and Magic Trackpad (`0x0324`);
other models may need changes to that check.

## Manual release and connect

These commands remain available independently of SSH:

```sh
cd "$HOME/Applications/swish"
./release
./connect
./connect --device trackpad --timeout 90
./release --dry-run
```

Run `release` on the source Mac, then `connect` on the destination. Release removes
the selected accessories' pairing records; unrelated Bluetooth devices are left
alone. Existing working pairing records are reused during connection.

## Troubleshooting

- SSH timeout: check the address, network, sleep state, and Remote Login setting.
- Hostname resolution failure: try `switch status --peer yourname@192.0.2.10`,
  replacing the example address with the other Mac's current IP.
- Bluetooth abort or Bluetooth reported unavailable: check Bluetooth permissions
  for the app or SSH session running the command.
- Accessories immediately reconnect to the old Mac: quit automatic switching apps
  and check whether a data cable is connected to that Mac.
- Bluetooth reports connected but input does not work: wake or power-cycle the
  accessory and retry; the command checks input registration before success.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests simulate Bluetooth and SSH; they do not change hardware connections.

`ssh_handoff.py` coordinates the manual helpers. `ssh-work-package/switch` is the
current global launcher. The root-level `switch`, `install.sh`, `repair_transport.py`,
and [README-ssh.md](README-ssh.md) are earlier prototype tools and notes; use the
installation steps above for `switch here` and `switch send`.
