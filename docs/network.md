# What this app connects to

Vlocalhost records, transcribes and writes notes on your machine. No audio, no
transcript and no note is ever uploaded.

That is a claim, and a claim is only worth what you can check. So this page is
the complete list of connections the app is capable of making, the app will
print the same list on demand, and there is a switch that makes all of them
impossible.

```bash
python vlocalhost.py --network
```

---

## The list

| Connection | Goes to | When | Carries meeting content |
|---|---|---|---|
| Speech model download | huggingface.co | First run, or when you pick a model you don't have | No |
| Note model download | registry.ollama.ai, fetched by Ollama | Only from the setup wizard, when you ask for a model you don't have | No |
| Update check | api.github.com | Only when you press **Check for updates** | No |
| Note writing | Ollama, on `127.0.0.1` | Every time notes are written | **Yes** |
| Single-instance channel | `127.0.0.1` | At launch | No |
| Calendar sync | Google or Microsoft | Only with a paid package, and an account you connected | No |
| Emailing notes | Google or Microsoft | Only when you send notes yourself | **Yes** |

Two of those carry your meetings, and both are named plainly rather than buried:

- **Note writing** sends the transcript to a model. At the shipped setting that
  model is on this computer and the traffic never leaves it. `OLLAMA_URL` is
  yours to change, and if you point it at another machine your transcripts go
  there. Nothing else in the app can be redirected this way.
- **Emailing notes** sends your notes to the people you address them to. That
  is the entire purpose of the feature; it happens when you ask and not before.

Opening the guide, the pricing page or a support link hands a URL to your
browser. The app fetches nothing.

---

## Sealed Mode

```bash
python vlocalhost.py --set SEALED_MODE=true
```

Sealed, every connection above that could leave the machine is **refused at the
point of the call**, not merely left unused:

- the update check returns immediately and says why,
- the speech model loader will not fetch, and fails with an explanation if the
  model is genuinely absent,
- `get_provider` hands out nothing, so an installed calendar or email package
  cannot reach the network even by accident.

Recording, transcription, summarizing and notes on disk are unaffected, because
none of them needed a network to begin with.

**What it is for.** "We do not upload your meetings" is a sentence somebody else
has to take on trust. Sealed, it stops being a statement of intent and becomes a
property of the install: disconnect the machine and keep working. An
administrator can set it once across a fleet, and a hospital or a bank can set
it and stop asking.

**What it is not.** Sealing is a setting, and settings can be changed by whoever
controls the machine. It is a strong default and an enforceable policy, not a
sandbox.

---

## Checking it yourself

Do not take this page's word for it either. In rough order of effort:

**Seal it and pull the cable.** Turn on Sealed Mode, disconnect, and record a
meeting. Everything except the two optional integrations works exactly as
before. This is the whole argument, and it takes two minutes.

**Watch the traffic.** Run the app behind Wireshark, a proxy, or a host
firewall. What you should see is nothing, other than the entries above at the
moments this page says they happen.

**Read the source.** Every outbound call in this codebase is a `urlopen`,
`requests` or `socket` call, and `network.py` names each file allowed to contain
one. Grep for those yourself; the list is short.

**Let the app check itself.** `--network` ends with a contract check that scans
the tree for outbound-capable calls and fails if it finds one that isn't
declared. It reads source rather than watching traffic, so it is a drift
detector, not a proof — its job is to catch a future commit that adds a call and
forgets to describe it here.

---

## For administrators

`SEALED_MODE` is an ordinary setting. It lives in `settings.json` in the
per-user config directory, and it can be written by the same tooling you use for
anything else there:

```bash
python vlocalhost.py --set SEALED_MODE=true
python vlocalhost.py --network          # confirm, and record the output
```

The diagnostic report (`--diagnose`) states whether an install is sealed, so a
support ticket from a sealed machine reads correctly rather than looking like a
connectivity fault.
