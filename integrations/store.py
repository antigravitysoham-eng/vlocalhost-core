"""Where OAuth client secrets and cached tokens live on disk.

Cross-platform: we pick a per-user config directory that exists on Windows,
macOS, and Linux, without any third-party dependency.

    Windows:  %APPDATA%\\MeetingNotesAgent
    macOS:    ~/Library/Application Support/MeetingNotesAgent
    Linux:    $XDG_CONFIG_HOME/meeting-notes-agent  (or ~/.config/...)

Files kept here:
    google_client_secret.json   # the OAuth "app" credentials YOU provide
    google_token.json           # cached user token (created after --connect)
    ms_client.json              # {"client_id": "..."} you provide for Outlook
    ms_token_cache.json         # cached MSAL token (created after --connect)
"""

import os
import platform


def config_dir() -> str:
    """Return (creating if needed) the per-user config directory for this app."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "MeetingNotesAgent")
    elif system == "Darwin":
        path = os.path.expanduser("~/Library/Application Support/MeetingNotesAgent")
    else:  # Linux / other POSIX
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        path = os.path.join(base, "meeting-notes-agent")
    os.makedirs(path, exist_ok=True)
    return path


def path_for(filename: str) -> str:
    """Absolute path to a file inside the config dir."""
    return os.path.join(config_dir(), filename)
