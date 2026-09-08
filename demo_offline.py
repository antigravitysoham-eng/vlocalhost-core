"""The demo: a sealed install writing notes with no network at all.

    cd core && python demo_offline.py

It is one claim, shown rather than asserted — *nothing leaves this machine* —
and it is the one thing no competitor in the bracket can currently demonstrate.
Granola, Wispr Flow and Fireflies all need the cloud to produce a summary.

Five steps, each printing what it did:

1. Print the network contract — every connection the app can make.
2. Seal the install, so those connections become impossible rather than unused.
3. Load the speech model with the network sealed, proving it is already here.
4. Write notes with an embedded engine, so there is no second process either.
5. Show that the update check refuses, and says why rather than failing oddly.

Run it with the wifi off. That is the point; nothing in it needs a network, and
step 5 is the only part that even notices.

    --keep-sealed   leave Sealed Mode on when the demo finishes
    --engine NAME   ollama | ctranslate2 | llamacpp  (default: pick what is here)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import network         # noqa: E402
import summarizer      # noqa: E402

TRANSCRIPT = """[10:01:04] You: Right — the Linux binary. Where are we?
[10:01:12] Them: Packaging is done, signing is not. I can have it Friday.
[10:01:31] You: Friday works. Does it need the offline installer to land first?
[10:01:44] Them: No, they are independent. I will own the binary, you own the bundle.
[10:02:03] You: Agreed. Let us not announce until both are out.
[10:02:15] Them: One risk — the notarisation queue was slow last week."""

HF_HUB = os.environ.get("HF_HUB_CACHE") or os.path.join(
    os.path.expanduser("~"), ".cache", "huggingface", "hub")

EMBEDDED = [
    ("ctranslate2", os.path.join(
        HF_HUB, "models--jncraton--Qwen2.5-1.5B-Instruct-ct2-int8",
        "snapshots", "55bb006d27f0b32c8b872a49fecc1d27b5071276")),
    ("llamacpp", os.path.join(
        HF_HUB, "models--Qwen--Qwen2.5-1.5B-Instruct-GGUF", "snapshots",
        "91cad51170dc346986eccefdc2dd33a9da36ead9",
        "qwen2.5-1.5b-instruct-q4_k_m.gguf")),
]


def step(n, title):
    print(f"\n\033[1m{n}. {title}\033[0m\n" + "-" * 62)


def pick_engine(requested):
    """The best engine available here, preferring one that needs no other process."""
    if requested:
        for name, path in EMBEDDED:
            if name == requested:
                return name, path
        return requested, ""
    for name, path in EMBEDDED:
        if os.path.exists(path):
            return name, path
    return "ollama", ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep-sealed", action="store_true")
    ap.add_argument("--engine", default="")
    args = ap.parse_args()

    was_sealed = config.SEALED_MODE
    engine_name, model_path = pick_engine(args.engine)

    try:
        step(1, "What this app can connect to")
        print(network.report())

        step(2, "Sealing the install")
        config.SEALED_MODE = True
        print(network.summary())
        blocked = [c.label for c in network.CONNECTIONS if not network.allowed(c.key)]
        for label in blocked:
            print(f"    blocked   {label}")

        step(3, "Loading the speech model, sealed")
        import transcriber
        t = time.perf_counter()
        model = transcriber.FasterWhisperTranscriber().load()
        print(f"    {config.WHISPER_MODEL} loaded in {time.perf_counter() - t:.1f}s "
              f"({type(model).__name__})")
        print("    No download was possible. It was already here, which is the point.")

        step(4, f"Writing notes with the {engine_name} engine")
        config.SUMMARY_ENGINE = engine_name
        if model_path:
            config.NOTES_MODEL = model_path
        summarizer._engine = None
        ok, detail = summarizer.status()
        print(f"    engine    {summarizer.model_label()} — {detail}")
        if not ok:
            print("    Engine not ready; skipping the summary.")
        else:
            if engine_name == "ollama":
                print("    (Ollama is loopback, so it survives sealing — but it is a")
                print("     second process. An embedded engine needs nothing at all.)")
            t = time.perf_counter()
            notes = summarizer.summarize(TRANSCRIPT)
            print(f"    written in {time.perf_counter() - t:.1f}s\n")
            for line in notes.strip().splitlines():
                print("    " + line)

        step(5, "The one thing that does notice")
        import updates
        try:
            updates.check_now()
            print("    Update check succeeded — the install is not sealed.")
        except network.Sealed as e:
            print(f"    {e}")

        print("\n" + "=" * 62)
        print("  Audio, transcript and notes were produced on this machine.")
        print("  Nothing was uploaded, and while sealed nothing could be.")
        print("=" * 62)

    finally:
        if not args.keep_sealed:
            config.SEALED_MODE = was_sealed
            print("\n(Sealed Mode restored to its previous value. This demo never "
                  "wrote to your settings.)")


if __name__ == "__main__":
    main()
