"""The languages Vlocalhost can transcribe.

Whisper's multilingual models cover 100 languages. This module curates that
list for the UI and, just as importantly, records what is *not* covered — a
missing language must fail loudly, because Whisper's failure mode on an
unsupported language is confident nonsense rather than an error.

Only the multilingual models can do any of this. The ``.en`` models
(``base.en`` and friends) are English-only weights: give them Hindi and they
will hallucinate English. :func:`check` catches that pairing.
"""

# code -> (English name, name in the language itself)
# Curated from faster_whisper.tokenizer._LANGUAGE_CODES; verified present.
INDIAN = {
    "hi": ("Hindi", "हिन्दी"),
    "bn": ("Bengali", "বাংলা"),
    "mr": ("Marathi", "मराठी"),
    "gu": ("Gujarati", "ગુજરાતી"),
    "ta": ("Tamil", "தமிழ்"),
    "te": ("Telugu", "తెలుగు"),
    "kn": ("Kannada", "ಕನ್ನಡ"),
    "ml": ("Malayalam", "മലയാളം"),
    "pa": ("Punjabi", "ਪੰਜਾਬੀ"),
    "ur": ("Urdu", "اردو"),
    "as": ("Assamese", "অসমীয়া"),
    "sa": ("Sanskrit", "संस्कृतम्"),
    "sd": ("Sindhi", "سنڌي"),
    "ne": ("Nepali", "नेपाली"),
    "si": ("Sinhala", "සිංහල"),
}

COMMON = {
    "en": ("English", "English"),
    "fr": ("French", "Français"),
    "es": ("Spanish", "Español"),
    "ja": ("Japanese", "日本語"),
    "zh": ("Chinese", "中文"),
    "yue": ("Cantonese", "粵語"),
    "de": ("German", "Deutsch"),
    "pt": ("Portuguese", "Português"),
    "it": ("Italian", "Italiano"),
    "ru": ("Russian", "Русский"),
    "ar": ("Arabic", "العربية"),
    "ko": ("Korean", "한국어"),
    "nl": ("Dutch", "Nederlands"),
    "tr": ("Turkish", "Türkçe"),
    "vi": ("Vietnamese", "Tiếng Việt"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "th": ("Thai", "ไทย"),
    "pl": ("Polish", "Polski"),
    "uk": ("Ukrainian", "Українська"),
    "he": ("Hebrew", "עברית"),
    "fa": ("Persian", "فارسی"),
    "sw": ("Swahili", "Kiswahili"),
    "cy": ("Welsh", "Cymraeg"),
}

CURATED = {**COMMON, **INDIAN}

# Asked for, but genuinely absent from Whisper. Naming them beats letting a
# user select something that would quietly produce garbage.
NOT_SUPPORTED = {
    "ga": "Irish (Gaeilge)",
    "gd": "Scottish Gaelic",
    "or": "Odia (Oriya)",
    "kok": "Konkani",
    "mai": "Maithili",
    "bho": "Bhojpuri",
    "mni": "Manipuri (Meitei)",
    "doi": "Dogri",
    "ks": "Kashmiri",
    "sat": "Santali",
    "brx": "Bodo",
}

AUTO = "auto"  # detect per utterance


def all_codes():
    """Every language code Whisper actually supports (not just the curated set)."""
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES

        return set(_LANGUAGE_CODES)
    except Exception:  # noqa: BLE001 - faster-whisper not installed / renamed
        return set(CURATED)


def name_for(code):
    """A human label for a language code, e.g. 'hi' -> 'Hindi (हिन्दी)'."""
    if not code or code == AUTO:
        return "Auto-detect"
    if code in CURATED:
        english, native = CURATED[code]
        return english if english == native else f"{english} ({native})"
    return code


def choices():
    """(code, label) pairs for a picker: auto first, then common, then Indian."""
    items = [(AUTO, "Auto-detect (any language)")]
    items += [(code, name_for(code)) for code in COMMON]
    items += [(code, name_for(code)) for code in INDIAN]
    return items


def normalize(value):
    """UI value -> what the transcriber wants (None means auto-detect)."""
    if not value or value == AUTO:
        return None
    return value


def check(model, language):
    """Validate a model/language pairing. Returns a warning string, or ''.

    ``language`` is a code or None/'auto'. This is the guard against the one
    combination that fails silently: an English-only model asked for another
    language.
    """
    code = normalize(language)
    english_only = isinstance(model, str) and model.endswith(".en")

    if english_only and code is None:
        return (f"“{model}” is an English-only model, so auto-detect can't work — "
                "everything will be transcribed as English. Choose a "
                "multilingual model (drop the “.en”, e.g. “small”).")
    if english_only and code != "en":
        return (f"“{model}” is an English-only model and cannot transcribe "
                f"{name_for(code)}. It will produce confident nonsense rather "
                f"than an error. Use a multilingual model such as “small”.")
    if code and code not in all_codes():
        known = NOT_SUPPORTED.get(code)
        return (f"Whisper does not support {known or code} — "
                "no model of any size can transcribe it.")
    return ""
