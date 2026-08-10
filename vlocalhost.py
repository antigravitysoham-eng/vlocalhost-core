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

Recording, transcription, summaries and notes on disk need no account and no
network. If a calendar provider is installed, connecting one additionally lets
notes name themselves from the meeting, lets recording start and stop on its
own, and lets the summary be emailed to attendees or written back onto the
event. ``--connect`` lists whichever providers this build has.
"""

import sys
import threading

import config
import settings


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
    from audio_listener import LoopbackListener
    import sounddevice as sd

    print("Input devices (microphones)\n" + "-" * 34, flush=True)
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            print(f"  [{index}] {device['name']}", flush=True)
    default_in = sd.default.device[0]
    print(f"\nDefault input: {default_in}", flush=True)

    ok, reason = LoopbackListener.available()
    print(f"\nSystem audio (the other people on a call): "
          f"{'available' if ok else 'NOT available'}\n  {reason}", flush=True)
    print(f"\nCAPTURE_MODE is {config.CAPTURE_MODE!r}.", flush=True)


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
def main(argv):
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
            print(f"Usage: python vlocalhost.py --connect "
                  f"{'|'.join(choices)}", flush=True)
            return 1
        run_connect(name)
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
