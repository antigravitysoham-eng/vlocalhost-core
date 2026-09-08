# Bringing your own models

Vlocalhost ships with sensible defaults and no model lock-in. Two models do the
work, and you can replace either one:

| Job | Default | Runs on |
|---|---|---|
| Speech → text | faster-whisper `base` | your CPU (or GPU) |
| Text → notes | Ollama `llama3.2` | your CPU/GPU, via `127.0.0.1` |

Nothing here needs an account, and none of it sends audio anywhere.

> **Settings persist, `config.py` does not.** Change these from
> **Settings → Models**, from the setup wizard, or with `--set`. Values saved
> that way live in your per-user config folder and survive updates. Editing
> `config.py` by hand works until the next update, which replaces the file.
> Run `python vlocalhost.py --paths` to see where everything lives.

---

## 1. The speech model

### Pick a different size

The quickest change. Bigger is more accurate and slower:

```bash
python vlocalhost.py --set WHISPER_MODEL=small
```

`tiny` · `base` · `small` · `medium` · `large-v3`, each with an optional `.en`
suffix.

**`.en` models are English-only.** Given Hindi they don't fail, they
hallucinate fluent English. The app blocks that pairing rather than producing
confident nonsense — drop the suffix for the multilingual weights, which cover
100 languages.

Or use **Settings → Performance**, which sets the model, beam size and compute
type together, and can benchmark your actual machine rather than quoting
someone else's.

### Use a model from Hugging Face

Any CTranslate2-format Whisper model works by repo id:

```bash
python vlocalhost.py --set WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2
```

It downloads once and is cached.

### Use a model folder on disk

Fully offline, any size, any language, no download:

```bash
python vlocalhost.py --set WHISPER_MODEL="D:\models\my-whisper-large"
```

or **Settings → Models → Folder…**

If your model is a normal Transformers Whisper checkpoint, convert it first:

```bash
pip install ctranslate2 transformers[torch]
ct2-transformers-converter \
    --model openai/whisper-large-v3 \
    --output_dir  D:\models\my-whisper-large \
    --copy_files tokenizer.json preprocessor_config.json \
    --quantization int8
```

The folder you point at should contain `model.bin`, `config.json` and the
tokenizer files.

### Use a completely different engine

To bypass faster-whisper entirely, write a class with two methods:

```python
# my_engine.py — anywhere on your PYTHONPATH
class MyWhisper:
    """Vlocalhost hands you raw audio and expects text back."""

    def load(self):
        """Optional. Called once before the first utterance."""
        self.model = ...

    def transcribe(self, pcm_bytes: bytes) -> str:
        """16-bit mono PCM at 16 kHz -> what was said. Return "" for silence."""
        return self.model.run(pcm_bytes)
```

Point the app at it:

```bash
python vlocalhost.py --set CUSTOM_TRANSCRIBER=my_engine:MyWhisper
```

Clear it with `--set CUSTOM_TRANSCRIBER=none`.

This imports and runs code you name, with your user account's permissions —
the same as any Python you choose to run. Settings → Models keeps it behind an
Advanced label for that reason.

### GPU

```bash
python vlocalhost.py --set WHISPER_DEVICE=cuda
python vlocalhost.py --set WHISPER_COMPUTE=float16
```

Needs a CUDA-capable card with the matching CUDA and cuDNN libraries installed.
Leave it on `cpu`/`int8` otherwise.

---

## 2. The summary model

Summaries are written by whatever **notes engine** is configured. Ollama is the
default and, today, the only one enabled. Recording and transcription do not
need it at all — without it you still get a full timestamped transcript, and
only the `-notes.md` summary is skipped. The transcript keeps its timestamps;
the summary never has them.

### Choosing an engine

The speech side has always been open: any model name, any folder, or a custom
engine. The notes side now works the same way, because "no model lock-in" ought
to mean both halves of the product.

| Engine | What it needs | Status |
|---|---|---|
| `ollama` | Ollama installed | **Default.** A model manager as well as a runtime |
| `ctranslate2` | a converted model folder in `NOTES_MODEL` | Working, not yet the default |
| `llamacpp` | a `.gguf` in `NOTES_MODEL`, plus `llama-cpp-python` | Working, not yet the default |

The two embedded engines run the model inside Vlocalhost, with no second
process to install — which is the whole point of them. Both have been run
end to end against Qwen2.5-1.5B-Instruct and produce correct notes. Neither is
the default yet: which one ships is a measurement, not a preference.

Select one before its weights exist and it refuses immediately, naming what is
missing, rather than failing somewhere less obvious later.

```bash
python vlocalhost.py --set SUMMARY_ENGINE=llamacpp
python vlocalhost.py --set NOTES_MODEL="D:\models\qwen2.5-1.5b-instruct-q4_k_m.gguf"
```

**A note if you write your own conversion.** Instruct models need their turns
marked up, and neither embedded runtime applies a chat template on your behalf
— CTranslate2's `Generator` is a raw next-token loop, and llama-cpp's plain
call is too. Handed a bare prompt, Qwen2.5-Instruct continued the transcript
instead of summarising it: forty repetitions of one line, and 210 seconds
because nothing told it to stop. Vlocalhost applies ChatML for CTranslate2 and
uses llama-cpp's chat API, so this is handled — but it is the failure to expect
if you plug in a model that expects some other markup.

### Write your own

The same shape as a custom speech engine. Two methods are required:

```python
# my_notes.py — anywhere on your PYTHONPATH
class MyNotes:
    def summarize(self, transcript: str) -> str:
        """The transcript -> Markdown notes."""
        return my_model.run(transcript)

    def title(self, transcript: str) -> str:
        """A short title, or "" if you cannot produce one."""
        return ""
```

```bash
python vlocalhost.py --set CUSTOM_SUMMARIZER=my_notes:MyNotes
```

Two optional methods make it a better citizen: `status()` returning
`(ok, detail)` so Settings and `--diagnose` can report on it, and
`model_label()` for the status bar. Leave them out and the app falls back to
your class's name.

You do not have to handle prompts, the notes language, or stripping timestamps
— Vlocalhost does all of that around your engine, so every backend behaves the
same way without knowing about any of it.

**This is the one setting that can send a transcript somewhere we do not
control.** A custom engine reaches wherever you point it, including a cloud
endpoint. `--network` says so plainly rather than burying it.

### Use a different model

```bash
ollama pull mistral
python vlocalhost.py --set OLLAMA_MODEL=mistral
```

Anything Ollama can run works: `llama3.1`, `mistral`, `qwen2.5`, `phi3`, a
larger quantisation, or your own `Modelfile` build.

**Tags count.** Ollama reports models as `name:tag` — `llama3.2:latest`, not
`llama3.2`. Use **Settings → Models → List…**, which reads the installed models
straight off the server and fills the dropdown, rather than typing a name and
hoping.

### Point at Ollama on another machine

Useful when your laptop is modest and there's a workstation on the LAN. Audio
still never leaves your machine — only the finished transcript text is sent to
the summariser you nominate.

```bash
python vlocalhost.py --set OLLAMA_URL=http://192.168.1.20:11434
```

The remote Ollama must be started with `OLLAMA_HOST=0.0.0.0` to accept
connections beyond its own loopback.

---

## 3. Where notes are written

```bash
python vlocalhost.py --set OUTPUT_DIR="D:\Dropbox\Meetings"
```

An absolute path is used exactly as given. A plain name lands inside your
per-user data folder. Either way it is never inside the application folder, so
updating or reinstalling cannot touch your notes.

---

## Checking what's set

```bash
python vlocalhost.py --get              # every setting you can change
python vlocalhost.py --get WHISPER_MODEL
python vlocalhost.py --paths            # notes, settings, models, app
python vlocalhost.py --setup            # re-run the setup wizard
python vlocalhost.py --diagnose         # a report to attach to a bug
```
