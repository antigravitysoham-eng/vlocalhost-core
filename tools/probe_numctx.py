"""Is the prompt reaching Ollama whole, or is the server cutting it?

Not an inference about the notes -- a count. ``/api/generate`` reports
``prompt_eval_count``, the number of prompt tokens the server actually
evaluated. Send a prompt of known size and compare. If the count stops rising
while the prompt keeps growing, the rest of the meeting was dropped, and no
amount of prompt wording will bring it back.
"""
import os, sys, json
import requests
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)
import config
from summarizer import _notes_prompt
sys.path.insert(0, HERE)
from probe_context import transcript

print("model: %s   url: %s" % (config.OLLAMA_MODEL, config.OLLAMA_URL))
print("%8s %10s %18s %s" % ("words", "prompt~tok", "prompt_eval_count", "verdict"))
for target in (400, 1500, 3000, 6000, 9000):
    text, words = transcript(target)
    prompt = _notes_prompt(text)
    approx = len(prompt) // 4                      # rough, only for orientation
    body = {"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False,
            "options": {"num_predict": 1}}         # generate nothing; we want the count
    r = requests.post(f"{config.OLLAMA_URL}/api/generate", json=body, timeout=600)
    r.raise_for_status()
    d = r.json()
    seen = d.get("prompt_eval_count")
    verdict = "ok" if seen and seen >= approx * 0.8 else "TRUNCATED"
    print("%8d %10d %18s %s" % (words, approx, seen, verdict), flush=True)
