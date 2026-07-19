// Marketing / info landing page — shown to every signed-out visitor.
//
// On desktop the primary action is downloading the Mac app (.dmg on InsForge
// storage). On a phone you can't run the Mac app, so the download and install
// steps are hidden and the primary action becomes "View your meetings →",
// which opens the sign-in page (same arrow the desktop nav uses).

import React from "react";

const DMG_URL =
  "https://5uh76ypz.us-east.insforge.app/api/storage/buckets/downloads/objects/MeetingScribe.dmg";

const IS_MAC = /Macintosh|Mac OS X/.test(navigator.userAgent);

function MicIcon({ size = 24 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="#5fb8c9" strokeWidth="2" strokeLinecap="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  );
}

const FEATURES = [
  { icon: "◉", title: "Live captions as you talk",
    body: "See every word appear in real time while you record — on the Neural Engine, on-device. No cloud round-trip." },
  { icon: "❝", title: "Who said what",
    body: "Speaker-labelled transcripts that tell people apart automatically, from Zoom, Meet, Teams, or in-person." },
  { icon: "✦", title: "Summaries by your own Claude",
    body: "One click turns a meeting into a TL;DR, decisions, action items with owners, and a ready-to-send follow-up email — written by your Claude account." },
  { icon: "◔", title: "Never forget to record",
    body: "A gentle nudge when a calendar event starts or when it notices you've joined a call — one tap to record." },
  { icon: "◑", title: "Tidy transcripts",
    body: "One click cleans up echo duplicates and merges split-up speakers — validated on-device, so no words are ever invented." },
  { icon: "▢", title: "Read it on your phone",
    body: "Choose a meeting to sync and read its transcript and summary on your phone. Text only — audio never leaves the Mac." },
];

export default function Landing({ onOpenApp, phone = false }) {
  const showDownload = !phone;
  return (
    <div className="landing">
      <nav className="lp-nav">
        <div className="brand"><MicIcon size={20} /><span>MeetingScribe</span></div>
        <button className="lp-link" onClick={onOpenApp}>View your meetings →</button>
      </nav>

      <header className="lp-hero">
        <div className="lp-pill"><span className="lp-dot" /> 100% local · your audio never leaves your Mac</div>
        <h1>Every meeting, transcribed<br />and summarized — on your Mac.</h1>
        <p className="lp-sub">
          Record any call or conversation, watch live captions as people speak, get a
          speaker-labelled transcript, and turn it into an AI summary with action items —
          all running on your own machine. No accounts required, no subscription, nothing uploaded.
        </p>
        <div className="lp-cta">
          {showDownload ? (
            <a className="lp-btn primary" href={DMG_URL} download>
              <DownloadGlyph /> Download for Mac
            </a>
          ) : (
            <button className="lp-btn primary" onClick={onOpenApp}>
              View your meetings <span className="lp-arrow">→</span>
            </button>
          )}
          <a className="lp-btn ghost" href="#how">How it works</a>
        </div>
        <div className="lp-meta">
          {showDownload
            ? <>Free · Apple Silicon Mac · macOS 26 recommended (older works with Whisper){!IS_MAC && " · you're not on a Mac, but you can still view synced meetings"}</>
            : <>Sign in with your MeetingScribe account to read the transcripts and summaries you synced from your Mac — audio stays on the Mac.</>}
        </div>
      </header>

      <section className="lp-shot">
        <div className="lp-window">
          <div className="lp-titlebar"><span /><span /><span /></div>
          <div className="lp-shotbody">
            <div className="lp-live">
              <div className="lp-livehead"><span className="lp-dot rec" /> LIVE CAPTIONS · on-device</div>
              <div className="lp-row"><b className="you">You</b><span>Thanks for joining — I want to lock the launch date today.</span></div>
              <div className="lp-row"><b>Priya</b><span>Let's move it to Friday the twelfth, the build is stable.</span></div>
              <div className="lp-row"><b className="you">You</b><span>Agreed. Can you update the release notes by Wednesday?</span></div>
            </div>
            <div className="lp-summary">
              <div className="lp-sumhead">✦ Summary</div>
              <p>Launch moved to Fri the 12th; Priya owns release notes.</p>
              <div className="lp-tag">Action · Priya → release notes · Wed</div>
              <div className="lp-tag">Decision · Launch → Friday the 12th</div>
            </div>
          </div>
        </div>
      </section>

      <section className="lp-features" id="how">
        {FEATURES.map((f) => (
          <div className="lp-card" key={f.title}>
            <div className="lp-ic">{f.icon}</div>
            <h3>{f.title}</h3>
            <p>{f.body}</p>
          </div>
        ))}
      </section>

      <section className="lp-privacy">
        <h2>Private by design</h2>
        <p>
          Recording, transcription, speaker separation and live captions all happen on your
          Mac — your meeting audio is never uploaded, ever. Summaries use <b>your own Claude
          account</b> (only the transcript text is sent, never audio), or you can keep them
          fully offline with Apple Intelligence. Syncing a meeting to your phone is opt-in and
          per-meeting, and sends transcript text only.
        </p>
      </section>

      {showDownload ? (
        <section className="lp-install">
          <h2>Installing</h2>
          <ol>
            <li><b>Download</b> the disk image and drag <b>MeetingScribe</b> into Applications.</li>
            <li><b>First open:</b> right-click the app → <b>Open</b> (it's a free indie app, not
              signed through Apple's paid program, so macOS asks once).</li>
            <li><b>First launch</b> sets itself up — it installs its local engine on your Mac
              (a few minutes, one time). After that it opens instantly.</li>
            <li>For meeting summaries, sign in to Claude once in Terminal
              (<code>claude</code>); for capturing the other side of calls, install the free
              <code> BlackHole</code> audio driver. The app guides you.</li>
          </ol>
          <a className="lp-btn primary" href={DMG_URL} download><DownloadGlyph /> Download for Mac</a>
        </section>
      ) : (
        <section className="lp-install">
          <h2>Your meetings, on your phone</h2>
          <p>
            You record and summarize on your Mac; the meetings you choose to sync show up
            here to read anywhere. Sign in with the same account as the Mac app to open them.
          </p>
          <button className="lp-btn primary" onClick={onOpenApp}>
            View your meetings <span className="lp-arrow">→</span>
          </button>
        </section>
      )}

      <footer className="lp-foot">
        <div className="brand"><MicIcon size={16} /><span>MeetingScribe</span></div>
        <span>Runs on your Mac. <button className="lp-link" onClick={onOpenApp}>View your meetings</button></span>
      </footer>
    </div>
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
