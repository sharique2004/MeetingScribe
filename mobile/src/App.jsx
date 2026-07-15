// MeetingScribe on your phone — read-only companion web app.
//
// Shows the meetings you chose to sync from the Mac app ("View on phone"):
// transcript + summary text. Audio never leaves the Mac. Rows are protected
// by owner-only RLS; this app just signs in and reads.

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { insforge } from "./insforge.js";

const PAGE = 25;

/* ------------------------------------------------------------- helpers -- */

function fmtDuration(seconds) {
  if (!seconds) return "";
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString([], {
    month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }),
  }) + " · " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function fmtTs(s) {
  s = Math.max(0, Math.floor(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const SPEAKER_COLORS = ["#5fb8c9", "#c98f5f", "#9a7fd1", "#7fbf7f", "#d17fa5", "#b9b96a"];

function speakerColor(label, speakers) {
  if (label === "you") return "#5fb8c9";
  const others = Object.keys(speakers || {}).filter((k) => k !== "you");
  return SPEAKER_COLORS[(others.indexOf(label) + 1) % SPEAKER_COLORS.length];
}

function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const match = hash.match(/^#\/m\/(.+)$/);
  return match ? decodeURIComponent(match[1]) : null;
}

/* --------------------------------------------------------------- login -- */

function Login({ onSignedIn }) {
  const [mode, setMode] = useState("signin"); // signin | signup | verify
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (mode === "verify") {
        const { data, error } = await insforge.auth.verifyEmail({ email, otp: code.trim() });
        if (error) throw error;
        if (data?.user) return onSignedIn(data.user);
      } else if (mode === "signup") {
        const { data, error } = await insforge.auth.signUp({ email, password });
        if (error) throw error;
        if (data?.requireEmailVerification) return setMode("verify");
        if (data?.user) return onSignedIn(data.user);
      } else {
        const { data, error } = await insforge.auth.signInWithPassword({ email, password });
        if (error) throw error;
        if (data?.requireEmailVerification) return setMode("verify");
        if (data?.user) return onSignedIn(data.user);
      }
      setError("Something unexpected happened — try again.");
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  }

  async function google() {
    setError("");
    await insforge.auth.signInWithOAuth("google", { redirectTo: window.location.origin });
  }

  return (
    <div className="login">
      <div className="login-card">
        <div className="brand">
          <MicIcon />
          <h1>MeetingScribe</h1>
        </div>
        <p className="sub">
          Sign in with the same account as the Mac app to read the meetings
          you synced. Transcripts and summaries only — audio stays on the Mac.
        </p>
        <button className="btn google" onClick={google} disabled={busy}>
          Continue with Google
        </button>
        <div className="sep"><span>or</span></div>
        <form onSubmit={submit}>
          {mode === "verify" ? (
            <>
              <p className="hint">We emailed a 6-digit code to <b>{email}</b>.</p>
              <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456"
                     inputMode="numeric" maxLength={6} autoComplete="one-time-code" required />
              <button className="btn primary" disabled={busy}>
                {busy ? "Verifying…" : "Verify & sign in"}
              </button>
            </>
          ) : (
            <>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                     placeholder="Email" autoComplete="username" required />
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                     placeholder={mode === "signup" ? "Password (6+ characters)" : "Password"}
                     autoComplete={mode === "signup" ? "new-password" : "current-password"} required />
              <button className="btn primary" disabled={busy}>
                {busy ? "One moment…" : mode === "signup" ? "Create account" : "Sign in"}
              </button>
            </>
          )}
        </form>
        {error && <p className="error">{error}</p>}
        {mode !== "verify" && (
          <p className="hint center">
            {mode === "signup" ? (
              <>Already have an account?{" "}
                <a href="#" onClick={(e) => { e.preventDefault(); setMode("signin"); }}>Sign in</a></>
            ) : (
              <>New here?{" "}
                <a href="#" onClick={(e) => { e.preventDefault(); setMode("signup"); }}>Create an account</a></>
            )}
          </p>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- list -- */

function MeetingList({ onOpen }) {
  const [items, setItems] = useState([]);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (offset) => {
    setBusy(true);
    setError("");
    const { data, error } = await insforge.database
      .from("meetings")
      .select("meeting_id, title, created, duration, mode, summary")
      .order("created", { ascending: false })
      .range(offset, offset + PAGE); // one extra row = "there's more"
    if (error) {
      setError(error.message || "Could not load meetings.");
    } else {
      const page = data || [];
      setHasMore(page.length > PAGE);
      setItems((prev) => offset === 0 ? page.slice(0, PAGE) : [...prev, ...page.slice(0, PAGE)]);
    }
    setBusy(false);
  }, []);

  useEffect(() => { load(0); }, [load]);

  return (
    <div className="list">
      {error && <p className="error pad">{error}</p>}
      {!busy && !error && items.length === 0 && (
        <div className="empty">
          <MicIcon />
          <h2>No meetings here yet</h2>
          <p>On your Mac, open a meeting and tap <b>“View on phone”</b> —
          it appears here within seconds.</p>
        </div>
      )}
      {items.map((m) => (
        <button key={m.meeting_id} className="row" onClick={() => onOpen(m.meeting_id)}>
          <div className="row-title">{m.title || m.meeting_id}</div>
          <div className="row-meta">
            {fmtDate(m.created)}
            {m.duration ? <> · {fmtDuration(m.duration)}</> : null}
            {m.mode ? <> · {m.mode === "inperson" ? "In person" : "Online"}</> : null}
          </div>
          {m.summary?.tldr && <div className="row-tldr">{m.summary.tldr}</div>}
        </button>
      ))}
      {busy && <div className="pad dim">Loading…</div>}
      {hasMore && !busy && (
        <button className="btn more" onClick={() => load(items.length)}>Show older meetings</button>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- detail -- */

function MeetingDetail({ meetingId, onBack }) {
  const [meeting, setMeeting] = useState(null);
  const [tab, setTab] = useState("summary");
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      const { data, error } = await insforge.database
        .from("meetings").select("*").eq("meeting_id", meetingId).limit(1);
      if (error) setError(error.message);
      else if (!data || !data.length) setError("This meeting is no longer synced.");
      else {
        setMeeting(data[0]);
        setTab(data[0].summary?.tldr ? "summary" : "transcript");
      }
    })();
  }, [meetingId]);

  const turns = meeting?.turns || [];
  const speakers = meeting?.speakers || {};
  const summary = meeting?.summary;

  const grouped = useMemo(() => {
    const out = [];
    for (const t of turns) {
      const prev = out[out.length - 1];
      if (prev && prev.speaker === t.speaker) prev.texts.push(t.text);
      else out.push({ speaker: t.speaker, start: t.start, texts: [t.text] });
    }
    return out;
  }, [turns]);

  if (error) {
    return (
      <div className="detail">
        <p className="error pad">{error}</p>
        <button className="btn more" onClick={onBack}>Back to meetings</button>
      </div>
    );
  }
  if (!meeting) return <div className="pad dim">Loading…</div>;

  return (
    <div className="detail">
      <h1 className="d-title">{meeting.title}</h1>
      <div className="d-meta">
        {fmtDate(meeting.created)}
        {meeting.duration ? <> · {fmtDuration(meeting.duration)}</> : null}
      </div>
      {summary?.tldr && (
        <div className="tabs">
          <button className={tab === "summary" ? "on" : ""} onClick={() => setTab("summary")}>Summary</button>
          <button className={tab === "transcript" ? "on" : ""} onClick={() => setTab("transcript")}>Transcript</button>
        </div>
      )}
      {tab === "summary" && summary?.tldr ? (
        <div className="summary">
          <p className="tldr">{summary.tldr}</p>
          {[["Key points", summary.key_points],
            ["Decisions", summary.decisions],
            ["Follow-ups", summary.follow_ups],
            ["Open questions", summary.open_questions]].map(([label, list]) =>
            list?.length ? (
              <section key={label}>
                <h3>{label}</h3>
                <ul>{list.map((x, i) => <li key={i}>{x}</li>)}</ul>
              </section>
            ) : null
          )}
          {summary.action_items?.length ? (
            <section>
              <h3>Action items</h3>
              <ul className="actions">
                {summary.action_items.map((a, i) => (
                  <li key={i}><b>{a.owner || "—"}:</b> {a.task}
                    {a.due ? <span className="due"> — {a.due}</span> : null}</li>
                ))}
              </ul>
            </section>
          ) : null}
          {summary.follow_up_email?.body ? (
            <section>
              <h3>Draft follow-up email</h3>
              {summary.follow_up_email.subject && (
                <p className="dim">Subject: {summary.follow_up_email.subject}</p>
              )}
              <blockquote>{summary.follow_up_email.body}</blockquote>
            </section>
          ) : null}
        </div>
      ) : (
        <div className="transcript">
          {grouped.map((g, i) => (
            <div className="turn" key={i}>
              <div className="turn-head">
                <span className="who" style={{ color: speakerColor(g.speaker, speakers) }}>
                  {speakers[g.speaker] || g.speaker}
                </span>
                <span className="ts">{fmtTs(g.start)}</span>
              </div>
              <p>{g.texts.join(" ")}</p>
            </div>
          ))}
          {!grouped.length && <p className="dim pad">No transcript text.</p>}
        </div>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- app -- */

function MicIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
         stroke="#5fb8c9" strokeWidth="2" strokeLinecap="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  );
}

export default function App() {
  const [user, setUser] = useState(undefined); // undefined = still checking
  const meetingId = useHashRoute();

  useEffect(() => {
    (async () => {
      const { data } = await insforge.auth.getCurrentUser();
      setUser(data?.user || null);
    })();
  }, []);

  async function signOut() {
    if (!window.confirm("Sign out?")) return;
    await insforge.auth.signOut();
    setUser(null);
    window.location.hash = "";
  }

  if (user === undefined) return <div className="boot">MeetingScribe…</div>;
  if (!user) return <Login onSignedIn={setUser} />;

  return (
    <div className="shell">
      <header className="top">
        {meetingId ? (
          <button className="back" onClick={() => (window.location.hash = "")}>‹ Meetings</button>
        ) : (
          <div className="brand small"><MicIcon /><span>MeetingScribe</span></div>
        )}
        <button className="signout" onClick={signOut} title={user.email}>Sign out</button>
      </header>
      {meetingId ? (
        <MeetingDetail meetingId={meetingId} onBack={() => (window.location.hash = "")} />
      ) : (
        <MeetingList onOpen={(id) => (window.location.hash = `#/m/${encodeURIComponent(id)}`)} />
      )}
    </div>
  );
}
