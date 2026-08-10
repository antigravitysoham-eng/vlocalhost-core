"""Performance profiles — trade accuracy against RAM and CPU.

The speech model dominates both. These presets bundle the settings that have to
move together (model size, search width, precision) so a user picks one thing
instead of four.

The numbers are **measured**, not estimated: full app with microphone and
system-audio capture both running, `int8` on CPU, on a 6-core Ryzen 5 laptop.
``benchmark()`` re-measures on the machine you're actually on.
"""

import config

PROFILES = {
    "light": {
        "label": "Light",
        "summary": "Old or low-power machines, 2 cores, ≤4 GB RAM",
        "WHISPER_MODEL": "tiny",
        "WHISPER_BEAM_SIZE": 1,
        "WHISPER_COMPUTE": "int8",
        # measured
        "ram_mb": 250,
        "speed": "well ahead of real time",
        "accuracy": "usable; expect errors on names and accents",
    },
    "balanced": {
        "label": "Balanced",
        "summary": "The default. Keeps up with live speech on modest hardware",
        "WHISPER_MODEL": "base",
        "WHISPER_BEAM_SIZE": 1,
        "WHISPER_COMPUTE": "int8",
        "ram_mb": 350,
        "speed": "4.5× real time (measured, 6-core CPU)",
        "accuracy": "good; all 100 languages",
    },
    "accurate": {
        "label": "Accurate",
        "summary": "4+ fast cores and RAM to spare; best for non-English",
        "WHISPER_MODEL": "small",
        "WHISPER_BEAM_SIZE": 5,
        "WHISPER_COMPUTE": "int8",
        "ram_mb": 735,
        "speed": "around real time — lagged on a 6-core CPU, catching up in pauses",
        "accuracy": "best; noticeably better on Indian languages",
    },
}

DEFAULT = "balanced"

# Settings a profile owns. Anything else the user set by hand is left alone.
KEYS = ("WHISPER_MODEL", "WHISPER_BEAM_SIZE", "WHISPER_COMPUTE")


def values(name):
    """The config values for a profile, ready to hand to ``settings.save``."""
    profile = PROFILES.get(name) or PROFILES[DEFAULT]
    return {key: profile[key] for key in KEYS}


def current():
    """Which profile the live config matches, or 'custom' if it matches none."""
    for name, profile in PROFILES.items():
        if all(getattr(config, key, None) == profile[key] for key in KEYS):
            return name
    return "custom"


def describe(name):
    """One line for a UI: what this profile costs and buys."""
    profile = PROFILES.get(name)
    if not profile:
        return "Custom settings."
    return (f"{profile['summary']} · ~{profile['ram_mb']} MB RAM · "
            f"{profile['speed']} · {profile['accuracy']}")


def estimate_ram_mb():
    """Peak RAM to expect with the current settings, in MB."""
    name = current()
    if name in PROFILES:
        return PROFILES[name]["ram_mb"]
    # Custom model: fall back to the closest known size.
    model = str(getattr(config, "WHISPER_MODEL", "")).lower()
    for key, mb in (("tiny", 240), ("base", 340), ("small", 730),
                    ("medium", 1900), ("large", 3600)):
        if key in model:
            return mb
    return 400


def benchmark():
    """Measure this machine: (peak_mb, seconds_to_transcribe_10s_of_audio).

    Loads the configured model and runs it over synthetic audio. Takes a few
    seconds and allocates whatever the model needs, so don't call it while a
    meeting is being recorded.
    """
    import time

    import numpy as np

    from transcriber import FasterWhisperTranscriber

    def rss_mb():
        try:
            import psutil

            info = psutil.Process().memory_info()
            return getattr(info, "peak_wset", info.rss) / (1024 * 1024)
        except Exception:  # noqa: BLE001 - psutil is optional
            return 0.0

    engine = FasterWhisperTranscriber()
    engine.load()

    # 10 s of syllable-shaped tone: enough to exercise the full forward pass.
    t = np.arange(16000 * 10) / 16000
    signal = sum(np.sin(2 * np.pi * f * t) * 0.1 for f in (120, 240, 480, 900))
    signal *= np.abs(np.sin(2 * np.pi * 2.5 * t))
    pcm = (np.clip(signal, -1, 1) * 20000).astype(np.int16).tobytes()

    start = time.time()
    engine.transcribe(pcm)
    elapsed = time.time() - start
    peak = rss_mb()
    engine.unload()
    return peak, elapsed
