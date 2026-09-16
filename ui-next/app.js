/* Vlocalhost — Aurora prototype.
 *
 * No framework, no build step: open index.html in a browser, or point an
 * embedded webview at it. Everything the real engine would push arrives
 * through the `engine` object at the bottom, so migrating means replacing
 * that one object with calls into Python and touching nothing else.
 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };

const mmss = s => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
const howLong = s => s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)} min`
  : `${Math.floor(s / 3600)}h ${String(Math.floor(s / 60) % 60).padStart(2, '0')}m`;

/* ------------------------------------------------------------ navigation */
const screens = {};
$$('.screen').forEach(s => (screens[s.id.replace('screen-', '')] = s));

function show(name) {
  Object.entries(screens).forEach(([k, s]) => (s.hidden = k !== name));
  $$('.nav button').forEach(b => {
    const on = b.dataset.screen === name;
    on ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current');
  });
  $('#body').scrollTop = 0;
}
$$('.nav button').forEach(b => b.addEventListener('click', () => show(b.dataset.screen)));
$('#back').addEventListener('click', () => show('library'));

/* ------------------------------------------------------------ disclosure */
/* One behaviour, used by every foldable section. The triangle points inward
 * when hidden and down when visible, and the button carries aria-expanded so
 * a screen reader announces the state rather than the glyph.
 *
 * Wired from the markup: the button names what it controls with aria-controls,
 * and its aria-expanded in the HTML is the starting state. Nothing here keeps
 * a list of section ids in step with the page. */
$$('.disclose[aria-controls]').forEach(btn => {
  const body = document.getElementById(btn.getAttribute('aria-controls'));
  if (!body) return;
  body.hidden = btn.getAttribute('aria-expanded') !== 'true';
  btn.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    body.hidden = !open;
  });
});

/* ----------------------------------------------------------- the meter */
/* 22 capsules: 18 live, 4 held back as the gate's tail. The last four stay
 * grey while the room is quiet, which is how "silence is not being recorded"
 * is shown rather than claimed. */
const BARS = 22, LIVE_BARS = 18;
const wave = $('#wave');
const bars = Array.from({ length: BARS }, (_, i) => {
  const b = el('i'); if (i >= LIVE_BARS) b.className = 'q';
  b.style.height = '8%'; wave.append(b); return b;
});

let level = null, quietFor = 0;
function meter(on) {
  clearInterval(level);
  if (!on) {
    // Idle reads as a flat quiet line, not as a row of equal amber dashes:
    // a meter showing the same bar everywhere looks like a broken meter.
    wave.classList.add('idle');
    bars.forEach(b => { b.style.height = '40%'; b.className = 'q'; });
    return;
  }
  wave.classList.remove('idle');
  bars.forEach((b, i) => (b.className = i >= LIVE_BARS ? 'q' : ''));
  level = setInterval(() => {
    // Stand-in for real input levels; speech-shaped rather than random noise.
    const talking = Math.random() > .18;
    quietFor = talking ? 0 : quietFor + 0.12;
    for (let i = 0; i < LIVE_BARS; i++) {
      const h = talking ? 18 + Math.random() * 78 : 6 + Math.random() * 8;
      bars[i].style.height = h.toFixed(0) + '%';
    }
    $('#gate').textContent = quietFor > 0.4
      ? `Silence gated · ${quietFor.toFixed(1)} s not transcribed`
      : `Capturing · ${sourceLabel()}`;
  }, 120);
}

/* -------------------------------------------------------------- recording */
/* The line the site leads with. Idle is the only time there is room for the
 * claim the whole product rests on, so it goes here rather than "Ready to
 * record", which says nothing a person did not already know. A live session
 * replaces it with the meeting's own title; a *finished* one must not. */
const IDLE_TITLE = 'Your meetings never leave this machine.';
const IDLE_META = 'Nothing is uploaded. No account, and no bot in the call.';

const recordBtn = $('#record');
const lines = $('#lines');
let recording = false, t0 = 0, clock = null, spoken = 0;

const SCRIPT = [
  ['00:00:04', 'Ana', 'Right — pricing. Where did we land?'],
  ['00:00:11', 'Soham', 'Two tiers. The middle one never converted.'],
  ['00:00:19', 'Ana', "And the installer? We can't ship unsigned."],
  ['00:00:26', 'Soham', "Certificate lands Thursday. I'll buy it today."],
  ['00:00:34', 'Ana', "Then Friday works. I'll redo the copy."],
];

function line(ts, who, text, cls) {
  $$('.ln.interim', lines).forEach(n => n.remove());   // a real line replaces the guess
  $$('.ln.hint', lines).forEach(n => n.remove());
  const row = el('div', 'ln' + (cls ? ' ' + cls : ''));
  row.append(el('span', 'ts', ts || ''));
  const p = el('p');
  if (who) { p.append(el('span', 'who', who)); p.append(document.createTextNode(' — ' + text)); }
  else p.textContent = text;
  row.append(p);
  $$('.ln.fresh', lines).forEach(n => n.classList.remove('fresh'));
  if (who) row.classList.add('fresh');
  lines.append(row);
  row.scrollIntoView({ block: 'nearest' });
}

recordBtn.addEventListener('click', () => (recording ? stop() : start()));

/* What the Source pop-up currently says, in the middle of a sentence. */
function sourceLabel() {
  const s = $('#rec-source');
  return s.options[s.selectedIndex].textContent.toLowerCase();
}

function start() {
  recording = true; t0 = Date.now(); spoken = 0;
  lines.replaceChildren();
  /* The source cannot change under a running capture, so the control says so
     by going unavailable rather than by accepting a change and ignoring it. */
  $('#rec-meta').classList.remove('lede');   // a live session's meta is a label
  $$('[data-setting="CAPTURE_MODE"]').forEach(c => (c.disabled = true));
  recordBtn.textContent = '■ Stop & save';
  recordBtn.classList.replace('primary', 'stop');
  $('#live-text').textContent = 'Live · 0 bytes out';
  meter(true);
  driver.start();

  clock = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $('#rec-meta').textContent =
      `${new Date(t0).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} → now · ${mmss(s)} · ${sourceLabel()}`;
    driver.tick(s);
  }, 250);
}

function stop() {
  recording = false;
  clearInterval(clock); meter(false);
  $$('[data-setting="CAPTURE_MODE"]').forEach(c => (c.disabled = false));
  const secs = Math.max(1, Math.floor((Date.now() - t0) / 1000));
  recordBtn.textContent = '● Start recording';
  recordBtn.classList.replace('stop', 'primary');
  $('#rec-title').textContent = IDLE_TITLE;
  $('#rec-meta').classList.remove('lede');
  $('#rec-meta').textContent = 'Saved. Summarizing below — you can start the next meeting now.';
  $('#live-text').textContent = 'Idle · 0 bytes out';
  $('#gate').textContent = 'Silence gated · nothing transcribed yet';
  line('', null, '— saved, summarizing in the background —', 'hint');
  driver.stop(secs);
}

/* ------------------------------------------------------------ the meetings */
const rows = $('#rows'), pendingEmpty = $('#pending-empty'), libraryRows = $('#library-rows');
const meetings = [];

function rowFor(m, into) {
  const row = el('button', 'r');
  row.type = 'button';
  const left = el('div');
  left.append(el('div', 't', m.title || 'Untitled meeting'));
  const s = el('div', 's ' + m.state);
  left.append(s);
  /* A meeting read back off disk has a modified time and no duration: nothing
     writes one down. Rather than invent it, the column simply gets shorter. */
  const stamp = new Date(m.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const when = el('span', 'when',
    m.duration ? `${stamp}  ·  ${howLong(m.duration)}` : stamp);
  row.append(left, when);
  row.addEventListener('click', () => {
    if (m.state !== 'ready') return;
    openMeeting(m);
  });
  into.prepend(row);
  m._row = row; m._state = s; m._title = left.firstChild;
  paint(m);
  return row;
}

function paint(m) {
  const s = m._state;
  s.className = 's ' + m.state;
  s.replaceChildren();
  if (m.state === 'queued') {
    s.append(el('span', null, '◷'), el('span', null,
      m.ahead ? `Queued — ${m.ahead} ahead. Notes are written one at a time.` : 'Queued'));
  } else if (m.state === 'working') {
    const sp = el('span', 'spin', '⟳');
    s.append(sp, el('span', null, `Summarizing… ${mmss(m.elapsed || 0)}`));
  } else if (m.state === 'ready' && m.summary === false) {
    /* Words saved, notes never written. Not a success: a mint tick here says
       "done" about the half of the job that did not happen. Amber and an open
       circle say what is true — the meeting is safe, the notes are missing. */
    s.className = 's partial';
    s.append(el('span', null, '○'),
             el('span', null, 'Transcript only · no summary written'));
  } else if (m.state === 'ready') {
    /* How many facts, only when they are cited facts. A summary parsed back
       out of the notes file carries claims but no timestamps -- Core's prompt
       forbids clock times -- so it says "Notes" and does not imply a pack it
       does not have. */
    const n = m.notes && m.notes.cited !== false && m.notes.facts
      ? `${m.notes.facts.length} checkable facts · open`
      : 'Notes · open';
    s.append(el('span', null, '✓'), el('span', null, n));
  } else if (m.state === 'failed') {
    s.append(el('span', null, '⚠'), el('span', null,
      `${m.error} Transcript saved.`));
  }
  if (m._title) m._title.textContent = m.title || 'Untitled meeting';
}

/* ---------------------------------------------------------- one meeting */
async function openMeeting(m) {
  /* A row from the library is a filename until it is opened. Parsing the notes
     file costs a read and a few hundred lines of matching, so it happens when
     somebody asks for the meeting rather than for all of them at startup. */
  if (!m.notes) {
    m.notes = await driver.notesFor(m);
    if (m.notes.title) m.title = m.notes.title;
  }
  $('#chrome-title').textContent = (m.title || 'Meeting').toUpperCase();
  $('#m-title').textContent = m.title;
  const at = new Date(m.at);
  $('#m-meta').textContent = [
    `${at.toLocaleDateString([], { weekday: 'long' })} `
    + at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    (m.notes.speakers || []).join(', '),
    m.duration ? howLong(m.duration) : '',
    'transcribed here',
  ].filter(Boolean).join(' · ');
  const pill = $('#m-pill');
  pill.textContent = m.notes.pill || '';
  pill.hidden = !m.notes.pill;
  $('#m-summary-text').textContent = m.notes.summary || '';

  /* The facts, each one cited. Aurora's caption: "Every line carries the second
   * it came from. That timestamp is the product." Which is also the constraint
   * — a citation can only come from an extracted pack, so a meeting without one
   * shows the role and the claim and simply omits the second, rather than
   * printing a plausible-looking time nobody said. */
  const facts = $('#m-facts');
  facts.replaceChildren();
  for (const f of m.notes.facts) {
    const box = el('div', 'fact' + (f.role === 'Risk' ? ' risk' : '') + (f.owner ? ' owed' : ''));
    box.append(el('div', 'k', f.role));
    const v = el('div', 'v');
    if (f.owner) {
      const cb = el('input'); cb.type = 'checkbox';
      cb.id = 'f' + Math.random().toString(36).slice(2);
      v.append(cb);
      const wrap = el('div', 'body-text');
      const lab = el('label', null, f.text); lab.htmlFor = cb.id;
      wrap.append(lab);
      // Inside the wrap, so it trails the commitment the way it trails every
      // other claim. Appended to the row instead it drifts to the far edge and
      // the pairing stops being obvious.
      if (f.at) wrap.append(el('span', 'cite', f.at));
      const who = [f.owner, f.due].filter(Boolean).join('  ·  ');
      if (who) wrap.append(el('div', 'who', who));
      v.append(wrap);
    } else {
      v.append(el('span', 'body-text', f.text));
      if (f.at) v.append(el('span', 'cite', f.at));
    }
    box.append(v);
    facts.append(box);
  }

  buildNext(m);
  show('meeting');
}

const PACK = `You are picking up after a meeting. Draft the follow-up email.

MEETING: Pricing review — 23 min, 2 speakers

DECIDED
- Hold the launch until the installer is signed (Soham, 00:00:26)
- Fold the middle tier into Pro (Ana, 00:00:11)

OWED
- Buy the signing certificate — Soham — Thursday (00:00:26)
- Redraft the pricing copy — Ana (00:00:34)

STILL OPEN
- Whether the Linux card stays "coming soon" through launch week`;

function buildNext(m) {
  const host = $('#m-next');
  host.replaceChildren();
  const row = el('div', 'actions'); row.style.marginTop = '0';
  const outcome = el('p', 'outcome'); outcome.hidden = true;
  let pre = null, copyBtn = null;

  for (const label of ['Copy for an assistant', 'Copy redacted', 'Context pack (JSON)']) {
    const b = el('button', 'btn', label);
    b.addEventListener('click', () => {
      $$('button', row).forEach(x => (x.disabled = true));
      outcome.hidden = false; outcome.className = 'outcome';
      outcome.textContent = `${label}… working`;
      setTimeout(() => {
        $$('button', row).forEach(x => (x.disabled = false));
        if (!pre) {
          pre = el('pre', 'preview'); pre.tabIndex = 0;
          host.append(pre);
          const cr = el('div', 'actions');
          copyBtn = el('button', 'btn', 'Copy to clipboard');
          copyBtn.addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(pre.textContent); } catch {}
            outcome.textContent =
              `Copied — ${pre.textContent.split(/\s+/).length} words, ready to paste. Nothing was sent.`;
          });
          cr.append(copyBtn); host.append(cr);
        }
        pre.textContent = PACK;
        /* Shown before it can be copied. Pasting a meeting into an assistant
         * that is not on this machine is an upload, so it gets read first and
         * the word count is the disclosure. */
        outcome.textContent =
          `${label} — ${PACK.split(/\s+/).length} words. Read it, then copy. Nothing has been sent.`;
      }, 800);
    });
    row.append(b);
  }
  host.append(row, outcome);
}

/* ============================================================== SETTINGS ==
 *
 * Every control carries `data-setting="KEY"`, and KEY is a real name from
 * core/settings.py's EDITABLE tuple. That list is the whole contract: a
 * setting that only exists as a line in config.py is reverted by the next
 * update, so anything the app tells someone to configure has to be reachable
 * from here.
 *
 * Three behaviours make the screen honest rather than merely complete:
 * a setting saves the moment it changes and says so; a setting that governs
 * others visibly governs them; and a setting the machine cannot honour right
 * now is dimmed with the reason, not silently ignored.
 */
const savedLine = $('#saved');
savedLine.hidden = false;               // reserved height from here on, so
let savedTimer = null;                  // showing it never shifts the layout

function saved(msg) {
  savedLine.textContent = msg;
  savedLine.classList.add('on');
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => savedLine.classList.remove('on'), 3200);
}

const fieldOf = n => n.closest('.field');
const labelOf = n => {
  const f = fieldOf(n);
  const l = f && (f.querySelector(':scope > label') || f.querySelector(':scope > .label-as-text'));
  return l ? l.textContent.trim() : 'Setting';
};
const valueOf = n =>
  n.type === 'checkbox' ? (n.checked ? 'on' : 'off')
  : n.tagName === 'SELECT' ? n.options[n.selectedIndex].textContent.trim()
  : n.value || 'empty';

/* Dim a group and say why, rather than letting someone set something that
   will not be honoured. */
function govern(fields, allowed, reason) {
  for (const f of fields) {
    if (!f) continue;
    f.classList.toggle('off', !allowed);
    $$('input, select, button', f).forEach(c => (c.disabled = !allowed));
  }
  if (reason) reason.hidden = allowed;
}

/* The two Capture source controls are one setting in two places — the Record
   screen, where it is task-specific, and Settings, where it is findable. */
const sourceControls = $$('[data-setting="CAPTURE_MODE"]');
sourceControls.forEach(sel => sel.addEventListener('change', () => {
  sourceControls.forEach(o => (o.value = sel.value));
}));

$$('#screen-settings [data-setting]').forEach(node => {
  /* A range fires on every pixel of the drag. Writing settings.json that
     often is silly, so the value is reported live and written on release. */
  node.addEventListener('input', () => {
    if (node.type === 'range') saved(`${labelOf(node)} → ${valueOf(node)}`);
  });
  node.addEventListener('change', () => driver.saveSetting(node));
});

$('#set-thresh').addEventListener('input', e =>
  ($('#thresh-out').textContent = Number(e.target.value).toFixed(2)));

/* Sealed mode refuses every outbound connection, so everything that needs one
   goes dark. The update reminder stays: it is computed from the date on this
   machine and fetches nothing. */
const sealedNote = el('p', 'note warn',
  'Sealed mode is on. Nothing below can run until it is turned off.');
$('#s-deliver').prepend(sealedNote);

const deliverMaster = $('#set-deliver');
const deliverFields = [fieldOf($('#set-deliver')), fieldOf($('#deliver-group').querySelector('input')),
                       fieldOf($('#set-cal')), fieldOf($('#set-autostart'))];
const subFields = [fieldOf($('#deliver-group').querySelector('input')),
                   fieldOf($('#set-cal')), fieldOf($('#set-autostart'))];

function applyDelivery() {
  const sealed = $('#set-sealed').checked;
  govern(deliverFields, !sealed, sealedNote);
  if (!sealed) govern(subFields, deliverMaster.checked, null);
}
$('#set-sealed').addEventListener('change', applyDelivery);
deliverMaster.addEventListener('change', applyDelivery);
applyDelivery();

const hotkeyField = [$('#hotkey-field')];
const applyHotkey = () => govern(hotkeyField, $('#set-hotkey-on').checked, null);
$('#set-hotkey-on').addEventListener('change', applyHotkey);
applyHotkey();

/* What "Model" means depends on the engine above it, so the field says so
   instead of leaving a path in a box labelled for a model name. */
const ENGINES = {
  ollama: ['Ollama manages its own models. Nothing is downloaded here.',
           'llama3.2', 'Any model Ollama has already pulled.'],
  ctranslate2: ['Runs in this process. The weights have to be on this machine.',
                'C:\\models\\qwen2.5-3b-ct2', 'The converted model folder.'],
  custom: ['Named under Advanced, below.', '', 'Set "Notes engine" in Advanced instead.'],
};
$('#set-engine').addEventListener('change', e => {
  const [hint, placeholder, modelHint] = ENGINES[e.target.value];
  $('#engine-hint').textContent = hint;
  $('#set-model').placeholder = placeholder;
  $('#set-model').value = '';
  $('#model-hint').textContent = modelHint;
  govern([$('#model-field')], e.target.value !== 'custom', null);
});

$('#choose-out').addEventListener('click', () =>
  saved('The system folder picker opens here in the real window.'));

/* =================================================================== ASK ==
 *
 * Cross-meeting questions, answered from packs already cached on this machine
 * (next_actions/across.py — it reads `store`, and never extracts, which is why
 * an answer arrives in milliseconds). The module states two rules at the top
 * and this screen is built to keep both of them visible:
 *
 *   "Every answer says what it searched."   -> the scope line, before the answer
 *   "Every fact keeps its citation."        -> meeting and second, on every line
 */
const ARCHIVE = { total: 41, indexed: 38 };

/* The seam again. In the real window each of these is one call into
   across.py, and none of them involves a model — they are searches over
   extracted facts, which is why nothing here can hallucinate a commitment. */
const ANSWERS = [
  { on: /\bowe|owed|owing\b/i, fn: 'open_action_items',
    head: 'Three commitments to Priya are still open, oldest first.',
    facts: [
      { role: 'Owed', text: 'Send the revised procurement clause', who: 'Soham', due: 'was due 4 Sep',
        meeting: 'Northwind — contract', at: '00:18:22' },
      { role: 'Owed', text: 'Confirm whether the Linux card ships in launch week', who: 'Soham',
        meeting: 'Launch checkpoint', at: '00:06:03' },
      { role: 'Owed', text: 'Share the signing certificate receipt once it lands', who: 'Soham',
        due: 'Thursday', meeting: 'Pricing review', at: '00:00:26' },
    ] },
  { on: /\bdecide|decided|decision\b/i, fn: 'search_decisions',
    head: 'Four decisions mention pricing. Two of them were later reversed, and say so.',
    facts: [
      { role: 'Decided', text: 'Fold the middle tier into Pro and drop it from the page.',
        meeting: 'Pricing review', at: '00:00:11' },
      { role: 'Decided', text: 'Hold the launch until the installer is signed.',
        meeting: 'Pricing review', at: '00:00:26' },
      { role: 'Decided', text: 'Price per machine, not per seat — reversed two weeks later.',
        meeting: 'Pricing — first pass', at: '00:31:40' },
      { role: 'Decided', text: 'No trial. A free tier instead.',
        meeting: 'Kickoff — Northwind', at: '00:12:55' },
    ] },
  { on: /\bstill open|unresolved|open question/i, fn: 'open_questions',
    head: 'Two questions about the installer were raised and never answered.',
    facts: [
      { role: 'Still open', text: 'Whether notarization is needed for the DMG or only for the app bundle.',
        meeting: 'macOS build', at: '00:22:14' },
      { role: 'Still open', text: 'Who pays for the certificate, and out of which budget.',
        meeting: 'Pricing review', at: '00:00:19' },
    ] },
  { on: /\bmove|over (six )?weeks|timeline|change[d]? over/i, fn: 'meeting_timeline',
    head: 'The Linux argument across six weeks, in the order it was actually made.',
    facts: [
      { role: 'Decided', text: 'Ship Linux at launch — it is the differentiator.',
        meeting: 'Kickoff — Northwind', at: '00:41:02' },
      { role: 'Risk', text: 'No one on the team runs Linux daily, so nothing is being tested.',
        meeting: 'Launch checkpoint', at: '00:14:31' },
      { role: 'Decided', text: 'Build it, attach it to the release, keep the card at "coming soon".',
        meeting: 'Launch checkpoint', at: '00:16:08' },
      { role: 'Still open', text: 'When the card stops saying "coming soon".',
        meeting: 'Pricing review', at: '00:00:19' },
    ] },
  { on: /\bbrief|before I walk|what happened last time/i, fn: 'brief_me',
    head: 'Northwind, from three meetings. What was settled, and what you still owe them.',
    facts: [
      { role: 'Decided', text: 'No trial. A free tier instead.',
        meeting: 'Kickoff — Northwind', at: '00:12:55' },
      { role: 'Owed', text: 'Send the revised procurement clause', who: 'Soham', due: 'was due 4 Sep',
        meeting: 'Northwind — contract', at: '00:18:22' },
      { role: 'Risk', text: 'Their legal review closes 20 Sep. Nothing has been sent.',
        meeting: 'Northwind — contract', at: '00:44:50' },
    ] },
  { on: /\bstale|overdue|slipped/i, fn: 'stale_commitments',
    head: 'Two commitments are older than 14 days and nothing later looks like an answer to them.',
    facts: [
      { role: 'Owed', text: 'Send the revised procurement clause', who: 'Soham', due: '17 days ago',
        meeting: 'Northwind — contract', at: '00:18:22' },
      { role: 'Owed', text: 'Write the support policy page', who: 'Ana', due: '22 days ago',
        meeting: 'Launch checkpoint', at: '00:29:07' },
    ] },
];

const answerHost = $('#answer');

function scopeLine() {
  const s = el('div', 'scope');
  s.append(document.createTextNode('Searched '));
  s.append(el('b', null, String(ARCHIVE.indexed)));
  s.append(document.createTextNode(` of ${ARCHIVE.total} meetings`));
  const missing = ARCHIVE.total - ARCHIVE.indexed;
  if (missing) {
    s.append(el('span', 'gap', `· ${missing} not indexed, and not included below`));
  }
  return s;
}

/* The same fact pattern the meeting screen uses, with one addition: across an
   archive the citation has to name the meeting as well as the second, because
   "00:18:22" on its own does not identify anything. */
function crossFact(f) {
  const box = el('div', 'fact cross' + (f.role === 'Risk' ? ' risk' : ''));
  box.append(el('div', 'k', f.role));
  const v = el('div', 'v');
  v.append(el('span', 'body-text', f.text));
  const cite = el('span', 'cite');
  cite.append(el('span', 'mt', f.meeting), document.createTextNode(' · ' + f.at));
  v.append(cite);
  box.append(v);
  const who = [f.who, f.due].filter(Boolean).join('  ·  ');
  if (who) box.append(el('div', 'who', who));
  return box;
}

function renderAnswer(hit, question) {
  answerHost.replaceChildren();
  if (!hit) {
    /* generative-ai.md › Outputs: "Help people improve requests when blocked or
       undesirable results occur … coaching people how to be more successful
       next time." Nothing matched is not an error, and it is not an excuse to
       write a plausible paragraph instead. */
    const e = el('div', 'empty');
    e.append(el('strong', null, 'Nothing in the archive answers that.'));
    e.append(document.createTextNode(
      `No decision, commitment, open question or risk matches “${question}”. `
      + 'These searches read extracted facts, so they find what was said and '
      + 'nothing else — try naming a person, a topic, or a meeting.'));
    answerHost.append(scopeLine(), e);
    return;
  }
  answerHost.append(scopeLine());
  answerHost.append(el('p', 'answer-head', hit.head));
  hit.facts.forEach(f => answerHost.append(crossFact(f)));
  answerHost.append(el('p', 'note',
    `Assembled by ${hit.fn}() from cached packs. Every line above names the meeting `
    + 'and the second it came from. No model wrote this, and nothing was sent.'));
}

function ask(question) {
  const q = question.trim();
  if (!q) return;
  $('#q').value = q;

  /* generative-ai.md › Outputs: "Messages that describe what's actually
     happening can be more helpful than a vague status message." */
  answerHost.replaceChildren();
  const w = el('div', 'working');
  w.append(el('span', 'spin', '⟳'),
           el('span', null, `Reading ${ARCHIVE.indexed} context packs…`));
  answerHost.append(w);

  setTimeout(() => renderAnswer(ANSWERS.find(a => a.on.test(q)), q), 420);
}

$('#ask-form').addEventListener('submit', e => { e.preventDefault(); ask($('#q').value); });
$$('#chips .chip').forEach(c => c.addEventListener('click', () => ask(c.dataset.q)));

/* Indexing is something a person asks for, not something that happens to
   them: it is one extraction per meeting and each one costs real seconds on a
   local model. So it says how many and how long before it starts. */
const indexBtn = $('#index-btn'), indexT = $('#index-t'), indexS = $('#index-s');
indexBtn.addEventListener('click', () => {
  const todo = ARCHIVE.total - ARCHIVE.indexed;
  indexBtn.disabled = true;
  let done = 0;
  const step = () => {
    done++;
    ARCHIVE.indexed++;
    indexT.textContent = `${ARCHIVE.indexed} of ${ARCHIVE.total} meetings indexed`;
    if (done < todo) {
      indexS.textContent = `Extracting ${done + 1} of ${todo} — about 45 seconds each, on this machine.`;
      setTimeout(step, 1100);
    } else {
      $('#index-strip').classList.add('complete');
      indexS.textContent = 'Every meeting on this machine can be searched and cited.';
      indexBtn.remove();
    }
  };
  indexS.textContent = `Extracting 1 of ${todo} — about 45 seconds each, on this machine.`;
  setTimeout(step, 1100);
});

/* ========================================================== ASSISTANTS ====
 *
 * Three questions, in the order somebody actually asks them: how do I connect
 * one, what will it be able to see, and what has it looked at. The third is
 * the one no config-file integration ever answers.
 */
const HOSTS = [
  { name: 'Claude Desktop', where: 'claude_desktop_config.json', on: true, since: 'connected 9 Sep' },
  { name: 'Claude Code', where: '.mcp.json in a project', on: false },
  { name: 'Cursor', where: '~/.cursor/mcp.json', on: false },
  { name: 'VS Code — Continue', where: 'config.yaml', on: false },
];

const AUDIT = [
  { when: '14:31', who: 'Claude Desktop', what: 'read the pack for “Pricing review”' },
  { when: '14:30', who: 'Claude Desktop', what: 'listed 38 meetings' },
  { when: '09:12', who: 'Claude Desktop', what: 'read the notes for “Northwind — contract”' },
];

const hostRows = $('#host-rows'), auditRows = $('#audit-rows'), auditEmpty = $('#audit-empty');

function drawHosts() {
  hostRows.replaceChildren();
  for (const h of HOSTS) {
    const row = el('div', 'r static');
    const left = el('div');
    left.append(el('div', 't', h.name));
    const s = el('div', 's ' + (h.on ? 'ready' : ''));
    s.append(el('span', null, h.on ? '✓' : '○'),
             el('span', null, h.on ? h.since : `Not connected · ${h.where}`));
    left.append(s);
    const act = el('div', 'act');
    const b = el('button', 'btn', h.on ? 'Disconnect' : 'Connect');
    b.type = 'button';
    b.addEventListener('click', () => {
      h.on = !h.on;
      if (h.on) {
        h.since = 'connected just now';
        AUDIT.unshift({ when: 'just now', who: h.name, what: `listed ${ARCHIVE.total} meetings` });
        drawAudit();
      }
      drawHosts();
    });
    act.append(b);
    row.append(left, act);
    hostRows.append(row);
  }
}

function drawAudit() {
  auditRows.replaceChildren();
  auditEmpty.hidden = AUDIT.length > 0;
  for (const a of AUDIT) {
    const row = el('div', 'r static audit-r');
    const left = el('div');
    left.append(el('div', 't', a.who));
    left.append(el('div', 's', a.what));
    row.append(left, el('span', 'when', a.when));
    auditRows.append(row);
  }
}

drawHosts();
drawAudit();

/* How much of the archive is in reach, said as a count of meetings rather than
   as a number of days — days are the setting, meetings are the consequence. */
const INSIDE = { 0: 41, 7: 4, 30: 12, 90: 27, 180: 36 };
const scopeSel = $('#scope-days'), scopeHint = $('#scope-hint');
function drawScope() {
  const d = scopeSel.value, n = INSIDE[d];
  scopeHint.textContent = d === '0'
    ? `Every meeting on this machine — all ${ARCHIVE.total} of them.`
    : `${n} of ${ARCHIVE.total} meetings. The other ${ARCHIVE.total - n} are refused, `
      + 'and the refusal says so rather than pretending they do not exist.';
}
scopeSel.addEventListener('change', drawScope);
drawScope();

const MCP_JSON = `{
  "mcpServers": {
    "vlocalhost": {
      "command": "C:\\\\Program Files\\\\Vlocalhost\\\\vlocalhost.exe",
      "args": ["--mcp"]
    }
  }
}`;
$('#mcp-json').textContent = MCP_JSON;
$('#copy-json').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(MCP_JSON); } catch {}
  const o = $('#json-outcome');
  o.hidden = false;
  o.textContent = 'Copied. It names this machine\u2019s own binary — nothing is fetched.';
});

/* =============================================================== the seam ==
 *
 * Everything above is UI. Everything below is where the words come from.
 *
 * Two drivers behind one interface. `demo` is the prototype: a scripted
 * meeting, a random meter, a five-second summary — it is what runs when the
 * page is opened in a browser, and it is what the review screenshots capture.
 * `live` is the real one: it calls into Python through `window.pywebview.api`
 * and takes its events back on a single `vl` CustomEvent.
 *
 * The page above cannot tell which it is holding, and that is the point: what
 * ships is the code path that was reviewed.
 */

let LIVE = false;
const api = () => window.pywebview && window.pywebview.api;

/* -------------------------------------------------------------- demo data */
const NOTES = {
  pill: 'Follow-up ready to draft',
  speakers: ['Soham', 'Ana'],
  cited: true,
  summary:
    'We walked the pricing page end to end and agreed to hold the launch until the '
    + 'installer is signed. Two tiers tested better than three, so the middle tier is '
    + 'being folded into Pro. Procurement was not discussed and is the gap both of us noticed.',
  facts: [
    { role: 'Decided', text: 'Hold the launch until the installer is signed.', at: '00:00:26' },
    { role: 'Decided', text: 'Fold the middle tier into Pro and drop it from the page.', at: '00:00:11' },
    { role: 'Owed', text: 'Buy the signing certificate', owner: 'Soham', due: 'Thursday', at: '00:00:26' },
    { role: 'Owed', text: 'Redraft the pricing copy for two tiers', owner: 'Ana', at: '00:00:34' },
    { role: 'Still open', text: 'Whether the Linux card stays "coming soon" through launch week.', at: '00:00:19' },
    { role: 'Risk', text: 'Unsigned binaries could hold the release past Friday.', at: '00:00:19' },
  ],
};

/* One pending row, however the meeting got there. */
function pendingRow(m) {
  meetings.push(m);
  pendingEmpty.hidden = true; rows.hidden = false;
  rowFor(m, rows);
  return m;
}

/* Queued meetings count down in the order they stopped, in both drivers. */
function renumber() {
  let ahead = 0;
  for (const m of meetings) {
    if (m.state === 'queued') { m.ahead = ahead; paint(m); }
    if (m.state === 'queued' || m.state === 'working') ahead++;
  }
}

/* ------------------------------------------------------------ demo driver */
/* Notes are written ONE AT A TIME — two local model calls at once makes both
 * slow rather than either fast — so a second meeting queues and says so. */
const demo = {
  busy: false,
  waiting: [],

  start() { $('#rec-title').textContent = 'Pricing review'; },

  tick(s) {
    if (spoken < SCRIPT.length && s >= spoken * 3 + 2) {
      const [ts, who, text] = SCRIPT[spoken++];
      line(ts, who, text);
    }
  },

  stop(secs) {
    const m = pendingRow({ title: 'Pricing review', duration: secs,
                           at: Date.now(), state: 'queued', ahead: 0 });
    this.waiting.push(m);
    renumber();
    this.pump();
  },

  async notesFor() { return NOTES; },

  saveSetting(node) { saved(labelOf(node) + ' → ' + valueOf(node) + '. Saved.'); },

  pump() {
    if (this.busy) return;
    const m = this.waiting.shift();
    if (!m) return;
    this.busy = true;
    m.state = 'working'; m.elapsed = 0; paint(m); renumber();
    const t = setInterval(() => { m.elapsed++; paint(m); }, 1000);

    setTimeout(() => {
      clearInterval(t);
      m.state = 'ready';
      m.notes = NOTES;
      paint(m);
      rowFor(m, libraryRows);            // it joins the library once it exists
      this.busy = false;
      renumber();
      this.pump();
    }, 5000);
  },
};

/* ------------------------------------------------------------ live driver */
/* The engine reports no input level — there is no RMS and no VAD probability
 * on any callback it offers — so the meter cannot show one without a small
 * addition to Core's audio path. Until then it does not pretend. It holds a
 * calm floor and pulses on speech that was actually detected, and the gate
 * line says which of the two is happening. A meter animating on random
 * numbers would be the one element on this screen that lies. */
function pulse() {
  if (!recording) return;
  bars.forEach((b, i) => {
    if (i >= LIVE_BARS) return;
    b.style.height = (22 + Math.random() * 60).toFixed(0) + '%';
  });
  clearTimeout(pulse._t);
  pulse._t = setTimeout(() => {
    if (!recording) return;
    bars.forEach((b, i) => { if (i < LIVE_BARS) b.style.height = '10%'; });
  }, 900);
}

const live = {
  jobs: new Map(),          // engine job id -> the row showing it

  async start() {
    $('#rec-title').textContent = 'Starting…';
    $('#gate').textContent = 'Loading the speech model…';
    const r = await api().start();
    if (r && r.error) fail(r.error);
  },

  tick() {},

  async stop() {
    const r = await api().stop();
    if (r && r.error) { fail(r.error); return; }
    const m = pendingRow({
      title: r.title || 'Untitled meeting', duration: r.recorded || null,
      at: Date.now(), state: 'queued', ahead: 0, job: r.job, base: null,
    });
    if (r.job !== null && r.job !== undefined) this.jobs.set(r.job, m);
    renumber();
  },

  async notesFor(m) {
    if (!m.base) return { summary: '', facts: [], speakers: [], cited: false };
    const got = await api().meeting(m.base);
    return {
      title: got.title,
      summary: got.summary,
      facts: got.facts || [],
      speakers: [],
      /* Said out loud rather than shown as an absence. This design is built on
         cited facts; these are claims without citations, because Core's
         summariser is forbidden from writing clock times. */
      pill: got.missing ? 'No summary was written' : '',
      cited: false,
    };
  },

  async saveSetting(node) {
    const key = node.dataset.setting;
    const value = node.type === 'checkbox' ? node.checked : node.value;
    const r = await api().settings_set({ [key]: value });
    if (r && r.error) { saved(labelOf(node) + ' — not saved: ' + r.error); return; }
    saved(labelOf(node) + ' → ' + valueOf(node) + '. Saved.');
    if (key === 'SEALED_MODE') applyDelivery();
  },

  /* Everything Python pushes arrives here. */
  event(type, p) {
    if (type === 'line') { line('', null, p.text); pulse(); }
    else if (type === 'level') realLevel(p.db, p.speech);
    else if (type === 'pull') suPull(p);
    else if (type === 'partial') { interim(p.text, p.who); pulse(); }
    else if (type === 'state') {
      if (p.listening) {
        $('#rec-title').textContent = p.title || 'Recording';
        $('#gate').textContent = 'Capturing · waiting for speech';
      }
    }
    else if (type === 'notes') this.notes(p);
    else if (type === 'error') fail(p.message);
    else if (type === 'closing' && p.pending) {
      $('#rec-meta').textContent =
        'Finishing ' + p.pending + ' summary' + (p.pending > 1 ? 'ies' : '')
        + ' before closing…';
    }
  },

  notes(p) {
    const m = this.jobs.get(p.job);
    if (!m) return;
    if (p.title) m.title = p.title;
    if (p.state === 'running') {
      m.state = 'working'; m.elapsed = m.elapsed || 0;
      clearInterval(m._t);
      m._t = setInterval(() => { m.elapsed++; paint(m); }, 1000);
    } else if (p.state === 'done') {
      clearInterval(m._t);
      m.state = 'ready';
      m.summary = !!p.summary;
      m.base = (p.summary || p.transcript || '')
        .replace(/-(summary|transcript)\.(txt|md)$/, '') || null;
      m.notes = null;                       // parsed when it is opened
      rowFor(m, libraryRows);
    } else if (p.state === 'failed') {
      clearInterval(m._t);
      m.state = 'failed';
      m.error = p.error || 'The summary could not be written.';
    }
    paint(m);
    renumber();
  },

  async loadLibrary() {
    const list = await api().library(200);
    libRows = [];
    for (const r of list) {
      const m = {
        base: r.base, title: r.title, at: Date.parse(r.modified) || Date.now(),
        duration: null, state: 'ready', summary: !!r.summary, notes: null,
      };
      meetings.push(m);
      libRows.push(m);
    }
    wireLibraryFilter();
    drawLibrary();
    tally(list);
  },

  async loadSettings() {
    const got = await api().settings_get();
    const values = (got && got.values) || {};
    for (const node of $$('#screen-settings [data-setting]')) {
      const v = values[node.dataset.setting];
      if (v === undefined) continue;
      if (node.type === 'checkbox') node.checked = !!v;
      else if (node.type === 'radio') node.checked = (node.value === String(v));
      else node.value = v === null ? '' : String(v);
    }
    $('#thresh-out').textContent = Number($('#set-thresh').value).toFixed(2);
    sourceControls.forEach(o => (o.value = values.CAPTURE_MODE || 'both'));
    applyDelivery(); applyHotkey();

    /* The device list is this machine's, not a guess. entering-data.md:
       offer choices instead of requiring text entry. */
    const d = await api().devices();
    const sel = $('#set-device');
    if (d && d.devices && d.devices.length) {
      const keep = sel.value;
      const first = el('option', null, 'System default');
      first.value = '';
      sel.replaceChildren(first);
      for (const dev of d.devices) {
        const o = el('option', null, dev.name);
        o.value = (dev.index === null || dev.index === undefined)
          ? dev.name : String(dev.index);
        sel.append(o);
      }
      sel.value = keep;
    }
  },
};

/* One place where a failure lands, so a broken start cannot leave the button
   saying "Stop & save" over a microphone that was never opened. */
function fail(message) {
  $('#rec-title').textContent = IDLE_TITLE;
  $('#gate').textContent = message;
  if (recording) {
    recording = false;
    clearInterval(clock);
    meter(false);
    recordBtn.textContent = '● Start recording';
    recordBtn.classList.replace('stop', 'primary');
    $$('[data-setting="CAPTURE_MODE"]').forEach(c => (c.disabled = false));
  }
}

let driver = demo;

/* What the native menu drives. The menu asks the page to do what the button
 * would have done, so the window and the menu bar can never disagree. */
window.vl = {
  menu(what) {
    if (what === 'record') recordBtn.click();
    else if (what === 'folder') $('#open-folder').click();
    else if (what.indexOf('screen:') === 0) show(what.slice(7));
  },
};

$('#open-folder').addEventListener('click', () => {
  if (LIVE) api().open_notes_folder();
});

/* ------------------------------------------------------------------- boot */
meter(false);   // grey and flat until the microphone is actually open

window.addEventListener('vl', e => {
  if (LIVE) live.event(e.detail.type, e.detail.payload || {});
});

/* pywebview injects its bridge after the document is ready and announces it.
 * Until that fires the page is the prototype — which is also what happens in a
 * browser, where it never fires at all. */
window.addEventListener('pywebviewready', async () => {
  LIVE = true;
  driver = live;
  document.documentElement.dataset.live = 'true';
  try {
    await live.loadSettings();
    await live.loadLibrary();
    const st = await api().status();
    if (st && st.listening) {       // the tray or the hotkey got here first
      recording = true;
      t0 = Date.now() - (st.elapsed * 1000);
      recordBtn.textContent = '■ Stop & save';
      recordBtn.classList.replace('primary', 'stop');
      $('#rec-title').textContent = st.title || 'Recording';
      $('#live-text').textContent = 'Live · 0 bytes out';
      meter(true);
    }
  } catch (err) {
    $('#gate').textContent = 'Could not reach the engine: ' + err;
  }
});


/* Demo only: something in the library so the screen is not empty in a browser.
 * The live driver replaces the list wholesale from what is on disk. */
if (!window.pywebview) {
  meetings.push({
    title: 'Kickoff — Northwind', at: Date.now() - 864e5, duration: 2460,
    state: 'ready', notes: NOTES,
  });
  rowFor(meetings[0], libraryRows);
}

/* ================================================== the library tally ==== *
 *
 * Three numbers read off the rows the library already returned, and one
 * claim. No new call, no estimate, and deliberately no streak: a counter you
 * can break punishes the week somebody had no meetings, and this product has
 * no business making anyone feel that. What accumulates here is worth showing
 * precisely because it is theirs and it is local -- which is what the claim
 * on the right says, standing next to the pile it is about.
 */
function bytesLabel(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

function tally(list) {
  const box = $('#library-tally');
  if (!box) return;
  const rows = list || [];
  /* The tally and the empty state are the same decision seen from two sides,
   * so they are made in one place and can never both be showing. */
  libraryEmpty(!rows.length);
  if (!rows.length) { box.hidden = true; return; }
  $('#tally-meetings').textContent = rows.length;
  $('#tally-notes').textContent = rows.filter(r => r.summary).length;
  $('#tally-size').textContent = bytesLabel(rows.reduce((a, r) => a + (r.bytes || 0), 0));
  box.hidden = false;
}

/* =================================================== this machine ======== *
 *
 * The parity panel. Every handler is one bridge call and one sentence back,
 * because a button that does something invisible reads as a button that is
 * broken. Failures say what failed rather than reverting silently.
 */
const machEl = id => $('#' + id);

function said(node, text, kind) {
  if (!node) return;
  node.textContent = text;
  node.className = 'val' + (kind ? ' ' + kind : '');
}

async function machine(btn, valueId, work, busyText) {
  const val = machEl(valueId);
  if (btn) btn.disabled = true;
  if (busyText) said(val, busyText, 'busy');
  try {
    const r = await work();
    if (r && r.error) said(val, r.error, 'bad');
    return r;
  } catch (e) {
    said(val, String(e), 'bad');
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function wireMachine() {
  if (!$('#mach-version')) return;

  /* The area-of-work suggestions come from Core, so Settings offers the same
   * twelve the first-run step did. It is a datalist, not a select: someone
   * whose job is not on the list must be able to type it. */
  api().setup_options().then(o => {
    if (!o) return;
    const fill = (id, values) => {
      const dl = $('#' + id);
      if (!dl || !values) return;
      dl.replaceChildren();
      for (const v of values) { const opt = el('option'); opt.value = v; dl.append(opt); }
    };
    fill('user-fields', o.user_fields);
    fill('user-tones', o.user_tones);
  }).catch(() => {});

  /* Version and engine state are read at boot -- neither touches the network. */
  const refresh = async () => {
    const v = await api().version();
    if (v && !v.error) said(machEl('mach-version'), v.line || v.version || '—');
    const e = await api().engine_check();
    const r = e && e.result;
    if (Array.isArray(r)) said(machEl('mach-engine'), r[1] || '', r[0] ? 'ok' : 'bad');
    const p = await api().performance();
    if (p && !p.error) {
      said(machEl('mach-perf'), `${p.profile} · ~${p.ram_mb} MB`);
      machEl('mach-perf').title = p.describes || '';
    }
  };

  machEl('mach-update').onclick = e => machine(e.target, 'mach-version', async () => {
    const r = await api().check_updates();
    if (r && r.error) return r;
    said(machEl('mach-version'),
         r.current ? `${r.version} — up to date` : `${r.latest} available`,
         r.current ? 'ok' : '');
    return r;
  }, 'checking…');

  machEl('mach-engine-check').onclick = e => machine(e.target, 'mach-engine', async () => {
    const r = await api().engine_check();
    const got = r && r.result;
    if (Array.isArray(got)) said(machEl('mach-engine'), got[1] || '', got[0] ? 'ok' : 'bad');
    return r;
  }, 'checking…');

  machEl('mach-bench').onclick = e => machine(e.target, 'mach-perf', async () => {
    const r = await api().benchmark();
    if (r && !r.error) {
      said(machEl('mach-perf'),
           `${r.realtime}× real time · peak ${Math.round(r.peak_mb)} MB`, 'ok');
    }
    return r;
  }, 'measuring…');

  machEl('mach-notes-folder').onclick = () => api().open_notes_folder();
  machEl('mach-config-folder').onclick = () => api().open_config_folder();
  machEl('mach-guide').onclick = () => api().support('guide');
  machEl('mach-support').onclick = () => api().support('');
  machEl('mach-report').onclick = e => machine(e.target, 'mach-version', async () => {
    const r = await api().save_report();
    if (r && r.path) saved('Problem report saved — the folder is open.');
    return r;
  });

  refresh();
}

window.addEventListener('pywebviewready', wireMachine);

/* In a browser there is no bridge, so the panel would sit dead and look
 * broken. Say so rather than leaving three em-dashes with no explanation. */
if (!window.pywebview) {
  window.addEventListener('DOMContentLoaded', () => {
    for (const id of ['mach-version', 'mach-engine', 'mach-perf']) {
      said(machEl(id), 'only in the app', 'busy');
    }
    for (const b of $$('#screen-settings .ctl.act .btn')) b.disabled = true;
  });
}

/* ================================================ assistants, for real ==== *
 *
 * The screen shipped drawing HOSTS and MCP_JSON, which are prototype
 * constants. Live, both come from Core: `mcp_hosts.available_hosts()` knows
 * every destination this build can offer -- including the ones Pro registers
 * at import time, which is why the list is longer inside the app than it is
 * in a browser -- and each host carries the block it wants, because a few
 * clients take the standard shape under a different key.
 *
 * Nothing here connects to anything. It reads paths off this machine and
 * composes text for the user to paste, which is the same thing the tkinter
 * Connections tab did and the reason it could be honest about "0 bytes out".
 */
async function loadAssistants() {
  const rowsBox = $('#host-rows');
  if (!rowsBox || !api()) return;
  const got = await api().mcp();
  if (!got || got.error) return;

  const hosts = got.hosts || [];
  if (hosts.length) {
    rowsBox.replaceChildren();
    for (const h of hosts) {
      const row = el('div', 'r static');
      const left = el('div');
      left.append(el('div', 't', h.name));
      const s = el('div', 's');
      /* No "connected" claim. Reading another program's config file to decide
       * whether it is wired up is a guess, and a green tick that is a guess is
       * worse than no tick -- so this says where it goes and stops there. */
      s.append(el('span', null, '○'), el('span', null, h.where || 'your MCP config'));
      left.append(s);
      const act = el('div', 'act');
      const b = el('button', 'btn', 'Show configuration');
      b.onclick = () => {
        $('#mcp-json').textContent = h.snippet || got.config || '';
        const note = $('#json-outcome');
        note.hidden = false;
        note.textContent = h.note || 'Paste this into ' + (h.where || 'your MCP config') + '.';
        $('#mcp-json').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      };
      act.append(b);
      row.append(left, act);
      rowsBox.append(row);
    }
  }

  if (got.config) {
    $('#mcp-json').textContent = got.config;
    /* Rebind copy so it copies what is on screen now, not the constant the
     * prototype captured when the page loaded. */
    const copy = $('#copy-json');
    if (copy) {
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText($('#mcp-json').textContent); } catch {}
        const o = $('#json-outcome');
        o.hidden = false;
        o.textContent = 'Copied. It names this machine\u2019s own binary — nothing is fetched.';
      };
    }
  }
}

window.addEventListener('pywebviewready', loadAssistants);

/* ==================================================== the real meter ====== *
 *
 * `meter(true)` animates a stand-in, because for most of this page's life the
 * engine reported no input level at all. It does now -- `on_level` in
 * audio_listener, about twelve times a second, carrying the peak since the
 * last report and whether the detector called it speech.
 *
 * So the live driver stops animating and starts drawing. The first real
 * report cancels the stand-in for the rest of the session: whichever arrives,
 * the meter is never showing invented numbers next to real ones.
 *
 * The scale is dBFS floored at -60, which is where the segmenter floors it.
 * Mapping it straight to height would put ordinary speech in the top eighth
 * of the bar and everything else flat on the floor, so it is raised to 0.6 --
 * the quiet end gets room to move, which is the end a person is watching when
 * they are checking whether the microphone is live at all.
 */
let realMeter = false;

function realLevel(db, speech) {
  if (!recording) return;
  if (!realMeter) { realMeter = true; clearInterval(level); wave.classList.remove('idle'); }

  const unit = Math.max(0, Math.min(1, (db + 60) / 60));
  logoLevel(speech ? unit : unit * 0.25);   // the mark answers the room
  const height = 6 + Math.pow(unit, 0.6) * 88;

  /* Shift left by one and put the newest at the right, so the bar scrolls with
   * time instead of every capsule jumping at once. */
  for (let i = 0; i < LIVE_BARS - 1; i++) {
    bars[i].style.height = bars[i + 1].style.height;
    bars[i].className = bars[i + 1].className;
  }
  const last = bars[LIVE_BARS - 1];
  last.style.height = height.toFixed(0) + '%';
  /* The four held-back capsules are the gate's tail, and `q` is the grey that
   * says "not being transcribed". A frame the detector did not call speech
   * gets the same grey, so the meter and the gate never disagree. */
  last.className = speech ? '' : 'q';

  $('#gate').textContent = speech
    ? `Capturing · ${sourceLabel()}`
    : `Silence gated · ${Math.round(db)} dB, not transcribed`;
}

/* A session ends, the next one starts from the stand-in again until its first
 * real report. Without this a meter left holding the last session's bars looks
 * like it is still listening. */
const _stopWasCalled = stop;
stop = function () {                                        // eslint-disable-line
  realMeter = false;
  logoLevel(0);
  return _stopWasCalled.apply(this, arguments);
};

/* ================================================= the first run ========= *
 *
 * Two things a new user meets that the prototype never had to: an empty
 * library, and a machine with no notes engine on it yet.
 *
 * The empty library is just an empty state, and it exists because every other
 * screen had one and this did not.
 *
 * The readiness check is the one that matters. Without Ollama the app still
 * records and still saves the transcript -- only the summary is missing. That
 * is a perfectly usable product and a terrible surprise, so it is said before
 * the first recording instead of after it. Checked once, on open; shown only
 * when the answer is bad; never nagged again, because a banner that returns
 * after you have decided to ignore it is a different product than this one.
 */
function libraryEmpty(isEmpty) {
  const box = $('#library-empty');
  if (box) box.hidden = !isEmpty;
}

async function checkReady() {
  const bar = $('#notready');
  if (!bar || !api()) return;
  let ok = false, why = '';
  try {
    const r = await api().engine_check();
    const got = r && r.result;
    /* Core answers (ready, reason). Anything else -- an error, a shape this
     * page does not know -- is treated as "cannot tell", and a banner is not
     * raised on a guess. */
    if (Array.isArray(got)) { ok = !!got[0]; why = got[1] || ''; }
    else ok = true;
  } catch { ok = true; }
  if (ok) { bar.hidden = true; return; }
  $('#notready-why').textContent = why
    || 'No notes engine was found on this machine.';
  bar.hidden = false;
}

$('#notready-fix') && ($('#notready-fix').onclick = () => show('settings'));

window.addEventListener('pywebviewready', checkReady);

/* In a browser there is no engine to check and nothing to warn about. */
if (!window.pywebview) {
  window.addEventListener('DOMContentLoaded', () => libraryEmpty(false));
}

/* ===================================================== first run ========= *
 *
 * The five setup questions, drawn by this window. Every heading, subtitle and
 * blurb below is the wording the tkinter wizard already used -- this is a
 * change of rendering, not of product, so the copy came across verbatim.
 *
 * Nothing here decides anything. The choices go to `setup_wizard.commit()`,
 * which runs the same `plan()` the tkinter wizard runs, so both windows write
 * identical settings from identical answers.
 */
const SU = {
  i: 0,
  opts: null,
  choice: {
    notes_dir: '', language: '', profile: '', custom_model: '',
    /* Overwritten from `options().notes_kind_default` in suStart(); Core
     * decides, because Core is the side that knows what this build can run. */
    notes_kind: 'ollama', ollama_url: '', ollama_model: '',
    user_field: '', user_tone: '', user_context: '',
  },
};

const suBody = () => $('#su-body');

/* A labelled row of single-click choices. Chips size to their own label rather
 * than to a grid column: twelve fixed-width pills wide enough for "Customer
 * Success" made "HR" look like a mistake, and pushed the row that matters off
 * the bottom of the card. Clicking a chosen chip clears it, because "none of
 * these" is a real answer and there is no other way to say it. */
function chipRow(label, name, values, get, set) {
  const row = el('div', 'chiprow');
  row.append(el('div', 'chiprow-h', label));
  const chips = el('div', 'chips');
  chips.setAttribute('role', 'radiogroup');
  chips.setAttribute('aria-label', label);
  for (const v of values || []) {
    const chip = el('button', 'chip', v);
    chip.type = 'button';
    chip.setAttribute('role', 'radio');
    const mark = () => chip.setAttribute('aria-checked', String(get() === v));
    mark();
    chip.onclick = () => {
      set(get() === v ? '' : v);
      $$('.chip', chips).forEach(c => c.setAttribute(
        'aria-checked', String(c.textContent === get())));
    };
    chips.append(chip);
  }
  row.append(chips);
  return row;
}

function suOption(name, value, title, blurb, onPick) {
  const row = el('label', 'su-opt');
  row.setAttribute('role', 'radio');
  const input = el('input');
  input.type = 'radio'; input.name = name; input.value = value;
  const box = el('div');
  box.append(el('b', null, title));
  if (blurb) box.append(el('span', null, blurb));
  row.append(input, box);
  row.addEventListener('click', () => {
    input.checked = true;
    $$('.su-opt', suBody()).forEach(o => o.setAttribute('aria-checked', 'false'));
    row.setAttribute('aria-checked', 'true');
    onPick(value);
  });
  return row;
}

const SU_STEPS = [
  {
    head: 'Everything stays on this machine',
    sub: "A few quick questions and you're recording. Every answer can be "
       + 'changed later in Settings.',
    draw() {
      const box = el('div');
      box.append(el('div', 'meta', 'Your notes will be saved to'));
      const path = el('div', 'su-path', SU.choice.notes_dir);
      box.append(path);
      const row = el('div', 'su-row');
      const pick = el('button', 'btn', 'Choose folder…');
      pick.onclick = async () => {
        const r = await api().choose_folder();
        if (r && r.path) { SU.choice.notes_dir = r.path; path.textContent = r.path; }
      };
      const def = el('button', 'btn', 'Use the default');
      def.onclick = () => {
        SU.choice.notes_dir = SU.opts.notes_dir;
        path.textContent = SU.opts.notes_dir;
      };
      row.append(pick, def);
      box.append(row);
      box.append(el('p', 'note', 'Updating or reinstalling the app never touches '
        + 'this folder. Point it at a synced folder and your notes follow you '
        + 'between machines.'));
      return box;
    },
  },
  {
    head: 'What language are your meetings in?',
    sub: 'Pinning the language is faster and more accurate than detecting it: '
       + 'meeting speech comes in short bursts, which is exactly where detection '
       + 'guesses wrong.',
    draw() {
      const box = el('div');
      const sel = el('select', 'sel');
      for (const l of SU.opts.languages) {
        const o = el('option', null, l.label);
        o.value = l.code;
        if (l.code === (SU.choice.language || SU.opts.language_default)) o.selected = true;
        sel.append(o);
      }
      sel.onchange = () => { SU.choice.language = sel.value; };
      SU.choice.language = SU.choice.language || sel.value;
      box.append(sel);
      const card = el('div', 'su-opt');
      card.style.cursor = 'default';
      const inner = el('div');
      inner.append(el('b', null, 'Meetings that switch languages'));
      inner.append(el('span', null, 'Choose "Auto-detect" instead. Each line is '
        + 'then worked out on its own and tagged with the language it was heard '
        + 'as, so a wrong guess is visible rather than silent.'));
      card.append(inner);
      box.append(card);
      return box;
    },
  },
  {
    head: 'How accurate should transcription be?',
    sub: 'This trades accuracy against how hard your machine has to work. The '
       + 'model downloads once, the first time you record.',
    draw() {
      const box = el('div');
      for (const p of SU.opts.profiles) {
        const row = suOption('su-profile', p.key,
          p.label + '  ·  ' + p.WHISPER_MODEL,
          p.summary + ' · ~' + p.ram_mb + ' MB · ' + p.accuracy,
          v => { SU.choice.profile = v; SU.choice.custom_model = ''; });
        if (p.key === (SU.choice.profile || SU.opts.profile_default)) {
          row.setAttribute('aria-checked', 'true');
          row.querySelector('input').checked = true;
        }
        box.append(row);
      }
      SU.choice.profile = SU.choice.profile || SU.opts.profile_default;
      const own = el('div', 'su-row');
      const shown = el('div', 'su-path', SU.choice.custom_model || '');
      const btn = el('button', 'btn', 'Use my own model folder…');
      btn.onclick = async () => {
        const r = await api().choose_folder();
        if (r && r.path) { SU.choice.custom_model = r.path; shown.textContent = r.path; }
      };
      own.append(btn);
      box.append(own, shown);
      return box;
    },
  },
  {
    head: 'Written summaries (optional)',
    sub: 'Recording and transcription work without this. You always get a full '
       + 'transcript; this decides who writes the summary.',
    draw() {
      const box = el('div');
      const mb = SU.opts.builtin.size_mb;
      /* "Built in" is only offered when this build can actually run it.
       * llama-cpp-python was dropped in 1.2.3, so in every shipped build the
       * engine those weights need is absent -- and offering it anyway wrote
       * SUMMARY_ENGINE = "llamacpp" and produced a transcript with no notes,
       * by following the wizard's own default. Core answers this by trying
       * the import, not by looking for the weights on disk. */
      const picks = [];
      if (SU.opts.builtin_usable) {
        picks.push(['builtin', 'Built in',
                    'one download, about ' + mb + ' MB, nothing else to install']);
      }
      picks.push(
        ['ollama', 'Use Ollama', 'if you already have it, or want to pick your own models'],
        ['skip', 'Not now', 'you still get a full transcript of every meeting'],
      );
      const detail = el('div');

      function drawDetail(kind) {
        detail.replaceChildren();
        if (kind !== 'ollama') return;
        const status = el('p', 'note', 'Looking for Ollama…');
        const row = el('div', 'su-row');
        const again = el('button', 'btn', 'Check again');
        const model = el('select', 'sel');
        row.append(again, model);
        detail.append(status, row);

        const probe = async () => {
          status.textContent = 'Looking for Ollama…';
          const r = await api().ollama_probe(SU.choice.ollama_url || SU.opts.ollama_url);
          model.replaceChildren();
          if (!r || !r.reachable) {
            /* Not an error and not a dead end -- Core knows how to install it
             * on this OS, so say that instead of "not found". */
            status.textContent = r && r.hint
              ? 'Ollama is not running. ' + r.hint
              : 'Ollama is not running on this machine.';
            if (r && r.command) detail.append(el('div', 'su-path', r.command));
            return;
          }
          if (!r.models.length) {
            status.textContent = 'Ollama is running but has no models yet. "'
              + SU.opts.ollama_model + '" will be downloaded.';
            SU.choice.ollama_model = SU.opts.ollama_model;
            const bar = el('div', 'su-bar');
            const fill = el('i');
            bar.append(fill);
            const go = el('button', 'btn primary', 'Download it now');
            go.onclick = () => {
              go.disabled = true;
              api().pull_model(SU.opts.ollama_model,
                               SU.choice.ollama_url || SU.opts.ollama_url);
            };
            SU.pullUI = { fill: fill, status: status, button: go, refresh: probe };
            detail.append(go, bar);
            return;
          }
          status.textContent = 'Ollama is running. Pick the model that writes your notes.';
          for (const m of r.models) {
            const o = el('option', null, m);
            o.value = m;
            if (m === SU.opts.ollama_model) o.selected = true;
            model.append(o);
          }
          SU.choice.ollama_model = model.value;
          model.onchange = () => { SU.choice.ollama_model = model.value; };
        };
        again.onclick = probe;
        probe();
      }

      for (const pick of picks) {
        const row = suOption('su-kind', pick[0], pick[1], pick[2], val => {
          SU.choice.notes_kind = val;
          drawDetail(val);
        });
        if (pick[0] === SU.choice.notes_kind) {
          row.setAttribute('aria-checked', 'true');
          row.querySelector('input').checked = true;
        }
        box.append(row);
      }
      box.append(detail);
      drawDetail(SU.choice.notes_kind);
      return box;
    },
  },
  {
    head: "You're set up",
    sub: 'Press Finish and the window opens. Everything here lives in Settings '
       + 'if you want to change it later.',
    draw() {
      const box = el('div');
      const lang = SU.opts.languages.find(l => l.code === SU.choice.language);
      const prof = SU.opts.profiles.find(p => p.key === SU.choice.profile);
      const kinds = { builtin: 'Built in', ollama: 'Ollama', skip: 'Not now' };
      const rows = [
        ['Notes folder', SU.choice.notes_dir],
        ['Language', lang && lang.label],
        ['Transcription', SU.choice.custom_model || (prof && prof.label)],
        ['Summaries', kinds[SU.choice.notes_kind]],
      ];
      for (const r of rows) {
        if (!r[1]) continue;
        const line = el('div', 'su-row');
        line.append(el('div', 'meta', r[0]), el('div', 'su-path', String(r[1])));
        box.append(line);
      }
      return box;
    },
  },
];

function suDraw() {
  const step = SU_STEPS[SU.i];
  $('#su-step').textContent = 'Step ' + (SU.i + 1) + ' of ' + SU_STEPS.length;
  $('#su-h').textContent = step.head;
  $('#su-sub').textContent = step.sub;
  suBody().replaceChildren(step.draw());
  $('#su-back').disabled = SU.i === 0;
  $('#su-next').textContent = SU.i === SU_STEPS.length - 1 ? 'Finish' : 'Next';
  $('#su-outcome').textContent = '';
}

async function suStart() {
  const need = await api().setup_needed();
  if (!need || !need.needed) return false;
  const opts = await api().setup_options();
  if (!opts || opts.error) return false;       // never trap anyone behind it
  SU.opts = opts;
  SU.choice.notes_dir = opts.notes_dir;
  SU.choice.language = opts.language_default;
  SU.choice.profile = opts.profile_default;
  SU.choice.notes_kind = opts.notes_kind_default || 'ollama';
  SU.choice.user_field = opts.user_field || '';
  SU.choice.user_tone = opts.user_tone || '';
  SU.choice.user_context = opts.user_context || '';
  SU.choice.ollama_url = opts.ollama_url;
  SU.choice.ollama_model = opts.ollama_model;
  $('#setup').hidden = false;
  suDraw();
  return true;
}

function suFinish() {
  $('#setup').hidden = true;
  askWhoFor();                       // the app is visible now; ask here
  /* The answers are live in the process now, so anything the window read
   * before setup ran has to be read again rather than kept. */
  if (LIVE) { live.loadSettings(); live.loadLibrary(); checkReady(); }
}

if ($('#su-back')) {
  $('#su-back').onclick = () => { if (SU.i > 0) { SU.i -= 1; suDraw(); } };
}
if ($('#su-next')) {
  $('#su-next').onclick = async () => {
    if (SU.i < SU_STEPS.length - 1) { SU.i += 1; suDraw(); return; }
    const out = $('#su-outcome');
    out.className = 'setup-outcome';
    out.textContent = 'Saving…';
    const r = await api().setup_commit(SU.choice);
    if (r && r.error) {
      /* Setup is not worth trapping anyone in. Say what failed, then let them
       * through: the app runs on its defaults and Settings reaches all of it. */
      out.className = 'setup-outcome bad';
      out.textContent = r.error + ' — continuing anyway.';
      setTimeout(suFinish, 1800);
      return;
    }
    suFinish();
  };
}


/* Model-pull progress, straight onto the bar the summaries step drew. The
 * numbers come from Ollama through `setup_wizard.pull_model` unchanged -- this
 * only renders them. */
function suPull(p) {
  const ui = SU.pullUI;
  if (!ui) return;
  if (p.finished) {
    ui.button.disabled = false;
    if (p.error) {
      ui.status.textContent = 'The download did not finish. ' + p.error;
      return;
    }
    ui.status.textContent = 'Notes model installed.';
    ui.fill.style.width = '100%';
    ui.refresh();            // re-probe, so the picker now lists it
    return;
  }
  if (p.total) {
    const pct = Math.max(0, Math.min(100, (p.done / p.total) * 100));
    ui.fill.style.width = pct.toFixed(0) + '%';
    ui.status.textContent = (p.status || 'Downloading')
      + ' — ' + Math.round(pct) + '%';
  } else if (p.status) {
    ui.status.textContent = p.status;
  }
}

/* Setup runs before anything else is drawn: the rest of the window reads
 * settings that setup is about to write, so loading first would show answers
 * that are one step out of date. */
window.addEventListener('pywebviewready', async () => {
  /* Setup first when it is needed: the card belongs on a visible app, not
   * underneath a modal. When setup is not needed this asks straight away. */
  const shown = await suStart();
  if (!shown) askWhoFor();
});


/* ============================================ who the notes are for ====== *
 *
 * Asked in the app on first open, not in setup. Setup is plumbing — where to
 * save, what language, which model — and it blocks the window until it is
 * answered. This is not plumbing: it is a question about the person, it has a
 * perfectly good empty answer, and asking it behind a modal before they have
 * seen the app made it feel like part of the installer.
 *
 * So the app opens, and the first screen carries the question. Answer it,
 * wave it away, or ignore it entirely and start recording — all three work,
 * and none of them asks again.
 */
const FR = { field: '', tone: '' };

function firstRunCard(opts) {
  const host = $('#firstrun');
  if (!host) return;
  host.replaceChildren();

  const head = el('div', 'fr-head');
  head.append(el('strong', null, 'Who are these notes for?'));
  head.append(el('span', null,
    'Two clicks and the notes come out in a shape that suits you. '
    + 'Optional — Settings has it either way.'));
  host.append(head);

  host.append(chipRow('Area of work', 'fr-field', opts.user_fields,
                      () => FR.field, v => { FR.field = v; }));
  host.append(chipRow('How should the notes read?', 'fr-tone', opts.user_tones,
                      () => FR.tone, v => { FR.tone = v; }));

  const act = el('div', 'fr-act');
  const save = el('button', 'btn primary', 'Save');
  const skip = el('button', 'btn', 'Not now');
  /* Both answers close it for good. "Not now" is a decision, and re-asking
     somebody who already said no is how a banner becomes nagging. */
  const done = async (changes) => {
    save.disabled = skip.disabled = true;
    try { await api().settings_set(Object.assign({USER_ASKED: true}, changes)); }
    catch (e) { /* never trap them behind it */ }
    host.hidden = true;
    if (LIVE) live.loadSettings();
  };
  save.onclick = () => done({USER_FIELD: FR.field, USER_TONE: FR.tone});
  skip.onclick = () => done({});
  act.append(save, skip);
  host.append(act);

  host.hidden = false;
}

async function askWhoFor() {
  if (!api()) return;
  try {
    const o = await api().setup_options();
    if (!o || o.error) return;
    /* Already answered, already waved away, or already set in Settings. */
    if (o.user_asked || o.user_field || o.user_tone) return;
    FR.field = ''; FR.tone = '';
    firstRunCard(o);
  } catch (e) { /* a missing card is better than a broken screen */ }
}

/* ================================================= provisional text ====== *
 *
 * The words as they are being heard, before the sentence is final.
 *
 * The engine has always sent these; this window used to drop them on the floor
 * and show "Transcribing…" instead. That is why the transcript felt slow after
 * the rewrite even though it was not: the pipeline waits 800ms of silence to
 * decide you have stopped, then decodes, so a finished line lands ~1.8s after
 * you stop talking -- measured 1770ms median, against 1746ms on the v1.1.1
 * baseline, with the decode itself *faster* than it used to be.
 *
 * Nothing about that is fixable by making the model quicker. What fixes it is
 * showing the guess while the sentence is still being spoken, so the wait is
 * filled rather than empty. Restoring this changes no engine timing at all.
 *
 * Grey and italic, and replaced wholesale by the real line when it arrives --
 * `line()` already removes `.ln.interim` before appending. It must never be
 * mistaken for the transcript: it is a guess, and it says so by looking like
 * one.
 */
function interim(text, who) {
  if (!text) return;
  $$('.ln.interim', lines).forEach(n => n.remove());
  $$('.ln.hint', lines).forEach(n => n.remove());
  const row = el('div', 'ln interim');
  row.append(el('span', 'ts', ''));
  const p = el('p');
  if (who) p.append(el('span', 'who', who), document.createTextNode(' — ' + text));
  else p.textContent = text;
  row.append(p);
  lines.append(row);
  row.scrollIntoView({ block: 'nearest' });
  $('#gate').textContent = `Hearing · ${sourceLabel()}`;
}

/* =============================================== the logo, while listening = *
 *
 * `--lvl` on the root, 0..1, from the same reports the meter draws. The mark
 * in the chrome bar scales and glows off it, so the thing that moves is the
 * room's actual loudness and not a timer. Set back to 0 the moment a session
 * ends: an app that keeps pulsing after you pressed Stop is telling you it is
 * still listening, which would be a lie.
 */
function logoLevel(unit) {
  document.documentElement.style.setProperty('--lvl', (unit || 0).toFixed(3));
}

/* ==================================================== library: filtering === *
 *
 * A range and a full-notes toggle. Both are view state and neither is saved:
 * a filter that survives a restart is a filter somebody forgets they set and
 * then reports as "my meetings are gone".
 */
const RANGES = [
  ['all', 'All'],
  ['7', 'Last 7 days'],
  ['30', 'Last 30 days'],
  ['today', 'Today'],
];
let libRange = 'all';
let libFull = false;
let libRows = [];

function withinRange(when) {
  if (libRange === 'all') return true;
  const now = new Date();
  const d = new Date(when);
  if (libRange === 'today') return d.toDateString() === now.toDateString();
  return (now - d) <= Number(libRange) * 864e5;
}

function drawLibrary() {
  libraryRows.replaceChildren();
  const shown = libRows.filter(r => withinRange(r.at));
  /* rowFor prepends, so the newest has to go in last. */
  for (const m of [...shown].reverse()) rowFor(m, libraryRows);
  if (libFull) for (const m of shown) showFull(m);

  const box = $('#library-filter');
  if (box) box.hidden = !libRows.length;
  const count = $('#library-count');
  if (count) {
    count.textContent = shown.length === libRows.length
      ? '' : `${shown.length} of ${libRows.length}`;
  }
  const empty = $('#library-empty');
  if (empty && libRows.length) {
    empty.hidden = shown.length > 0;
    if (!shown.length) {
      empty.replaceChildren(
        el('strong', null, 'Nothing in that range.'),
        document.createTextNode('You have ' + libRows.length
          + ' meeting' + (libRows.length === 1 ? '' : 's') + ' in all.'));
    }
  }
}

/* The notes as the file has them. Asked for once per meeting and kept, so
 * toggling the switch twice does not re-read the disk twice. */
async function showFull(m) {
  if (!m._row) return;
  if (m._row.querySelector('.full')) return;
  const box = el('div', 'full', 'Reading…');
  m._row.append(box);
  try {
    const got = await api().meeting(m.base);
    const text = (got && (got.notes || got.summary || '')).trim();
    if (text) { box.textContent = text; box.classList.remove('empty'); }
    else {
      box.classList.add('empty');
      box.textContent = 'No notes were written for this meeting — the '
        + 'transcript is still in the folder.';
    }
  } catch (e) {
    box.classList.add('empty');
    box.textContent = 'Could not read the notes: ' + e;
  }
}

function wireLibraryFilter() {
  const host = $('#library-range');
  if (!host) return;
  host.replaceChildren();
  for (const [value, label] of RANGES) {
    const chip = el('button', 'chip', label);
    chip.type = 'button';
    chip.setAttribute('role', 'radio');
    chip.setAttribute('aria-checked', String(libRange === value));
    chip.onclick = () => {
      libRange = value;
      $$('.chip', host).forEach(c => c.setAttribute(
        'aria-checked', String(c.textContent === label)));
      drawLibrary();
    };
    host.append(chip);
  }
  const toggle = $('#library-expand');
  if (toggle) {
    toggle.checked = libFull;
    toggle.onchange = () => { libFull = toggle.checked; drawLibrary(); };
  }
}
