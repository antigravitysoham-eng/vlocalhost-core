#!/usr/bin/env bash
# Vlocalhost.AI — Linux installer.
#
# Unpacks the bundle wherever you want it and adds a menu entry. Needs no root
# and no system Python: the bundle carries its own interpreter.
#
#     ./install.sh                      # ~/.local/opt/vlocalhost
#     ./install.sh --prefix /opt/vl     # anywhere you can write
#     ./install.sh --uninstall
#
# Your meeting notes live in $XDG_DATA_HOME/vlocalhost (by default
# ~/.local/share/vlocalhost) and are NEVER touched by this script, including on
# uninstall. The program deliberately does NOT install there: that is the app's
# data directory, and this script removes its prefix before copying.

set -euo pipefail

APP_NAME="Vlocalhost.AI"
# The program. Not $XDG_DATA_HOME/vlocalhost — see DATA_DIR below.
DEFAULT_PREFIX="$HOME/.local/opt/vlocalhost"
# Must match integrations/store.py:data_dir().
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/vlocalhost"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/512x512/apps"
DESKTOP_FILE="$DESKTOP_DIR/vlocalhost.desktop"
SUPPORT_URL="https://antigravitysoham-eng.github.io/vlocalhost-ai/support/"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$DEFAULT_PREFIX"
UNINSTALL=0

say()  { printf '   %s\n' "$*"; }
die()  { printf '\n   [X] %s\n\n   Get help: %s\n\n' "$*" "$SUPPORT_URL" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)     PREFIX="${2:-}"; shift 2 || die "--prefix needs a path" ;;
    --prefix=*)   PREFIX="${1#*=}"; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    -h|--help)    sed -n '2,14p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)            die "Unknown option: $1" ;;
  esac
done

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  rm -rf "$PREFIX"
  rm -f  "$DESKTOP_FILE" "$HOME/Desktop/vlocalhost.desktop" \
         "$ICON_DIR/vlocalhost.png"
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$DESKTOP_DIR" || true
  printf '\n   %s removed.\n\n' "$APP_NAME"
  say "Your notes and settings were kept, in:"
  say "  $DATA_DIR"
  printf '\n'
  exit 0
fi

# ------------------------------------------------------------------ install
printf '\n   %s\n   Meeting notes that never leave your machine\n' "$APP_NAME"
printf '   ==========================================\n\n'

[ -d "$HERE/runtime" ] && [ -d "$HERE/app" ] || \
  die "Run this from inside the unpacked bundle (it needs runtime/ and app/)."

# A running copy would have its files replaced underneath it.
if pgrep -f "vlocalhost.py" >/dev/null 2>&1; then
  die "Vlocalhost appears to be running. Close it and try again."
fi

say "[1/4] Checking the destination ..."

# Installing on top of the data directory would put notes inside the program
# folder, which this script deletes before copying. Refuse rather than destroy.
canon() { printf '%s' "$(cd "$(dirname "$1")" 2>/dev/null && pwd)/$(basename "$1")"; }
if [ "$(canon "$PREFIX")" = "$(canon "$DATA_DIR")" ] || \
   case "$(canon "$PREFIX")" in "$(canon "$DATA_DIR")"/*) true ;; *) false ;; esac; then
  die "That prefix is where your notes are kept ($DATA_DIR).
       Installing there would delete them. Pick another --prefix."
fi

parent="$(dirname "$PREFIX")"
mkdir -p "$parent" 2>/dev/null || die "Cannot create $parent — pick another --prefix."
[ -w "$parent" ] || die "No permission to write to $parent — pick another --prefix."

# ~470 MB unpacked. Fail now with a clear reason rather than halfway through.
need_kb=480000
free_kb="$(df -Pk "$parent" | awk 'NR==2 {print $4}')"
[ "${free_kb:-0}" -ge "$need_kb" ] || \
  die "Not enough space in $parent ($(( free_kb / 1024 )) MB free, ~470 MB needed)."

say "[2/4] Installing to $PREFIX ..."
rm -rf "$PREFIX"
mkdir -p "$PREFIX"
cp -a "$HERE/runtime" "$HERE/app" "$PREFIX/"
[ -f "$HERE/manifest.json" ] && cp -a "$HERE/manifest.json" "$PREFIX/"
chmod +x "$PREFIX/runtime/bin/python3" 2>/dev/null || true

PY="$PREFIX/runtime/bin/python3"
[ -x "$PY" ] || die "The bundled interpreter is missing or not executable."

say "[3/4] Checking it runs ..."
"$PY" -c "import faster_whisper, numpy, sounddevice" >/dev/null 2>&1 || {
  say "    Note: the audio backend needs PortAudio. On Debian/Ubuntu:"
  say "      sudo apt install libportaudio2"
  say "    On Fedora/RHEL:  sudo dnf install portaudio"
}

say "[4/4] Adding a menu entry ..."
mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
[ -f "$PREFIX/app/assets/vlocalhost.png" ] && \
  cp -f "$PREFIX/app/assets/vlocalhost.png" "$ICON_DIR/vlocalhost.png"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Meeting notes that never leave your machine
Exec="$PY" "$PREFIX/app/vlocalhost.py"
Path=$PREFIX/app
Icon=vlocalhost
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

if [ -d "$HOME/Desktop" ]; then
  cp -f "$DESKTOP_FILE" "$HOME/Desktop/vlocalhost.desktop"
  chmod +x "$HOME/Desktop/vlocalhost.desktop"
  # Newer GNOME/KDE hide a launcher they consider untrusted.
  command -v gio >/dev/null 2>&1 && \
    gio set "$HOME/Desktop/vlocalhost.desktop" metadata::trusted true 2>/dev/null || true
fi

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

printf '\n   ==========================================\n'
printf '   Done. Look for %s in your applications.\n' "$APP_NAME"
printf '   ==========================================\n\n'
say "Summaries are optional and need Ollama (ollama.com)."
say "Without it you still get a full transcript of every meeting."
printf '\n'
