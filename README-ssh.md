# Swish coordinated SSH CLI

These are historical notes for the earlier standalone `switch` prototype.
For the current `switch here` / `switch send` launcher, use [README.md](README.md).
The setup and diagnostic commands below may differ from the prototype source.

Type `switch` to move your Magic Keyboard and Magic Trackpad to the other Mac.
Type it again on either Mac to move them back. The command checks where both
accessories are connected, releases their pairing on the current Mac, then pairs
and connects them on the destination over SSH.

This is a real Bluetooth handoff. It needs two awake Macs on a network where
they can reach each other over SSH. It does not require the same Apple Account.

## Set up both Macs

1. Install [Homebrew](https://brew.sh) if needed, then install the dependencies:

   ```sh
   brew install blueutil python
   ```

2. Enable **System Settings → General → Sharing → Remote Login** on both Macs.
   Allow your user account. Set up key-based SSH in both directions using an
   existing SSH key or a new dedicated key:

   ```sh
   # Run on each Mac, replacing the destination with the OTHER Mac.
   ssh-keygen -t ed25519 -f ~/.ssh/swish_ed25519
   ssh-copy-id -i ~/.ssh/swish_ed25519.pub yourname@Other-Mac.local
   ```

   If `ssh-copy-id` is unavailable, `brew install ssh-copy-id`. Add a host entry
   to `~/.ssh/config` so SSH uses the dedicated key (use the actual hostname):

   ```sshconfig
   Host Other-Mac.local
       IdentityFile ~/.ssh/swish_ed25519
       IdentitiesOnly yes
   ```

   If your key has a passphrase, load it into your SSH agent using
   `ssh-add --apple-use-keychain ~/.ssh/swish_ed25519`.
   Connect manually once to verify the host fingerprint and test access:

   ```sh
   ssh yourname@Other-Mac.local true
   ssh -o BatchMode=yes yourname@Other-Mac.local true
   ```

   Swish uses your normal SSH configuration and host verification. Host aliases
   also work, including custom ports configured in `~/.ssh/config`.

3. Allow Bluetooth access to the terminal you use under
   **System Settings → Privacy & Security → Bluetooth**. The SSH session also
   needs Bluetooth permission on each Mac. On recent macOS versions this may
   be attributed to `/usr/libexec/sshd-keygen-wrapper`. Use the system file
   picker (⌘⇧G) if you need to locate that executable. Check access over SSH:

   ```sh
   ssh yourname@Other-Mac.local '/opt/homebrew/bin/blueutil --power'
   ```

   For Intel Homebrew, use `/usr/local/bin/blueutil` instead. Power should
   report `1`; `switch doctor` also checks that device queries work.

4. Copy this project to each Mac and install the command:

   ```sh
   ./install.sh
   ```

   This installs `~/.local/bin/switch`. If your shell cannot find it, add
   `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` and start a new terminal.
   The script refuses to overwrite an unrelated existing `switch` command.

5. On the Mac currently connected to both accessories, initialize:

   ```sh
   switch init --peer yourname@Other-Mac.local
   switch devices
   ```

   Setup detects a single paired keyboard and trackpad by their names. If
   there are several or you renamed them, provide their Bluetooth addresses:

   ```sh
   switch init --peer yourname@Other-Mac.local \
     --keyboard AA:BB:CC:DD:EE:01 \
     --trackpad AA:BB:CC:DD:EE:02
   ```

   On the other Mac, run the same explicit command with the **same accessory
   addresses**, but point `--peer` back at the first Mac. Addresses can be read
   with `switch devices` while they are paired. Initialization with explicit
   addresses saves configuration without changing any Bluetooth connections.

6. On each Mac, verify setup before the first handoff:

   ```sh
   switch doctor
   switch --dry-run
   ```

   The remote Mac needs Python 3 and blueutil; Swish sends its backend through
   the SSH session, so remote commands do not depend on a shell alias or PATH
   entry for `switch`.

## Use

```sh
switch                  # toggle to the other Mac
switch status           # show where each accessory is connected
switch --to here        # bring both to the Mac running this command
switch --to other       # send both to its configured peer
switch --timeout 90     # allow a longer pairing retry window
switch --dry-run        # check the route without changing Bluetooth state
```

If accessories are split across Macs or both disconnected, select an explicit
destination with `--to`. A destination that already has both accessories is a
no-op. For a non-default configuration, put `--config /path/to/config.json`
before the subcommand. Use `switch init --force ...` to replace a configuration.

The default config is `~/.config/swish/config.json`, written with user-only file
permissions. No password is stored. No background service is required.

## Reliability and recovery

Magic accessory behavior varies by model and macOS version. A disconnect alone
can leave an accessory tied to its old host. Swish unpairs only the two selected
accessories, clears stale records on the destination, and retries pairing and
connection for up to 60 seconds by default. It never turns off the Bluetooth
radio or unpairs your other devices.

Tap a key and move the trackpad during a handoff. If pairing stalls, turn each
accessory off and on. Some models require a pairing code or a cable; a CLI
cannot guarantee a completely hands-free switch for every Magic device.
Touch ID may also need separate setup on the destination Mac.

Both Macs are checked before the command disconnects anything. Locks on both
hosts prevent overlapping switches for the same user. During the bounded
handoff, Ctrl-C is deferred so it cannot interrupt the SSH protocol midway.
If pairing fails, Swish attempts to restore the original connections and exits
with an error. Recovery is best effort: a network failure, loss of Bluetooth
permission, or an asleep accessory can prevent it. A built-in keyboard and
trackpad or a USB cable provide recovery; then run `switch --to here`.

Reported success means both accessories were observed connected on the
destination at the end of the handoff. Hardware testing on your own pair of
Macs is still necessary.

Bluetooth backend: [blueutil](https://github.com/toy/blueutil).
Relevant handoff behavior and SSH permission notes:
[monnect](https://github.com/Eimen2018/monnect) and
[blueutil's cross-Mac keyboard discussion](https://github.com/toy/blueutil/issues/46).

## Development

The executable uses only the Python standard library (Python 3.9+). Run:

```sh
python3 -m unittest discover -s tests -v
```

Tests use simulated Bluetooth devices and SSH; they do not touch real devices.
