// Landing page for signed-out visitors. Product-first: six real screenshots
// of the app carry the page; text is captions. Desktop leads with the
// install command; phones say it is a Mac app and offer the synced viewer.
// The direction contract lives in mobile/index.html.

import React from "react";

// Canonical download link: our own domain. It currently redirects to the
// GitHub release asset (mobile/vercel.json); when the file moves to different
// hosting, only that redirect changes and every published link keeps working.
const DMG_URL = "https://meetingscribe.shariquekhatri.com/MeetingScribe.dmg";
const SITE_URL = "https://meetingscribe.shariquekhatri.com";
const INSTALL_CMD = `curl -fsSL ${SITE_URL}/install.sh | sh`;
const GITHUB_URL = "https://github.com/sharique2004/MeetingScribe";

const REDUCED =
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ------------------------------ motion hook ------------------------------- */

// Flips true once, the first time the element scrolls into view. The flag
// only ADDS a rise animation: a section is visible whether or not the
// observer ever fires (hidden tabs, old engines), so no screenshot can be
// missing from the page.
function useReveal(threshold = 0.15) {
  const ref = React.useRef(null);
  const [inView, setInView] = React.useState(REDUCED);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || inView) return;
    if (typeof IntersectionObserver !== "function") { setInView(true); return; }
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

/* --------------------------------- assets --------------------------------- */

function MicIcon({ size = 20 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true"
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
         aria-hidden="true" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12" /><path d="M7 11l5 5 5-5" /><path d="M4 20h16" />
    </svg>
  );
}

/* ------------------------------ screenshots ------------------------------- */

const SHOTS = {
  notes: {
    alt: "MeetingScribe's Notes tab for a meeting called Quarterly planning call: speaker chips for You, Priya Nair and Marcus Bell, a summary, decisions, open questions, and an audio player.",
  },
  home: {
    alt: "The Today screen: a sidebar listing four meetings, the greeting Up late, Sharique, and the prompt In a meeting right now? Start recording.",
  },
  record: {
    alt: "The Set up this meeting sheet: a title field named after your calendar, Online call or In person, language set to Auto-detect, and how many voices set to Work it out.",
  },
  transcript: {
    alt: "The Transcript tab: turns labelled You, Priya Nair and Marcus Bell with timestamps, plus Copy and Find in transcript.",
  },
  ask: {
    alt: "The Ask panel answering What did we decide? with a short answer and timestamp citations 0:29, 0:41 and 0:03.",
  },
  settings: {
    alt: "Settings, Who writes your summaries and answers: Apple Intelligence, Claude marked recommended, Codex, Gemini, Copilot marked unavailable, and a toggle to summarise every meeting automatically.",
  },
};

function Shot({ name, hero = false }) {
  return (
    <figure className={"lp-shot" + (hero ? " hero" : "")}>
      <img
        src={`/shots/${name}.webp`}
        srcSet={`/shots/${name}.webp 1x, /shots/${name}@2x.webp 2x`}
        width="1280"
        height="800"
        alt={SHOTS[name].alt}
        loading="eager"
        fetchpriority={hero ? "high" : "auto"}
        decoding={hero ? "sync" : "async"}
      />
    </figure>
  );
}

// The story, in the app's own order.
const SECTIONS = [
  { id: "home", shot: "home", num: "01",
    heading: "Start from Today.",
    line: "This week's meetings on the left; one click to record the next one." },
  { id: "record", shot: "record", num: "02",
    heading: "Set up the meeting.",
    line: "Left blank, the title comes from your calendar, read on your Mac." },
  { id: "transcript", shot: "transcript", num: "03",
    heading: "Who said what, when.",
    line: "Speakers are told apart on-device; a 45-minute meeting takes about a minute." },
  { id: "ask", shot: "ask", num: "04",
    heading: "Ask the meeting.",
    line: "Answers cite the moments they came from; click one to replay it." },
  { id: "engine", shot: "settings", num: "05",
    heading: "Choose who writes the notes.",
    line: "Apple Intelligence stays offline; Claude, Codex, Gemini or Copilot use your own account and see text only." },
];

function Section({ id, shot, num, heading, line }) {
  const [ref, inView] = useReveal(0.15);
  return (
    <section id={id} ref={ref} className={"lp-section" + (inView ? " in" : "")}>
      <div className="lp-caption-row">
        <div>
          <span className="lp-num">{num}</span>
          <h2 className="lp-h2">{heading}</h2>
        </div>
        <p className="lp-line">{line}</p>
      </div>
      <Shot name={shot} />
    </section>
  );
}

function Divider() {
  return <hr className="lp-hr" aria-hidden="true" />;
}

/* ------------------------------- controls --------------------------------- */

// Copies `text`; shows "Copied" for a moment. `onFail` runs if the clipboard
// promise rejects so the caller can reveal the text another way.
function CopyButton({ text, label = "Copy", ariaLabel, className = "lp-copy", onFail }) {
  const [copied, setCopied] = React.useState(false);
  const copy = () => {
    const p = navigator.clipboard && navigator.clipboard.writeText
      ? navigator.clipboard.writeText(text)
      : Promise.reject(new Error("no clipboard"));
    p.then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    }).catch(() => { if (onFail) onFail(); });
  };
  return (
    <>
      <button
        type="button"
        className={className}
        onClick={copy}
        aria-label={copied ? "Copied" : ariaLabel}
      >
        {copied ? "Copied" : label}
      </button>
      <span className="lp-sr" role="status" aria-live="polite">{copied ? "Copied" : ""}</span>
    </>
  );
}

function CommandRow() {
  return (
    <div className="lp-beam">
      <div className="lp-cmd">
        <span className="lp-prompt" aria-hidden="true">$</span>
        <code>{INSTALL_CMD}</code>
        <CopyButton text={INSTALL_CMD} ariaLabel="Copy the install command" />
      </div>
    </div>
  );
}

function DownloadRow() {
  return (
    <div className="lp-dlrow">
      <a className="lp-btn ghost" href={DMG_URL} download>
        <DownloadGlyph /> Download the DMG <span className="lp-size">· 225&nbsp;MB</span>
      </a>
      <span className="lp-req">free · open&nbsp;source, GPLv3 · Apple&nbsp;Silicon · macOS&nbsp;26</span>
    </div>
  );
}

// Phone: the app is for the Mac; give people the link to take there.
function MacCard({ onOpenApp }) {
  const [showUrl, setShowUrl] = React.useState(false);
  return (
    <section className="lp-maccard">
      <h2 className="lp-cardtitle">It's a Mac app.</h2>
      <CopyButton
        text={SITE_URL}
        label="Copy the link"
        ariaLabel="Copy the MeetingScribe link"
        className="lp-btn primary full"
        onFail={() => setShowUrl(true)}
      />
      {showUrl && <code className="lp-url">{SITE_URL}</code>}
      <p className="lp-req">
        meetingscribe.shariquekhatri.com · free · open&nbsp;source, GPLv3 · Apple&nbsp;Silicon · macOS&nbsp;26
      </p>
      <button type="button" className="lp-link" onClick={onOpenApp}>
        Open your synced meetings →
      </button>
      <p className="lp-helper">For people who already record on their Mac.</p>
    </section>
  );
}

/* --------------------------------- page ----------------------------------- */

export default function Landing({ phone = false, onOpenApp }) {
  return (
    <div className={"landing" + (phone ? " phone" : "")}>
      <nav className="lp-nav" aria-label="Site">
        <div className="brand"><MicIcon size={20} /><span>MeetingScribe</span></div>
        {!phone && (
          <div className="lp-navlinks">
            <a className="lp-navlink" href={GITHUB_URL} target="_blank" rel="noreferrer">GitHub</a>
            <button type="button" className="lp-link" onClick={onOpenApp}>View your meetings →</button>
          </div>
        )}
      </nav>

      <header className="lp-hero">
        <h1 className="lp-h1">Every meeting, transcribed on your Mac.</h1>
        <p className="lp-sub">
          It records the call, labels who said what, writes the notes and
          answers your questions. Audio never leaves your Mac.
        </p>
        {phone ? (
          <MacCard onOpenApp={onOpenApp} />
        ) : (
          <div className="lp-heroactions">
            <CommandRow />
            <DownloadRow />
          </div>
        )}
        <Shot name="notes" hero />
      </header>

      <div className="lp-story">
        {SECTIONS.map((s) => (
          <React.Fragment key={s.id}>
            <Divider />
            <Section {...s} />
          </React.Fragment>
        ))}
      </div>

      {phone ? (
        <MacCard onOpenApp={onOpenApp} />
      ) : (
        <section className="lp-install" id="install">
          <h2 className="lp-h2">Install it.</h2>
          <p className="lp-line">One command puts MeetingScribe.app in /Applications and opens it.</p>
          <CommandRow />
          <DownloadRow />
        </section>
      )}

      <footer className="lp-foot">
        <div className="brand"><span>MeetingScribe</span></div>
        <div className="lp-footlinks">
          <a href={GITHUB_URL} target="_blank" rel="noreferrer">GitHub</a>
          <a href="https://shariquekhatri.com" target="_blank" rel="noreferrer">Made by Sharique Khatri</a>
          <button type="button" className="lp-footbtn" onClick={onOpenApp}>
            {phone ? "Open your synced meetings" : "View your meetings"}
          </button>
        </div>
      </footer>
    </div>
  );
}
