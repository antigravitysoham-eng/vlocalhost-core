#!/usr/bin/env python3
"""Build the audio fixtures the pipeline bench measures against.

    python tools/make_fixtures.py

Writes ``tools/fixtures/``:

    speech-clean.wav    a six-turn meeting, two speakers, silence between turns
    speech-clean.json   the ground truth: what was said, and exactly when
    speech-cafe.wav     the same audio under room noise at ~10 dB SNR
    speech-music.wav    the same audio under music at ~5 dB SNR
    music-only.wav      music and nothing else — must produce zero transcript

Why synthesised speech rather than a recording: the bench needs to know the
sample at which each utterance *ended* to measure endpoint delay, and it needs
the same audio every run so two phases can be compared. A recording gives
neither. Windows' own SAPI voices are on every target machine, so this
regenerates identically anywhere the product runs.

The tradeoff is honest and worth stating: TTS is cleaner and more evenly paced
than a person, so the word error rate here is optimistic in absolute terms. It
is a *relative* baseline — the question it answers is "did this change make
things better or worse", which is the question every later phase asks.
Real-voice clips can be dropped in beside these; the bench reads any WAV.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

SAMPLE_RATE = 16000

# A meeting, not a reading passage: names, a date, a number, and the domain
# words this product actually has to get right.
SCRIPT = [
    ("David", "Good morning everyone, thanks for joining the standup."),
    ("Zira", "Let's start with the release. The Windows installer is signed "
             "off and ready to ship."),
    ("David", "I still need two more days for the macOS build, mainly the "
              "notarisation."),
    ("Zira", "Can we move the launch to Thursday the twentieth?"),
    ("David", "That works for me. I'll update the calendar and let the team "
              "know."),
    ("Zira", "Last thing, the latency dropped from two seconds to under one."),
]

# One person talking without pausing, which is where provisional text earns its
# keep. A meeting made of two-second answers barely needs it — the finished
# line lands almost as soon as a guess could. Somebody explaining something for
# twenty seconds is the case where a live transcript is either useful or a
# blank rectangle.
MONOLOGUE = [
    ("David",
     "So the way the release pipeline works now is that every platform builds "
     "on its own runner, because native wheels cannot be cross compiled, and "
     "then a separate job collects whatever those runners produced and opens "
     "a draft release that a human has to approve before anything reaches a "
     "user. The part that bit us last time was that the collection step only "
     "looked for zip files, so the installer and the disk image were built "
     "correctly and then quietly thrown away, and nobody noticed until "
     "somebody tried to download the thing and got a page listing releases "
     "with no files attached to them at all."),
]

#: Silence between turns. Longer than any endpoint timeout we would consider,
#: so the segmenter's behaviour is being measured, not the gap's.
GAP_SECONDS = 1.5
#: Lead-in, so the first utterance does not start at sample zero.
LEAD_SECONDS = 1.0

_PS1 = r"""
# Synthesise each line to its own 16 kHz mono WAV using the SAPI voices that
# ship with Windows. One PowerShell launch for the whole batch; starting the
# runtime per line dominates the cost otherwise.
Add-Type -AssemblyName System.Speech
$jobs = Get-Content -Raw -Path "%JOBS%" | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($job in $jobs) {
    foreach ($v in $synth.GetInstalledVoices()) {
        if ($v.VoiceInfo.Name -like ("*" + $job.voice + "*")) {
            $synth.SelectVoice($v.VoiceInfo.Name); break
        }
    }
    $synth.SetOutputToWaveFile($job.out, $fmt)
    $synth.Speak($job.text)
}
$synth.SetOutputToNull()
$synth.Dispose()
"""


def _speak(jobs, workdir):
    """Render every (voice, text, out) job with SAPI. Windows only."""
    jobs_path = os.path.join(workdir, "_tts_jobs.json")
    ps1_path = os.path.join(workdir, "_tts.ps1")
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f)
    with open(ps1_path, "w", encoding="utf-8") as f:
        f.write(_PS1.replace("%JOBS%", jobs_path.replace("\\", "\\\\")))

    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", ps1_path],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"SAPI synthesis failed:\n{proc.stdout}\n{proc.stderr}")
    for path in (jobs_path, ps1_path):
        os.remove(path)


def read_wav(path):
    """A WAV as float32 in [-1, 1], resampled to SAMPLE_RATE if it has to be."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected 16-bit PCM")
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        # Linear resample. Only a guard for an odd voice driver — SAPI is asked
        # for 16 kHz above and gives it.
        n = int(round(len(audio) * SAMPLE_RATE / rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio).astype(np.float32)
    return audio


def write_wav(path, audio):
    """Write float audio as 16-bit mono PCM, clipped rather than wrapped."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def trim_silence(audio, threshold=0.01):
    """Drop leading/trailing near-silence SAPI pads each clip with.

    Without this the 'gap' between turns is the scripted gap plus whatever
    padding the voice happened to add, and the endpoint measurement inherits
    that error.
    """
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if loud.size == 0:
        return audio
    return audio[loud[0]:loud[-1] + 1]


def build_speech(workdir, script=SCRIPT, prefix="_turn"):
    """The clean clip plus ground truth: text and exact sample range per turn."""
    jobs, paths = [], []
    for i, (voice, text) in enumerate(script):
        out = os.path.join(workdir, f"{prefix}{i}.wav")
        jobs.append({"voice": voice, "text": text, "out": out})
        paths.append(out)
    _speak(jobs, workdir)

    timeline = np.zeros(int(LEAD_SECONDS * SAMPLE_RATE), dtype=np.float32)
    truth = []
    for (voice, text), path in zip(script, paths):
        clip = trim_silence(read_wav(path))
        start = len(timeline)
        timeline = np.concatenate([timeline, clip])
        truth.append({
            "voice": voice,
            "text": text,
            "start_sample": int(start),
            "end_sample": int(len(timeline)),
            "start_seconds": round(start / SAMPLE_RATE, 3),
            "end_seconds": round(len(timeline) / SAMPLE_RATE, 3),
        })
        timeline = np.concatenate(
            [timeline, np.zeros(int(GAP_SECONDS * SAMPLE_RATE), np.float32)])
        os.remove(path)

    # A hair of dither. Digital silence is not a realistic microphone floor,
    # and a VAD tuned against pure zeros flatters itself.
    rng = np.random.default_rng(7)
    timeline += rng.normal(0, 1e-4, len(timeline)).astype(np.float32)
    return timeline, truth


def pink_noise(n, rng):
    """1/f noise — the shape of room rumble, traffic and air handling."""
    white = rng.normal(0, 1, n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    freqs[0] = freqs[1] if len(freqs) > 1 else 1.0
    spectrum /= np.sqrt(freqs)
    out = np.fft.irfft(spectrum, n)
    return (out / (np.max(np.abs(out)) or 1.0)).astype(np.float32)


def cafe_noise(n, rng):
    """Room tone plus the clatter a café actually makes.

    Pink noise on its own is too polite: a VAD that survives steady noise can
    still trigger on a cup hitting a saucer, and that transient is the thing
    that puts junk in a transcript.
    """
    base = pink_noise(n, rng) * 0.7
    clatter = np.zeros(n, dtype=np.float32)
    t = np.arange(SAMPLE_RATE // 4) / SAMPLE_RATE
    for _ in range(int(n / SAMPLE_RATE * 1.5)):  # ~1.5 events a second
        at = rng.integers(0, max(1, n - len(t)))
        freq = rng.uniform(900, 4200)
        burst = np.sin(2 * np.pi * freq * t) * np.exp(-t * rng.uniform(25, 60))
        clatter[at:at + len(t)] += burst.astype(np.float32) * rng.uniform(.2, .6)
    # A murmur of far-off voices: narrow band, amplitude-wobbled.
    murmur = pink_noise(n, rng)
    wobble = 1 + 0.6 * np.sin(2 * np.pi * 0.7 * np.arange(n) / SAMPLE_RATE)
    return base + clatter * 0.5 + (murmur * wobble * 0.25).astype(np.float32)


def music(n, rng):
    """A chord progression with a beat — what a VAD most often mistakes for
    speech, and what makes Whisper invent lyrics."""
    t = np.arange(n) / SAMPLE_RATE
    out = np.zeros(n, dtype=np.float32)
    progression = [(220.0, 277.2, 329.6), (196.0, 246.9, 293.7),
                   (174.6, 220.0, 261.6), (164.8, 207.7, 246.9)]
    bar = SAMPLE_RATE * 2
    for i in range(0, n, bar):
        chord = progression[(i // bar) % len(progression)]
        span = slice(i, min(i + bar, n))
        local = t[span] - t[span][0]
        env = np.exp(-local * 0.6)
        for root in chord:
            for harmonic, gain in ((1, 1.0), (2, .5), (3, .25), (4, .12)):
                out[span] += (np.sin(2 * np.pi * root * harmonic * t[span])
                              * gain * env * 0.12).astype(np.float32)
    # Kick and hat, so it has the transient structure of a real track.
    beat = np.arange(0, n, SAMPLE_RATE // 2)
    kick_t = np.arange(SAMPLE_RATE // 8) / SAMPLE_RATE
    kick = (np.sin(2 * np.pi * 55 * kick_t) * np.exp(-kick_t * 30)).astype(np.float32)
    for at in beat:
        end = min(at + len(kick), n)
        out[at:end] += kick[:end - at] * 0.5
        hat = rng.normal(0, 1, min(SAMPLE_RATE // 40, n - at)).astype(np.float32)
        hat *= np.exp(-np.arange(len(hat)) / (SAMPLE_RATE / 400))
        out[at:at + len(hat)] += hat * 0.08
    return out / (np.max(np.abs(out)) or 1.0)


def mix_at_snr(speech, noise, snr_db):
    """Scale `noise` so speech sits `snr_db` above it, measured over the parts
    where there *is* speech — averaging over the silent gaps would understate
    the noise and make every fixture easier than it claims to be."""
    voiced = speech[np.abs(speech) > 0.02]
    speech_rms = float(np.sqrt(np.mean(voiced ** 2))) if voiced.size else 1e-3
    noise_rms = float(np.sqrt(np.mean(noise ** 2))) or 1e-9
    target = speech_rms / (10 ** (snr_db / 20))
    return speech + noise * (target / noise_rms)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cafe-snr", type=float, default=10.0,
                    help="speech-to-noise for the café clip, dB (default 10)")
    ap.add_argument("--music-snr", type=float, default=5.0,
                    help="speech-to-music for the music clip, dB (default 5)")
    args = ap.parse_args()

    if sys.platform != "win32":
        raise SystemExit("Fixture generation uses Windows SAPI. On macOS use "
                         "`say`, or copy the generated fixtures across.")

    # Clear only what this script produces. The directory also holds recorded
    # bench results (baseline-*.json, phase*.json), which are the numbers every
    # later change is scored against -- an rmtree here quietly deleted them
    # once, which is a bad way to find out that regenerating audio should not
    # destroy the measurements taken from it.
    os.makedirs(FIXTURES, exist_ok=True)
    for stale in glob.glob(os.path.join(FIXTURES, "*.wav")):
        os.remove(stale)
    for stale in glob.glob(os.path.join(FIXTURES, "speech-*.json")):
        os.remove(stale)

    speech, truth = build_speech(FIXTURES)
    rng = np.random.default_rng(11)

    write_wav(os.path.join(FIXTURES, "speech-clean.wav"), speech)
    with open(os.path.join(FIXTURES, "speech-clean.json"), "w",
              encoding="utf-8") as f:
        json.dump({"sample_rate": SAMPLE_RATE,
                   "duration_seconds": round(len(speech) / SAMPLE_RATE, 3),
                   "utterances": truth}, f, indent=2)
        f.write("\n")

    write_wav(os.path.join(FIXTURES, "speech-cafe.wav"),
              mix_at_snr(speech, cafe_noise(len(speech), rng), args.cafe_snr))
    write_wav(os.path.join(FIXTURES, "speech-music.wav"),
              mix_at_snr(speech, music(len(speech), rng), args.music_snr))
    # Music with no speech under it at all. The only correct transcript for
    # this file is an empty one.
    write_wav(os.path.join(FIXTURES, "music-only.wav"),
              music(len(speech), rng) * 0.5)

    # One uninterrupted turn: the case provisional text exists for.
    mono, mono_truth = build_speech(FIXTURES, MONOLOGUE, prefix="_mono")
    write_wav(os.path.join(FIXTURES, "speech-monologue.wav"), mono)
    with open(os.path.join(FIXTURES, "speech-monologue.json"), "w",
              encoding="utf-8") as f:
        json.dump({"sample_rate": SAMPLE_RATE,
                   "duration_seconds": round(len(mono) / SAMPLE_RATE, 3),
                   "utterances": mono_truth}, f, indent=2)
        f.write("\n")

    total = len(speech) / SAMPLE_RATE
    print(f"{os.path.relpath(FIXTURES, os.path.dirname(HERE))} — "
          f"{len(truth)} utterances, {total:.1f}s")
    for u in truth:
        print(f"  {u['start_seconds']:6.2f}-{u['end_seconds']:6.2f}s  "
              f"{u['voice']:6s} {u['text'][:56]}")


if __name__ == "__main__":
    main()
