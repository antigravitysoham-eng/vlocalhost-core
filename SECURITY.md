# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Email **soham.mitra@zeron.one** with:

- What the issue is and where in the code it lives
- How to reproduce it
- What an attacker could achieve

You will get an acknowledgement within 72 hours and an assessment within seven
days. If the report is valid we will agree a disclosure timeline with you, and
credit you in the release notes unless you would rather stay anonymous.

## Scope

This repository is Vlocalhost Core: the local recording, transcription and
summarization engine and its front ends.

Especially interested in reports about:

- Anything that causes audio, transcripts or notes to leave the machine
- Path traversal or arbitrary writes via note titles or calendar event titles
- Code execution through `CUSTOM_TRANSCRIBER`, the plugin loader, or the MCP
  server's tool arguments
- Credential or token material being written outside the per-user config
  directory, or with permissions that are too open

Out of scope: vulnerabilities in Ollama, faster-whisper, PortAudio or other
third-party dependencies — please report those upstream. Findings that require
an attacker to already have local code execution as the user are generally not
treated as vulnerabilities, since at that point the recording is theirs anyway.

## Design notes that are intentional

- **Core ships no network providers.** A Core install has no calendar or email
  integration compiled in. If you find a code path in this repository that
  performs an outbound network request with meeting content, that is a bug and
  we want to hear about it. (Model downloads on first run and the local Ollama
  call on `127.0.0.1` are expected.)
- **Notes are stored unencrypted** in `notes/` by design, so they stay greppable
  and outlive the app. Disk encryption is the operating system's job.
- **Users supply their own OAuth apps.** There is no shared cloud service and no
  vendor-held credentials to steal.
