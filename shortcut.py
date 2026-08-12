"""Desktop and menu shortcuts, so the app opens with a double-click.

Installing from source leaves people with a virtualenv and a command to
remember, which is a poor way to meet an app. This module creates the ordinary
platform shortcut instead:

    Windows  a .lnk on the Desktop and in the Start menu
    macOS    an .app bundle in ~/Applications
    Linux    a .desktop entry in ~/.local/share/applications and on the Desktop

Run it from the installer, or any time afterwards::

    python vlocalhost.py --install-shortcut
    python vlocalhost.py --remove-shortcut

The shortcut points at the interpreter that created it — the virtualenv's, if
you are in one — so double-clicking uses the same environment the install put
the dependencies in. On Windows it points at ``pythonw.exe`` so no console
window opens behind the app.

Everything here is best-effort and reversible: no registry writes, no elevation,
nothing outside the user's own home directory.
"""

import os
import platform
import subprocess
import sys

APP_NAME = "Vlocalhost.AI"
DESCRIPTION = "Meeting notes that never leave your machine"

_HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(_HERE, "assets")


# ---------------------------------------------------------------------------
# what to launch
# ---------------------------------------------------------------------------
def _entry_script() -> str:
    """The script a shortcut should run.

    Whatever was used to start this process, so a Pro install shortcuts its
    own launcher rather than Core's. Falls back to Core's entry point when
    that cannot be determined (a REPL, say).
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and argv0.endswith(".py") and os.path.isfile(argv0):
        return os.path.abspath(argv0)
    return os.path.join(_HERE, "vlocalhost.py")


def _interpreter(windowless: bool = True) -> str:
    """The running interpreter — pythonw.exe on Windows, to skip the console."""
    exe = sys.executable or "python"
    if windowless and platform.system() == "Windows":
        head, tail = os.path.split(exe)
        candidate = os.path.join(head, tail.replace("python", "pythonw", 1))
        if tail.startswith("python") and os.path.isfile(candidate):
            return candidate
    return exe


def _icon(*names) -> str:
    for name in names:
        path = os.path.join(ASSETS, name)
        if os.path.isfile(path):
            return path
    return ""


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise OSError((proc.stderr or proc.stdout or "powershell failed").strip())
    return (proc.stdout or "").strip()


def _windows_install() -> list:
    target = _interpreter()
    script = _entry_script()
    workdir = os.path.dirname(script) or _HERE
    icon = _icon("vlocalhost.ico")

    # GetFolderPath, not %USERPROFILE%\Desktop: the Desktop is frequently
    # redirected into OneDrive, and writing to the wrong one looks like a
    # shortcut that silently never appeared.
    ps = f"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$targets = @(
  [Environment]::GetFolderPath('Desktop'),
  [Environment]::GetFolderPath('Programs')
)
foreach ($dir in $targets) {{
  if (-not $dir) {{ continue }}
  if (-not (Test-Path $dir)) {{ New-Item -ItemType Directory -Path $dir -Force | Out-Null }}
  $path = Join-Path $dir {_ps_quote(APP_NAME + '.lnk')}
  $lnk = $shell.CreateShortcut($path)
  $lnk.TargetPath = {_ps_quote(target)}
  $lnk.Arguments = '"' + {_ps_quote(script)} + '"'
  $lnk.WorkingDirectory = {_ps_quote(workdir)}
  $lnk.Description = {_ps_quote(DESCRIPTION)}
  {"$lnk.IconLocation = " + _ps_quote(icon + ",0") if icon else ""}
  $lnk.Save()
  Write-Output $path
}}
"""
    return [line for line in _run_powershell(ps).splitlines() if line.strip()]


def _windows_remove() -> list:
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
$targets = @(
  [Environment]::GetFolderPath('Desktop'),
  [Environment]::GetFolderPath('Programs')
)
foreach ($dir in $targets) {{
  $path = Join-Path $dir {_ps_quote(APP_NAME + '.lnk')}
  if (Test-Path $path) {{ Remove-Item $path -Force; Write-Output $path }}
}}
"""
    return [line for line in _run_powershell(ps).splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------
def _macos_app_dir() -> str:
    return os.path.expanduser(f"~/Applications/{APP_NAME}.app")


def _macos_install() -> list:
    bundle = _macos_app_dir()
    macos_dir = os.path.join(bundle, "Contents", "MacOS")
    res_dir = os.path.join(bundle, "Contents", "Resources")
    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(res_dir, exist_ok=True)

    icon = _icon("vlocalhost.icns")
    if icon:
        import shutil
        shutil.copyfile(icon, os.path.join(res_dir, "vlocalhost.icns"))

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>{APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>{APP_NAME}</string>
  <key>CFBundleIdentifier</key><string>ai.vlocalhost.app</string>
  <key>CFBundleExecutable</key><string>vlocalhost</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  {'<key>CFBundleIconFile</key><string>vlocalhost.icns</string>' if icon else ''}
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>{APP_NAME} transcribes your meetings on this device.</string>
</dict>
</plist>
"""
    with open(os.path.join(bundle, "Contents", "Info.plist"), "w",
              encoding="utf-8") as f:
        f.write(plist)

    launcher = os.path.join(macos_dir, "vlocalhost")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n"
                f'exec "{_interpreter()}" "{_entry_script()}" "$@"\n')
    os.chmod(launcher, 0o755)
    return [bundle]


def _macos_remove() -> list:
    import shutil
    bundle = _macos_app_dir()
    if os.path.isdir(bundle):
        shutil.rmtree(bundle, ignore_errors=True)
        return [bundle]
    return []


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
def _linux_paths() -> tuple:
    apps = os.path.expanduser("~/.local/share/applications")
    desktop = os.path.expanduser("~/Desktop")
    return os.path.join(apps, "vlocalhost.desktop"), \
        os.path.join(desktop, "vlocalhost.desktop")


def _linux_install() -> list:
    icon_src = _icon("vlocalhost.png")
    icon_ref = "vlocalhost"
    if icon_src:
        icon_dir = os.path.expanduser(
            "~/.local/share/icons/hicolor/512x512/apps")
        os.makedirs(icon_dir, exist_ok=True)
        import shutil
        installed = os.path.join(icon_dir, "vlocalhost.png")
        shutil.copyfile(icon_src, installed)
    else:
        icon_ref = ""

    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Comment={DESCRIPTION}\n"
        f'Exec="{_interpreter()}" "{_entry_script()}"\n'
        f"Path={os.path.dirname(_entry_script())}\n"
        f"Icon={icon_ref}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Utility;\n"
        "StartupNotify=true\n"
    )

    written = []
    menu_path, desktop_path = _linux_paths()
    os.makedirs(os.path.dirname(menu_path), exist_ok=True)
    with open(menu_path, "w", encoding="utf-8") as f:
        f.write(entry)
    os.chmod(menu_path, 0o755)
    written.append(menu_path)

    if os.path.isdir(os.path.dirname(desktop_path)):
        with open(desktop_path, "w", encoding="utf-8") as f:
            f.write(entry)
        os.chmod(desktop_path, 0o755)
        written.append(desktop_path)

    # Newer GNOME/KDE will not offer a launcher it considers untrusted.
    for path in written:
        subprocess.run(["gio", "set", path, "metadata::trusted", "true"],
                       capture_output=True)
    subprocess.run(["update-desktop-database",
                    os.path.dirname(menu_path)], capture_output=True)
    return written


def _linux_remove() -> list:
    removed = []
    for path in _linux_paths():
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def install() -> list:
    """Create the shortcut(s). Returns the paths written."""
    system = platform.system()
    if system == "Windows":
        return _windows_install()
    if system == "Darwin":
        return _macos_install()
    return _linux_install()


def remove() -> list:
    """Delete any shortcut this module created. Returns the paths removed."""
    system = platform.system()
    if system == "Windows":
        return _windows_remove()
    if system == "Darwin":
        return _macos_remove()
    return _linux_remove()


def run_install() -> int:
    try:
        written = install()
    except Exception as e:  # noqa: BLE001 - a shortcut is never worth a traceback
        print(f"Could not create the shortcut: {e}", flush=True)
        print("The app still runs with: "
              f"python {os.path.basename(_entry_script())}", flush=True)
        return 1
    if not written:
        print("No shortcut location was available.", flush=True)
        return 1
    print(f"{APP_NAME} is now double-clickable. Created:", flush=True)
    for path in written:
        print(f"  {path}", flush=True)
    return 0


def run_remove() -> int:
    try:
        removed = remove()
    except Exception as e:  # noqa: BLE001
        print(f"Could not remove the shortcut: {e}", flush=True)
        return 1
    if not removed:
        print("No shortcut found — nothing to remove.", flush=True)
        return 0
    print("Removed:", flush=True)
    for path in removed:
        print(f"  {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run_remove() if "--remove" in sys.argv[1:] else run_install())
