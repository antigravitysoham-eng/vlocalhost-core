"""Vlocalhost.AI — entry point.

Meeting notes that never leave your machine. Opens a window by default; the
tray app, the terminal, and the MCP server are the same engine behind a
different front end.

    python vlocalhost.py                     # the app window
    python vlocalhost.py --tray              # system-tray icon only
    python vlocalhost.py --no-tray           # terminal (Ctrl+C to finish)
    python vlocalhost.py --mcp               # MCP server on stdio (for AI clients)
    python vlocalhost.py --devices            # list audio devices and capture support
    python vlocalhost.py --connect <account>  # link a calendar/mail account
    python vlocalhost.py --install-shortcut   # desktop/menu icon, double-click to run
    python vlocalhost.py --remove-shortcut    # take it away again
    python vlocalhost.py --diagnose           # write a report to send with a bug
    python vlocalhost.py --get                # list every setting you can change
    python vlocalhost.py --set OLLAMA_MODEL=mistral   # change one (or several)
    python vlocalhost.py --paths              # where notes, settings and models live

Recording, transcription, summaries and notes on disk need no account and no
network. If a calendar provider is installed, connecting one additionally lets
notes name themselves from the meeting, lets recording start and stop on its
own, and lets the summary be emailed to attendees or written back onto the
event. ``--connect`` lists whichever providers this build has.
"""

import os
import sys
import threading

import config
import settings


def _script() -> str:
    """How this process was invoked, for usage messages. A build reached
    through a launcher shouldn't tell people to run a different file."""
    name = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    return name if name.endswith(".py") else "vlocalhost.py"


def _print_line(line):
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Account setup
# ---------------------------------------------------------------------------
def run_connect(provider_name):
    """Interactive: link a Google/Microsoft account and cache the token."""
    from integrations import get_provider, store

    print(f"Config directory: {store.config_dir()}\n", flush=True)
    try:
        provider = get_provider(provider_name)
    except Exception as e:  # noqa: BLE001
        print(f"Cannot set up '{provider_name}': {e}", flush=True)
        return
    if provider is None:
        print("Provider is 'none' — nothing to connect.", flush=True)
        return
    try:
        provider.authenticate(interactive=True)
    except Exception as e:  # noqa: BLE001
        print(f"\nConnection failed: {e}", flush=True)
        return
    settings.save(CALENDAR_PROVIDER=provider_name)
    print(f"\n✓ Connected {provider_name}, and the app is now set to use it.",
          flush=True)


def run_devices():
    """Show what this machine can record, so capture problems are obvious."""
    from audio_listener import (LoopbackListener, device_candidates,
                                input_devices, rescan_devices)
    import sounddevice as sd

    rescan_devices()

    # The same list Settings offers. One entry per physical microphone: the
    # raw list below shows each of them once per Windows audio API, which is
    # why a machine with one microphone appears to have seven.
    print("Microphones\n" + "-" * 34, flush=True)
    offered = input_devices()
    for device in offered:
        mark = "  (default)" if device["default"] else ""
        print(f"  [{device['index']}] {device['name']}{mark}", flush=True)
    if not offered:
        print("  none found", flush=True)

    setting = getattr(config, "INPUT_DEVICE", None)
    if setting in (None, ""):
        print("\nINPUT_DEVICE is unset — recording follows the system default.",
              flush=True)
    else:
        candidates = device_candidates(setting)
        where = (f"device {candidates[0]}" if candidates
                 else "NOTHING CONNECTED — recording will fail")
        print(f"\nINPUT_DEVICE is {setting!r} -> {where}", flush=True)

    ok, reason = LoopbackListener.available()
    print(f"\nSystem audio (the other people on a call): "
          f"{'available' if ok else 'NOT available'}\n  {reason}", flush=True)
    print(f"\nCAPTURE_MODE is {config.CAPTURE_MODE!r}.", flush=True)

    # Everything PortAudio reports, for a support conversation. Kept second
    # because it answers "what is the driver stack doing", not "which
    # microphone do I pick".
    print("\nAll input endpoints, by audio API\n" + "-" * 34, flush=True)
    try:
        host_apis = sd.query_hostapis()
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] > 0:
                api = host_apis[device["hostapi"]]["name"]
                print(f"  [{index}] {api:22} {device['name']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  could not enumerate: {e}", flush=True)


# ---------------------------------------------------------------------------
# Terminal mode
# ---------------------------------------------------------------------------
def run_cli():
    import engine as engine_mod

    eng = engine_mod.build(on_line=_print_line)
    eng.front_end = "terminal"
    print("Loading the speech model…", flush=True)
    eng.start()
    sources = getattr(eng.notetaker.listener, "sources", None)
    print(f"🎤 Listening ({', '.join(sources) if sources else 'microphone'}). "
          "Silence is ignored. Press Ctrl+C to stop.\n", flush=True)
    try:
        threading.Event().wait()  # sleep until interrupted
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    print("Summarizing… (needs Ollama running)", flush=True)
    result = eng.stop_and_save()
    print("\n" + _result_text(result), flush=True)


def _result_text(result):
    if not result or not result.get("transcript"):
        return (result or {}).get("error") or "Nothing was saved."
    lines = [f"Meeting:    {result['title']}",
             f"Transcript: {result['transcript']}"]
    if result.get("summary"):
        lines.append(f"Summary:    {result['summary']}")
    if result.get("delivered"):
        lines.append(f"Delivered:  {result['delivered']}")
    if result.get("error"):
        lines.append(f"(Summary step failed: {result['error']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tray mode
# ---------------------------------------------------------------------------
def run_tray():
    import pystray
    from PIL import Image, ImageDraw

    import engine as engine_mod

    eng = engine_mod.build(on_line=_print_line)
    eng.front_end = "tray"

    def make_icon(active):
        img = Image.new("RGB", (64, 64), (30, 30, 30))
        d = ImageDraw.Draw(img)
        # A little mic: red dot = listening, grey = idle.
        color = (220, 60, 60) if active else (140, 140, 140)
        d.rounded_rectangle([26, 12, 38, 40], radius=6, fill=color)
        d.arc([20, 28, 44, 50], start=0, end=180, fill=color, width=3)
        d.line([32, 50, 32, 56], fill=color, width=3)
        d.line([24, 56, 40, 56], fill=color, width=3)
        return img

    icon = pystray.Icon("vlocalhost", make_icon(False), "Vlocalhost.AI")

    def notify(msg):
        try:
            icon.notify(msg, "Vlocalhost.AI")
        except Exception:  # noqa: BLE001 - notifications are best-effort
            print(msg, flush=True)

    def refresh(active):
        icon.icon = make_icon(active)
        icon.update_menu()

    def start_listening(_=None):
        if eng.is_listening:
            return
        notify("Loading the model…")

        def _go():
            try:
                eng.start()
            except Exception as e:  # noqa: BLE001
                notify(str(e))
                return
            refresh(True)
            label = eng.event.title if eng.event else None
            notify(f"🎤 Listening: {label}" if label
                   else "🎤 Listening. Silence is ignored.")

        threading.Thread(target=_go, daemon=True).start()

    def stop_and_save(_=None):
        if not eng.is_listening:
            return
        notify("Stopping & summarizing…")

        def _go():
            result = eng.stop_and_save()
            refresh(False)
            if not result.get("transcript"):
                notify(result.get("error") or "Nothing saved.")
            elif result.get("error"):
                notify(f"Transcript saved. Summary failed: {result['error']}")
            else:
                extra = f" ({result['delivered']})" if result.get("delivered") else ""
                notify(f"Saved “{result['title']}”{extra}")
            print(_result_text(result), flush=True)

        threading.Thread(target=_go, daemon=True).start()

    def quit_app(_=None):
        if eng.is_listening:
            notify("Saving transcript & notes…")
        result = eng.shutdown()
        if result:
            print(_result_text(result), flush=True)
        refresh(False)
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(
            lambda item: "⏹ Stop & save summary" if eng.is_listening
            else "▶ Start listening",
            lambda: stop_and_save() if eng.is_listening else start_listening()),
        pystray.MenuItem("Quit", quit_app),
    )

    if config.AUTO_START_FROM_CALENDAR and eng.start_scheduler():
        print("[calendar] auto-start enabled — watching your calendar.", flush=True)

    print("Vlocalhost.AI running in the system tray. "
          "Right-click the tray icon to start.", flush=True)
    icon.run()


# ---------------------------------------------------------------------------
def run_set(pairs):
    """``--set KEY=VALUE`` — write settings from a script or an installer."""
    changes = {}
    for pair in pairs:
        if "=" not in pair:
            print(f"Expected KEY=VALUE, got {pair!r}", flush=True)
            return 1
        key, raw = pair.split("=", 1)
        key = key.strip().upper()
        if key not in settings.EDITABLE:
            print(f"{key} is not a settable option. Try --get to list them.",
                  flush=True)
            return 1
        try:
            changes[key] = settings.coerce(key, raw)
        except ValueError as e:
            print(str(e), flush=True)
            return 1

    settings.save(**changes)
    for key, value in sorted(changes.items()):
        print(f"{key} = {value!r}", flush=True)
    print(f"\nSaved to {settings.path()}", flush=True)
    return 0


def run_get(key):
    """``--get [KEY]`` — read one setting, or list every settable option."""
    current = settings.current()
    if not key:
        width = max(len(k) for k in current)
        for name, value in sorted(current.items()):
            print(f"{name.ljust(width)}  {value!r}", flush=True)
        return 0
    key = key.strip().upper()
    if key not in current:
        print(f"{key} is not a settable option. Run --get with no name to "
              f"list them.", flush=True)
        return 1
    print(repr(current[key]), flush=True)
    return 0


def run_paths():
    """``--paths`` — where everything lives, for support conversations."""
    from integrations import store

    print(f"settings   {settings.path()}", flush=True)
    print(f"config     {store.config_dir()}", flush=True)
    print(f"data       {store.data_dir()}", flush=True)
    print(f"notes      {store.notes_dir()}", flush=True)
    print(f"models     {store.models_dir()}", flush=True)
    print(f"app        {os.path.dirname(os.path.abspath(__file__))}", flush=True)
    return 0


# ---------------------------------------------------------------------------
def main(argv):
    import diagnostics
    import migrate

    diagnostics.setup()
    settings.apply()
    # Before anything opens a window or writes a note: rescue anything an
    # older version left inside an application folder.
    migrate.run(quiet="--mcp" in argv)

    if "--diagnose" in argv:
        return diagnostics.run_diagnose()

    if "--set" in argv:
        return run_set([a for a in argv[argv.index("--set") + 1:]
                        if not a.startswith("--")])

    if "--get" in argv:
        i = argv.index("--get")
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        return run_get("" if nxt.startswith("--") else nxt)

    if "--paths" in argv:
        return run_paths()

    if "--setup" in argv:
        import setup_wizard

        setup_wizard.run()
        return 0

    # First run, and a window is what they're getting: ask the setup questions
    # before the app opens. Skipped for the tray, the terminal and MCP, which
    # are either headless or driven by something that can't answer.
    if not any(f in argv for f in ("--tray", "--no-tray", "--mcp",
                                   "--devices", "--connect")):
        import setup_wizard

        if setup_wizard.needed():
            setup_wizard.run()
            settings.apply()

    if "--connect" in argv:
        from integrations import UPGRADE_URL, available_providers

        i = argv.index("--connect")
        name = (argv[i + 1] if i + 1 < len(argv) else "").lower()
        choices = available_providers()
        if not choices:
            print("No calendar or email providers are installed in this build.\n"
                  "Recording, transcription and notes all work without one.\n"
                  f"Calendar auto-start and emailing notes are Pro features:\n"
                  f"  {UPGRADE_URL}", flush=True)
            return 1
        if name not in choices:
            print(f"Usage: python {_script()} --connect "
                  f"{'|'.join(choices)}", flush=True)
            return 1
        run_connect(name)
    elif "--install-shortcut" in argv:
        import shortcut

        return shortcut.run_install()
    elif "--remove-shortcut" in argv:
        import shortcut

        return shortcut.run_remove()
    elif "--devices" in argv:
        run_devices()
    elif "--mcp" in argv:
        import mcp_server

        mcp_server.main()
    elif "--no-tray" in argv:
        run_cli()
    elif "--tray" in argv:
        run_tray()
    else:
        try:
            import gui

            gui.run()
        except Exception as e:  # noqa: BLE001 - no display, no Tk, etc.
            print(f"Window unavailable ({e}); falling back to the tray.\n",
                  flush=True)
            try:
                run_tray()
            except Exception as tray_error:  # noqa: BLE001
                print(f"Tray unavailable ({tray_error}); using terminal mode.\n",
                      flush=True)
                run_cli()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
