"""Which way of opening the microphone disturbs a call already in progress?

Run this while you are on a browser call and someone can hear you. It opens the
microphone four different ways, ten seconds each, announcing each one. Say a few
words during every phase and note which ones the other person says sound wrong.

Nothing here transcribes or saves anything. It only opens and closes streams.

The question it answers: is the interference caused by *opening the mic at all*,
or by opening it at a rate the device does not natively run at? Everything that
does not disturb a call -- Chromium-based apps, whisper.cpp front ends -- opens
at the native format and resamples in its own code. We ask Windows to convert
48 kHz stereo down to 16 kHz mono, which makes the audio engine reconfigure a
capture endpoint that Chrome already has open.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import soundcard as sc
import config

SECONDS = 10


def phase(n, title, note):
    print("\n" + "=" * 62)
    print("PHASE %d: %s" % (n, title))
    print(note)
    print("Speak now for %d seconds..." % SECONDS, flush=True)


def run(mic, rate, channels, blocksize, label, note, n):
    phase(n, label, note)
    try:
        with mic.recorder(samplerate=rate, channels=channels,
                          blocksize=blocksize) as rec:
            end = time.time() + SECONDS
            frames = 0
            while time.time() < end:
                data = rec.record(numframes=blocksize)
                frames += len(data)
        print("  ok - captured %d frames at %d Hz" % (frames, rate), flush=True)
    except Exception as e:
        print("  FAILED: %s: %s" % (type(e).__name__, e), flush=True)
    print("  stream closed. Does it sound normal again?", flush=True)
    time.sleep(4)


def main():
    mic = sc.default_microphone()
    print("microphone: %s" % mic.name)
    print("app currently asks for: %d Hz mono" % config.SAMPLE_RATE)

    # 1. What the app does today.
    run(mic, config.SAMPLE_RATE, 1, 512, "16 kHz mono - what Vlocalhost does now",
        "If only this one distorts, the rate conversion is the cause.", 1)

    # 2. Native rate, native channels - what every app that behaves does.
    run(mic, 48000, 2, 1024, "48 kHz stereo - the device's native format",
        "If this is clean, the fix is to open natively and resample ourselves.", 2)

    # 3. Native rate, mono. Isolates channel conversion from rate conversion.
    run(mic, 48000, 1, 1024, "48 kHz mono - native rate, downmixed channels",
        "Separates the channel remix from the sample-rate conversion.", 3)

    # 4. Native rate with a larger block. The log is full of "data
    #    discontinuity", which is WASAPI saying it could not keep up.
    run(mic, 48000, 2, 4096, "48 kHz stereo, large block",
        "Tests whether the block size is contributing to the glitching.", 4)

    print("\n" + "=" * 62)
    print("Done. Which phases sounded wrong to the other person?")


if __name__ == "__main__":
    main()
