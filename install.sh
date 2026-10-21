#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${SWISH_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$install_dir"
if [ -e "$install_dir/switch" ] && ! cmp -s "$project_dir/switch" "$install_dir/switch"; then
    echo "Refusing to replace an existing switch command at $install_dir/switch" >&2
    echo "Move it aside first if you want to install Swish there." >&2
    exit 1
fi
install -m 755 "$project_dir/switch" "$install_dir/switch"
echo "Installed $install_dir/switch"
case ":$PATH:" in
    *":$install_dir:"*) ;;
    *) printf '\nAdd this to your shell profile:\n  export PATH="%s:$PATH"\n' "$install_dir" ;;
esac
echo 'Next: switch init --peer yourname@Other-Mac.local'
