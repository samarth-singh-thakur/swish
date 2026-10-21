# Installation and configuration

Set up Swish on both Macs. This guide calls them **Mac A** and **Mac B**; replace
the example usernames, hostnames, and Bluetooth addresses with your own values.

After setup, these commands work from any terminal directory:

```sh
switch here     # bring the keyboard and trackpad to this Mac
switch send     # send them to the other Mac
switch status   # check both Macs without switching
```

Some Magic accessories need to be turned off and on during each transfer. Swish
prints a reminder when connection begins.

## 1. Install the scripts on both Macs

Install [Homebrew](https://brew.sh) if it is not already available. On each Mac:

```sh
brew install blueutil python
git clone https://github.com/samarth-singh-thakur/swish.git
cd swish
mkdir -p "$HOME/Applications/swish" "$HOME/.local/bin"
cp release connect bluetooth_devices.py handoff ssh_handoff.py "$HOME/Applications/swish/"
cp ssh-work-package/switch "$HOME/Applications/swish/switch"
chmod +x "$HOME/Applications/swish/"{release,connect,handoff,switch}
cp -n devices.example.json "$HOME/Applications/swish/devices.json"
cp -n handoff.example.json "$HOME/Applications/swish/handoff.json"
```

If you have already cloned the repository, run `git pull` inside your checkout
instead of cloning again. The `cp -n` commands preserve existing configuration.
Use the launcher from `ssh-work-package/switch`; the root-level `switch` and
`install.sh` belong to an older prototype.

## 2. Find the keyboard and trackpad addresses

On the Mac where both accessories are currently paired, allow Bluetooth access
for your terminal under **System Settings → Privacy & Security → Bluetooth**.
Then list paired devices:

```sh
blueutil --paired
```

Find the entries by accessory name. Example output, with example addresses:

```text
address: aa-bb-cc-dd-ee-01, connected, paired, name: "Magic Keyboard"
address: aa-bb-cc-dd-ee-02, connected, paired, name: "Magic Trackpad"
```

Copy each accessory's `address` value. These are the accessories' Bluetooth
addresses, not the Mac's Bluetooth address, Wi-Fi address, or IP address. If an
accessory is missing, pair it using macOS Bluetooth settings, then list again.
See [blueutil's command reference](https://github.com/toy/blueutil#usage).

On **both Macs**, edit the installed device configuration:

```sh
nano "$HOME/Applications/swish/devices.json"
```

Replace the example values with the addresses you found:

```json
{
  "keyboard": "AA:BB:CC:DD:EE:01",
  "trackpad": "AA:BB:CC:DD:EE:02"
}
```

Use the **same two addresses on both Macs**. Colons or hyphens are accepted, and
letter case does not matter. Keep the JSON keys `keyboard` and `trackpad`. In nano,
save with **Control+O**, press **Return**, then exit with **Control+X**.

Check local configuration and Bluetooth access without releasing anything:

```sh
"$HOME/Applications/swish/release" --dry-run
```

## 3. Find each Mac's SSH address and installation path

Run these commands separately on **each Mac**:

```sh
printf 'SSH address: %s@%s.local\n' "$(id -un)" "$(scutil --get LocalHostName)"
printf 'Swish path: %s/Applications/swish\n' "$HOME"
```

Record both results. For example:

| Mac | SSH address | Swish path |
| --- | --- | --- |
| Mac A | `alex@Mac-A.local` | `/Users/alex/Applications/swish` |
| Mac B | `jordan@Mac-B.local` | `/Users/jordan/Applications/swish` |

The SSH username is the account's short login name, returned by `id -un`, rather
than its display name or Apple Account email. The `.local` hostname identifies
the Mac on the local network. You can also find it in **System Settings → General
→ Sharing**. [Apple's hostname guide](https://support.apple.com/guide/mac-help/mchlp1177/mac)
explains where it appears.

If `.local` does not resolve, use the Mac's current IP address instead. Find the
address in **System Settings → Network → Wi-Fi → Details → TCP/IP**. For example,
`jordan@192.168.1.20` uses the same username with an IP instead of a hostname.
IP addresses may change when reconnecting or changing networks.

## 4. Enable and test SSH

On the Mac accepting connections, open **System Settings → General → Sharing →
Remote Login**, turn it on, and allow the account from step 3. See
[Apple's Remote Login instructions](https://support.apple.com/guide/mac-help/mchlp1066/mac).

From **Mac A**, test access to **Mac B**, replacing the example address:

```sh
ssh -4 -o ConnectTimeout=5 jordan@Mac-B.local hostname
```

Verify the host fingerprint on first connection, then enter Mac B's login
password if prompted. Success prints Mac B's hostname. Your password does not
appear while typing. Swish can use this password interactively; it does not store
the password.

To run Swish from either Mac, also enable Remote Login on Mac A and test the
reverse direction from **Mac B**:

```sh
ssh -4 -o ConnectTimeout=5 alex@Mac-A.local hostname
```

If you always run Swish on Mac A, only Mac A → Mac B SSH access is needed. Both
`switch here` and `switch send` work over that same outgoing connection.

## 5. Set the SSH peer in handoff.json

On each Mac, open:

```sh
nano "$HOME/Applications/swish/handoff.json"
```

On **Mac A**, point to **Mac B**, using Mac B's results from step 3:

```json
{
  "peer": "jordan@Mac-B.local",
  "remote_dir": "/Users/jordan/Applications/swish"
}
```

On **Mac B**, point back to **Mac A**:

```json
{
  "peer": "alex@Mac-A.local",
  "remote_dir": "/Users/alex/Applications/swish"
}
```

| Field | What to enter |
| --- | --- |
| `peer` | The **other Mac's** `username@hostname` or `username@IP`. Omit the word `ssh`. |
| `remote_dir` | The **other Mac's** absolute Swish path. Use `/Users/...`, not `~` or `$HOME`. |
| `identity_file` | Optional: an existing private SSH key path on **this Mac**, already authorized on the other Mac. |

For an existing SSH key, the optional setting looks like this on Mac A:

```json
{
  "peer": "jordan@Mac-B.local",
  "remote_dir": "/Users/jordan/Applications/swish",
  "identity_file": "~/.ssh/swish_ed25519"
}
```

Test that key with `ssh -i ~/.ssh/swish_ed25519 jordan@Mac-B.local hostname`.
Use the private key file, rather than its `.pub` file. If your key has a
passphrase, unlock it in your SSH agent before running Swish. Without
`identity_file`, Swish uses your normal SSH configuration, agent, or password.
Keep private keys and your per-Mac JSON configuration out of Git.

## 6. Allow Bluetooth access over SSH

On both Macs, enable Bluetooth and allow your terminal app under **System
Settings → Privacy & Security → Bluetooth**. The SSH session on the remote Mac
also needs Bluetooth access; macOS may list it as `sshd-keygen-wrapper`
(`/usr/libexec/sshd-keygen-wrapper`). Enable that entry when shown.

From Mac A, check Mac B's Bluetooth access:

```sh
ssh -4 jordan@Mac-B.local '/opt/homebrew/bin/blueutil --power'
```

Expected output is `1`. On an Intel Mac, use `/usr/local/bin/blueutil` if that is
where Homebrew installed it. Run `command -v blueutil` directly on the remote Mac
to check its path. An abort or permission error requires fixing Bluetooth access
on the Mac executing blueutil. Repeat in reverse if running Swish from both Macs.

Quit other automatic accessory switching apps so their reconnect features do not
reclaim accessories during release.

## 7. Make switch available from any directory

On both Macs:

```sh
ln -s "$HOME/Applications/swish/switch" "$HOME/.local/bin/switch"
```

If the destination already exists, inspect `ls -l "$HOME/.local/bin/switch"`
before replacing it. An existing symlink to the same script needs no change.

For zsh, open `~/.zshrc` and add this line if it is not already present:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Reload and check command lookup:

```sh
source ~/.zshrc
rehash
command -v switch
switch --help
```

The path should be your `~/.local/bin/switch`, and help should list `here`, `send`,
and `status`. If help instead lists `setup`, `devices`, or `toggle` as subcommands,
an older `switch` command is taking precedence. Check `type -a switch`, remove
an old shell alias or function if present, and ensure `~/.local/bin` comes first
in PATH after your other shell setup.

## 8. Verify setup and switch

```sh
switch status
switch send --dry-run
```

Status should print both Mac hostnames and each accessory's connection state.
The dry run checks the proposed direction without changing Bluetooth connections.

Then use:

```sh
switch send                  # move to the other Mac
switch here                  # bring back to this Mac
switch here --device trackpad
switch here --timeout 90
```

Power-cycle accessories when prompted and respond to pairing prompts. In an SSH
shell, `here` refers to the Mac hosting that shell, not the Mac at your desk.

## SSH troubleshooting

If SSH fails, run these **on the Mac you are trying to connect to**:

```sh
sudo systemsetup -getremotelogin
nc -G 3 -vz 127.0.0.1 22
```

`Remote Login: Off` or a refused local connection means incoming SSH is not
available there. Enable Remote Login before retesting. If company management
locks the setting or repeatedly turns it off, ask your IT administrator.

If local port 22 succeeds, test from the initiating Mac:

```sh
nc -G 3 -vz Mac-B.local 22
ssh -4 -o ConnectTimeout=5 jordan@Mac-B.local hostname
```

Replace the examples with the target Mac's address. A timeout points to address,
network, sleep, or filtering issues. `Permission denied` means authentication
failed: check the login name, allowed Remote Login users, password, and SSH key.
Try the current IP if the hostname does not resolve. One-time override:

```sh
switch status --peer jordan@192.168.1.20
```

If that succeeds, update `peer` in `handoff.json`. The override changes the SSH
address only; `remote_dir` must still point to the correct remote installation.
