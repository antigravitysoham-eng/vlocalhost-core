"""A transcriber that does nothing, for isolating capture from compute.

Diagnostic only -- never referenced by the app unless CUSTOM_TRANSCRIBER points
at it. It exists because every audio theory for the call-distortion bug has now
been eliminated by measurement, and the one variable left standing is whether
the speech model is running at all:

    backend     mode   transcription   result
    soundcard   both   yes             distorted
    portaudio   both   yes             distorted
    portaudio   mic    yes             distorted
    soundcard   mic    NO              clean

Capture is innocent in every form tested. This keeps the whole app identical --
the same streams, the same VAD, the same queues, the same GUI -- and removes
only the model. If a recording is clean with this in place, the cause is the
transcription workload and not anything about how audio is captured.

    python vlocalhost.py --set CUSTOM_TRANSCRIBER=null_transcriber:NullTranscriber
    python vlocalhost.py --set CUSTOM_TRANSCRIBER=

Utterances still reach the transcript, so it is obvious at a glance that the
pipeline ran: each one becomes a placeholder line rather than real words.
"""


class NullTranscriber:
    """Accepts audio and returns a placeholder. Loads nothing, decodes nothing."""

    def __init__(self):
        self._utterances = 0

    def precheck(self):
        """Nothing to refuse: there is no model and no language to disagree with."""
        return None

    def load(self):
        """No model. Returning immediately is the whole point of this class."""
        return None

    def load_partial(self):
        return None

    def unload(self):
        return False

    def transcribe(self, pcm_bytes, partial=False):
        """Report that audio arrived, without spending anything on it.

        The byte count is included so the transcript still shows the capture
        pipeline working end to end -- silence would look identical to a broken
        recording, and that ambiguity is what this test cannot afford.
        """
        if partial:
            return ""
        self._utterances += 1
        return f"[no-model test] utterance {self._utterances}, {len(pcm_bytes)} bytes"
