#!/bin/zsh
# Installs hme into ~/.local/share/hme (virtualenv) and links ~/.local/bin/hme.
set -e
HERE="${0:A:h}"
VENV="$HOME/.local/share/hme/venv"
BIN="$HOME/.local/bin"

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$HERE/requirements.txt"
cp "$HERE/hme.py" "$HERE/hme_helper.py" "$HOME/.local/share/hme/"

mkdir -p "$BIN"
cat > "$BIN/hme" <<EOF
#!/bin/zsh
exec "$VENV/bin/python" "$HOME/.local/share/hme/hme.py" "\$@"
EOF
chmod +x "$BIN/hme"

echo "Installed: $BIN/hme"

# Mac app
"$HERE/app/build.sh"
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/Hide My Email.app"
cp -R "$HERE/app/Hide My Email.app" "$HOME/Applications/"
echo "Installed: ~/Applications/Hide My Email.app"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Add this to ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo "Next: hme login you@icloud.com"
