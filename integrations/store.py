"""Where the app's own files live on disk, outside the application folder.

Cross-platform: we pick per-user directories that exist on Windows, macOS, and
Linux, without any third-party dependency.

There are two of them, and the split matters:

**Config** — settings and credentials. Small, and this is also where a
per-user file has always lived, so the location must not change or existing
installs lose their settings.

    Windows:  %APPDATA%\\MeetingNotesAgent
    macOS:    ~/Library/Application Support/MeetingNotesAgent
    Linux:    $XDG_CONFIG_HOME/meeting-notes-agent  (or ~/.config/...)

    google_client_secret.json   # the OAuth "app" credentials YOU provide
    google_token.json           # cached user token (created after --connect)
    ms_client.json              # {"client_id": "..."} you provide for Outlook
    ms_token_cache.json         # cached MSAL token (created after --connect)
    settings.json               # anything changed from the Settings tab

**Data** — what the user creates. Notes, and any models they download.

    Windows:  %LOCALAPPDATA%\\Vlocalhost
    macOS:    ~/Library/Application Support/Vlocalhost
    Linux:    $XDG_DATA_HOME/vlocalhost  (or ~/.local/share/...)

Neither is inside the application folder, and that is the whole point. An
installer replaces the application folder wholesale — some delete it first —
so anything kept beside the source is destroyed the first time somebody
updates. Notes used to live there. See :mod:`migrate`.
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


def data_dir() -> str:
    """Return (creating if needed) the per-user data directory.

    For things the *user* made — notes above all. Kept apart from the
    application folder so that reinstalling, upgrading, or deleting the app
    cannot touch them.
    """
    system = platform.system()
    if system == "Windows":
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or os.path.expanduser("~"))
        path = os.path.join(base, "Vlocalhost")
    elif system == "Darwin":
        path = os.path.expanduser("~/Library/Application Support/Vlocalhost")
    else:  # Linux / other POSIX
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, "vlocalhost")
    os.makedirs(path, exist_ok=True)
    return path


def notes_dir() -> str:
    """Return (creating if needed) the folder transcripts and notes go in.

    ``config.OUTPUT_DIR`` is honoured as an absolute path — that is how you
    point your notes at a synced folder — and otherwise treated as a plain
    name inside :func:`data_dir`. It is never resolved against the application
    folder, whatever it is set to.

    ``config`` is imported here rather than at module scope so this module
    stays importable on its own, and so the value is read after
    ``settings.apply()`` has had a chance to overlay a saved override.
    """
    import config

    configured = str(getattr(config, "OUTPUT_DIR", "") or "notes")
    path = configured if os.path.isabs(configured) \
        else os.path.join(data_dir(), configured)
    os.makedirs(path, exist_ok=True)
    return path


def models_dir() -> str:
    """Return (creating if needed) the folder downloaded models are cached in.

    Survives reinstalls, so nobody re-downloads a multi-gigabyte model because
    they updated the app.
    """
    path = os.path.join(data_dir(), "models")
    os.makedirs(path, exist_ok=True)
    return path
