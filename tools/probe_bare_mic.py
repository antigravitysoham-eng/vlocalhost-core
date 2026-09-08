"""Does opening the microphone -- and nothing else -- distort a live call?

No VAD, no model, no transcription, no loopback stream, no Vlocalhost. It opens
one capture stream, reads from it, throws the audio away, and closes.

This is the fork the whole investigation rests on and it has never been run to a
conclusion:

  distorts     -> the cause is in the act of opening a capture stream on this
                  endpoint, and every theory about what we do afterwards is
                  irrelevant. The next step is comparing our WASAPI parameters
                  against Chromium's, byte for byte.
  clean        -> opening is fine, and the cause is something we do while
                  holding it. Threads and priority are already eliminated, so
                  what remains is the read pattern itself.

Run it with the Vlocalhost app CLOSED, on a live call, with someone listening.
"""
import sys, time
import soundcard as sc

SECONDS = 20
RATE, CHANNELS = 48000, 2          # the device's own format: nothing to convert
BLOCK = 4800                       # 100 ms


def main():
    mic = sc.default_microphone()
    print("microphone : %s" % mic.name)
    print("opening at : %d Hz, %d ch, %d-frame blocks" % (RATE, CHANNELS, BLOCK))
    print()
    print("Nothing else is running. Speak for %d seconds and ask whether you"
          % SECONDS)
    print("sound normal. The stream opens NOW.")
    print(flush=True)

    t0 = time.time()
    blocks = 0
    with mic.recorder(samplerate=RATE, channels=CHANNELS, blocksize=BLOCK) as rec:
        while time.time() - t0 < SECONDS:
            rec.record(numframes=BLOCK)      # read and discard
            blocks += 1
            left = SECONDS - int(time.time() - t0)
            if blocks % 10 == 0:
                print("  ...%ds left" % left, flush=True)

    print("\nSTREAM CLOSED. Do you sound normal again?", flush=True)


if __name__ == "__main__":
    main()
