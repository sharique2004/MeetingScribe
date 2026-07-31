// Landing page for signed-out visitors. Terminal-first: the page runs the
// same install command it asks you to run, and the app it installs opens
// right below it. Phones hide the install paths and lead to the synced
// viewer instead. The direction contract lives in mobile/index.html.

import React from "react";

const DMG_URL =
  "https://github.com/sharique2004/MeetingScribe/releases/latest/download/MeetingScribe.dmg";

const INSTALL_CMD =
  "curl -fsSL https://meetingscribe.shariquekhatri.com/install.sh | sh";

const GITHUB_URL = "https://github.com/sharique2004/MeetingScribe";

const IS_MAC = /Macintosh|Mac OS X/.test(navigator.userAgent);
const REDUCED =
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ------------------------------ motion hooks ------------------------------ */

// Types `text` one character at a time once `active` turns true.
function useTyped(text, active, { speed = 16, delay = 500 } = {}) {
  const [n, setN] = React.useState(() => (REDUCED ? text.length : 0));
  React.useEffect(() => {
    if (REDUCED || !active || n >= text.length) return;
    const t = setTimeout(() => setN((v) => v + 1), n === 0 ? delay : speed);
    return () => clearTimeout(t);
  }, [active, n, text, speed, delay]);
  return { shown: text.slice(0, n), done: n >= text.length };
}

// Flips true once, the first time the element scrolls into view.
function useReveal(threshold = 0.2) {
  const ref = React.useRef(null);
  const [inView, setInView] = React.useState(REDUCED);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || inView) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) {
          setInView(true);
          io.disconnect();
        }
      },
      { threshold }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [inView, threshold]);
  return [ref, inView];
}

// Words that land one by one when their container is revealed.
function Words({ text, on, step = 60, from = 0 }) {
  return text.split(" ").map((w, i) => (
    <span
      key={i}
      className={"lp-w" + (on ? " on" : "")}
      style={{ transitionDelay: `${from + i * step}ms` }}
    >
      {w}{" "}
    </span>
  ));
}

/* --------------------------------- assets --------------------------------- */

function MicIcon({ size = 20 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="#5eead4" strokeWidth="2" strokeLinecap="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  );
}

function DownloadGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12" /><path d="M7 11l5 5 5-5" /><path d="M4 20h16" />
    </svg>
  );
}

/* ------------------------------ demo content ------------------------------ */
// Demo meeting fixture (same fictional meeting as ui-demos/data.js).

const SPEAKER_COLOR = { You: "#5b8cff", "Priya Nair": "#c07af6", "Marcus Bell": "#2fbf87" };

const DEMO_TURNS = [
  { t: "00:03", s: "You",
    text: "Okay, we're recording. Let's keep this to the three things that actually block Q3: pricing, the mobile beta, and the data retention work." },
  { t: "00:19", s: "Priya Nair",
    text: "After last week's customer calls, the usage based tier is the one people keep asking for. Everyone above roughly forty seats wants to pay for what they actually use." },
  { t: "01:02", s: "Marcus Bell",
    text: "Metering itself isn't the hard part. The hard part is making the numbers trustworthy enough to put on an invoice. I'd want two weeks just for that." },
  { t: "01:28", s: "You", live: true,
    text: "Two weeks is fine if it means we don't refund half the first month. Trustworthy metering is the gate for the pricing launch." },
];

const DEMO_ACTIONS = [
  { who: "Priya Nair", what: "Pricing page copy for the usage tier", due: "Thu" },
  { who: "Marcus Bell", what: "Metering reconciliation design doc", due: "Thu" },
];

/* -------------------------------- terminal -------------------------------- */

// Mirrors what tools/install.sh actually prints.
const TERM_OUTPUT = [
  { text: "downloading MeetingScribe.app (310 MB)", cls: "" },
  { text: "installed to /Applications", cls: "ok" },
  { text: "cleared quarantine, no Gatekeeper prompts", cls: "ok" },
  { text: "opening MeetingScribe", cls: "ok" },
];

function Terminal() {
  const { shown, done } = useTyped(INSTALL_CMD, true, { speed: 14, delay: 700 });
  const [lines, setLines] = React.useState(REDUCED ? TERM_OUTPUT.length : 0);
  React.useEffect(() => {
    if (!done || lines >= TERM_OUTPUT.length) return;
    const t = setTimeout(() => setLines((v) => v + 1), lines === 0 ? 500 : 330);
    return () => clearTimeout(t);
  }, [done, lines]);

  return (
    <div className="lp-beam">
      <div className="lp-term" role="img"
           aria-label="Terminal running the MeetingScribe install command">
        <div className="lp-termbar">
          <span className="lp-tl" /><span className="lp-tl" /><span className="lp-tl" />
          <span className="lp-termtitle">meetingscribe · zsh</span>
          <CopyButton />
        </div>
        <div className="lp-termbody" aria-hidden="true">
          <div className="lp-cmdline">
            <span className="lp-prompt">$</span> {shown}
            {!done && <span className="lp-caret" />}
          </div>
          {TERM_OUTPUT.slice(0, lines).map((l, i) => (
            <div className={"lp-out " + l.cls} key={i}>
              {l.cls === "ok" && <span className="lp-check">✓</span>} {l.text}
            </div>
          ))}
          {lines >= TERM_OUTPUT.length && (
            <div className="lp-cmdline"><span className="lp-prompt">$</span> <span className="lp-caret idle" /></div>
          )}
        </div>
      </div>
    </div>
  );
}

function CopyButton({ compact = false }) {
  const [copied, setCopied] = React.useState(false);
  const copy = () => {
    navigator.clipboard.writeText(INSTALL_CMD).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    });
  };
  return (
    <button className={"lp-copy" + (compact ? " compact" : "")} onClick={copy}
            aria-label="Copy the install command">
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

/* ------------------------------- app window ------------------------------- */

function AppWindow() {
  const [ref, on] = useReveal(0.35);
  return (
    <div className="lp-window" ref={ref} role="img"
         aria-label="The MeetingScribe app showing a transcribed demo meeting with summary and action items">
      <div className="lp-titlebar">
        <span className="lp-tl" /><span className="lp-tl" /><span className="lp-tl" />
        <span className="lp-wintitle">Q3 Roadmap Sync · 24:18</span>
        <span className="lp-wintag">demo meeting</span>
      </div>
      <div className="lp-winbody">
        <div className="lp-transcript">
          {DEMO_TURNS.map((turn) => (
            <div className={"lp-turn" + (turn.live ? " live" : "")} key={turn.t}>
              <span className="lp-ts">{turn.t}</span>
              <div>
                <span className="lp-who" style={{ color: SPEAKER_COLOR[turn.s] }}>{turn.s}</span>
                <p>{turn.live ? <Words text={turn.text} on={on} step={55} from={400} /> : turn.text}</p>
              </div>
            </div>
          ))}
        </div>
        <aside className="lp-rail">
          <div className="lp-railhead">Summary</div>
          <p className="lp-tldr">
            The quarter narrows to one bet: hardening the usage event pipeline
            for billing, mobile sync, and enterprise purge proof. Marcus is the
            critical path.
          </p>
          <div className="lp-railhead">Decisions</div>
          <ul className="lp-decisions">
            <li>Usage based pricing waits for reconciled metering</li>
            <li>Mobile beta ships Friday with a known issues note</li>
          </ul>
          <div className="lp-railhead">Action items</div>
          {DEMO_ACTIONS.map((a) => (
            <div className="lp-action" key={a.what}>
              <span className="lp-owner" style={{ background: SPEAKER_COLOR[a.who] }} />
              <span className="lp-actiontext">{a.what}</span>
              <span className="lp-due">{a.due}</span>
            </div>
          ))}
        </aside>
      </div>
    </div>
  );
}

/* ------------------------------- proof rows ------------------------------- */

function ProofRow({ head, body, children, flip = false }) {
  const [ref, on] = useReveal(0.3);
  return (
    <div className={"lp-proofrow" + (flip ? " flip" : "") + (on ? " in" : "")} ref={ref}>
      <div className="lp-prooftext">
        <h3>{head}</h3>
        <p>{body}</p>
      </div>
      <div className="lp-proofart">{children}</div>
    </div>
  );
}

function CaptionArt() {
  const [ref, on] = useReveal(0.5);
  return (
    <div className="lp-art lp-captionart" ref={ref}>
      <div className="lp-artlabel">live captions · demo</div>
      <p className="lp-caption">
        <Words on={on} step={70}
          text="We can hold the launch until metering is signed off, that feels safer." />
      </p>
      <p className="lp-caption dim">
        <Words on={on} from={1300} step={70}
          text="Agreed. I'll own the reconciliation piece and report Thursday." />
      </p>
    </div>
  );
}

function SpeakerArt() {
  const [ref, on] = useReveal(0.5);
  const rows = [
    { name: "You", share: 41 },
    { name: "Priya Nair", share: 34 },
    { name: "Marcus Bell", share: 25 },
  ];
  return (
    <div className={"lp-art" + (on ? " in" : "")} ref={ref}>
      <div className="lp-artlabel">talk time · demo</div>
      {rows.map((r) => (
        <div className="lp-speakrow" key={r.name}>
          <span className="lp-who" style={{ color: SPEAKER_COLOR[r.name] }}>{r.name}</span>
          <span className="lp-sharebar">
            <span className="lp-sharefill"
                  style={{ width: `${r.share}%`, background: SPEAKER_COLOR[r.name] }} />
          </span>
          <span className="lp-sharepct">{r.share}%</span>
        </div>
      ))}
    </div>
  );
}

function SpeedArt() {
  const [ref, on] = useReveal(0.5);
  return (
    <div className={"lp-art" + (on ? " in" : "")} ref={ref}>
      <div className="lp-artlabel">on the neural engine</div>
      <div className="lp-speedrow">
        <span className="lp-speedlabel">meeting</span>
        <span className="lp-speedbar"><span className="lp-speedfill slow" style={{ width: "100%" }} /></span>
        <span className="lp-speedtime">45:00</span>
      </div>
      <div className="lp-speedrow">
        <span className="lp-speedlabel">transcript</span>
        <span className="lp-speedbar"><span className="lp-speedfill fast" style={{ width: "3.4%" }} /></span>
        <span className="lp-speedtime aqua">~1:30</span>
      </div>
    </div>
  );
}

function PhoneArt() {
  return (
    <div className="lp-art lp-phoneart">
      <div className="lp-phone">
        <div className="lp-phonehead">Your meetings <span className="lp-wintag">demo</span></div>
        <div className="lp-phonerow">
          <b>Q3 Roadmap Sync</b><span>Today · 24:18</span>
        </div>
        <div className="lp-phonerow">
          <b>Design review</b><span>Tue · 41:02</span>
        </div>
      </div>
    </div>
  );
}

/* --------------------------------- page ---------------------------------- */

export default function Landing({ onOpenApp, phone = false }) {
  const showDownload = !phone;
  const [appRef, appOn] = useReveal(0.15);

  return (
    <div className="landing">
      <nav className="lp-nav">
        <div className="brand"><MicIcon size={20} /><span>MeetingScribe</span></div>
        <div className="lp-navlinks">
          <a className="lp-navlink" href={GITHUB_URL} target="_blank" rel="noreferrer">GitHub</a>
          <button className="lp-link" onClick={onOpenApp}>View your meetings →</button>
        </div>
      </nav>

      <header className="lp-hero">
        <h1 className="lp-h1">
          Transcribed in seconds.<br />Never leaves your Mac.
        </h1>
        <p className="lp-sub">
          MeetingScribe records any call or conversation, captions it live,
          labels every speaker, and writes the summary with action items. A 45
          minute meeting lands in about 90 seconds on the Apple Neural Engine.
        </p>

        {showDownload ? (
          <>
            <Terminal />
            <div className="lp-heroactions">
              <a className="lp-btn ghost" href={DMG_URL} download>
                <DownloadGlyph /> Download the DMG
              </a>
              <span className="lp-req">
                free · open source, MIT · Apple Silicon · macOS 26 Tahoe · 365 MB DMG
              </span>
            </div>
            {!IS_MAC && (
              <p className="lp-notmac">
                You're not on a Mac right now, but you can still sign in and
                read the meetings you synced.
              </p>
            )}
          </>
        ) : (
          <div className="lp-heroactions phone">
            <button className="lp-btn primary" onClick={onOpenApp}>
              View your meetings <span className="lp-arrow">→</span>
            </button>
            <span className="lp-req">
              You record on your Mac. The meetings you sync are readable here.
            </span>
          </div>
        )}
      </header>

      <section className={"lp-app" + (appOn ? " in" : "")} ref={appRef}>
        <h2 className="lp-h2">Then it opens.</h2>
        <AppWindow />
        <p className="lp-quiet">
          Everything in this window happened on one Mac. The audio never left
          it. Summaries use your own Claude account with transcript text only,
          or stay fully offline with Apple Intelligence.
        </p>
      </section>

      <section className="lp-proof">
        <ProofRow
          head="Live captions while people talk"
          body="Words land on screen as they are spoken, transcribed by the Neural
                Engine while the meeting is still going. No bot joins the call,
                so nobody gets a recording notice from a stranger.">
          <CaptionArt />
        </ProofRow>
        <ProofRow flip
          head="Every voice gets a name"
          body="Zoom, Meet, Teams, or the room. Speakers are told apart
                automatically, and renaming one sticks across the whole
                transcript.">
          <SpeakerArt />
        </ProofRow>
        <ProofRow
          head="A 45 minute call, transcribed in about 90 seconds"
          body="Recording stops and the full speaker labelled transcript is
                moments away. One click then writes the TL;DR, decisions, and
                action items with owners.">
          <SpeedArt />
        </ProofRow>
        <ProofRow flip
          head="Read it on your phone"
          body="Sync is opt in and per meeting, and it moves text only. This
                site is that viewer: sign in and your synced meetings are
                waiting.">
          <PhoneArt />
        </ProofRow>
        <p className="lp-also">
          also in the box · a nudge when a call starts unrecorded · one click
          transcript tidy up · follow up email drafts
        </p>
      </section>

      {showDownload ? (
        <section className="lp-install">
          <h2 className="lp-h2">Install it</h2>
          <p className="lp-installlead">
            One command, no security prompts. It puts MeetingScribe.app in
            /Applications and opens it.
          </p>
          <div className="lp-beam sm">
            <div className="lp-cmd">
              <code>{INSTALL_CMD}</code>
              <CopyButton compact />
            </div>
          </div>
          <h3 className="lp-installsub">Prefer the disk image?</h3>
          <ol>
            <li>Open the DMG and drag <b>MeetingScribe</b> into <b>Applications</b>.</li>
            <li>First launch shows <b>"Apple could not verify…"</b> with no Open
              button. It's a free indie app without Apple's paid signing.
              Click <b>Done</b>.</li>
            <li>Open <b>System Settings → Privacy &amp; Security</b>, scroll to
              <b> Security</b>, click <b>Open Anyway</b>, and authenticate.</li>
            <li>Launch <b>MeetingScribe</b> again and confirm. After that it
              opens normally.</li>
          </ol>
          <p className="lp-installnote">
            First run downloads the speech models and walks you through setup:
            Claude sign in for summaries, and the optional{" "}
            <a href="https://existential.audio/blackhole/" target="_blank" rel="noreferrer">BlackHole</a>{" "}
            driver for hearing the other side of calls without speakerphone.
          </p>
          <a className="lp-btn primary" href={DMG_URL} download>
            <DownloadGlyph /> Download the DMG
          </a>
        </section>
      ) : (
        <section className="lp-install">
          <h2 className="lp-h2">Your meetings, on your phone</h2>
          <p className="lp-installlead">
            You record and summarize on your Mac. The meetings you choose to
            sync show up here to read anywhere, with the same account as the
            Mac app.
          </p>
          <button className="lp-btn primary" onClick={onOpenApp}>
            View your meetings <span className="lp-arrow">→</span>
          </button>
        </section>
      )}

      <footer className="lp-foot">
        <div className="brand"><MicIcon size={16} /><span>MeetingScribe</span></div>
        <div className="lp-footlinks">
          <a href={GITHUB_URL} target="_blank" rel="noreferrer">GitHub</a>
          <a href="https://shariquekhatri.com" target="_blank" rel="noreferrer">Made by Sharique Khatri</a>
          <button className="lp-link" onClick={onOpenApp}>View your meetings</button>
        </div>
      </footer>
    </div>
  );
}
