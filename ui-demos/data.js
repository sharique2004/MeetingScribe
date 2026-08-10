/* Shared dummy data for all MeetingScribe UI demos.
   Not wired to the real app — pure fixtures so every demo shows identical content. */
window.DEMO = (function () {
  const SPEAKERS = {
    you:    { key: "you",    name: "You",         color: "#5b8cff", initials: "Y", role: "you" },
    priya:  { key: "priya",  name: "Priya Nair",  color: "#c07af6", initials: "PN" },
    marcus: { key: "marcus", name: "Marcus Bell", color: "#2fbf87", initials: "MB" },
  };

  // share = fraction of speaking time; seconds derived from duration.
  const SHARE = { you: 0.41, priya: 0.34, marcus: 0.25 };

  const MEETING = {
    id: "demo-q3-roadmap",
    title: "Q3 Roadmap Sync",
    created: "Today · 2:14 PM",
    dateShort: "Jul 26",
    duration: 1458,            // 24:18
    mode: "online",            // online | inperson
    engine: "Whisper · GPU",
    language: "English",
    people: 3,
  };

  // t = start second. Longer paragraphs are intentional (tests reading column + smooth scroll).
  const TRANSCRIPT = [
    { t: 3,    s: "you",    text: "Okay, we're recording. Let's keep this to the three things that actually block Q3 — pricing, the mobile beta, and the data-retention work. Priya, want to start with where pricing landed?" },
    { t: 19,   s: "priya",  text: "Sure. So after the customer calls last week, the usage-based tier is the one people keep asking for. The flat plan works for small teams, but everyone above roughly forty seats wants to pay for what they actually use." },
    { t: 41,   s: "priya",  text: "The risk is billing complexity. If we ship metered pricing we need per-workspace usage reporting before launch, or support drowns in \"why is my invoice this number\" tickets." },
    { t: 62,   s: "marcus", text: "Metering itself isn't the hard part — we already emit usage events. The hard part is making them trustworthy enough to put on an invoice. That's reconciliation, idempotency, the boring stuff. I'd want two weeks just for that." },
    { t: 88,   s: "you",    text: "Two weeks is fine if it means we don't refund half the first month. Let's treat trustworthy metering as the actual gate for the pricing launch, not a nice-to-have." },
    { t: 104,  s: "priya",  text: "Agreed. I'll write the pricing page copy against the usage tier and we hold the launch until metering is signed off. Marcus, can you own the reconciliation piece?" },
    { t: 121,  s: "marcus", text: "Yep, I'll own it. I'll have a design doc by Thursday and a number for how confident we can be by end of quarter." },
    { t: 137,  s: "you",    text: "Perfect. Second thing — the mobile beta. Where are we on the TestFlight build? Last I heard we were blocked on push notifications." },
    { t: 154,  s: "marcus", text: "Push is unblocked as of yesterday. The remaining thing is offline sync — if someone records on the subway and comes back online, we occasionally double-count a session. It's rare but it looks terrible." },
    { t: 178,  s: "priya",  text: "How rare is rare? Because if it's one in a thousand I'd rather ship the beta and fix it in flight than hold the whole thing." },
    { t: 191,  s: "marcus", text: "Closer to one in two hundred under bad connectivity. I think we ship the beta with a known-issues note and I fix the dedupe in the first beta update. It's the same idempotency work as billing, honestly." },
    { t: 214,  s: "you",    text: "Good — bundle them. If the same fix covers billing and mobile, that's one problem, not two. Ship the beta Friday with the note, and let's get real devices in real hands over the weekend." },
    { t: 233,  s: "priya",  text: "I'll line up ten beta users from the waitlist and write the known-issues note so it sounds intentional, not scary." },
    { t: 247,  s: "you",    text: "Last one, and this is the boring-but-important one: data retention. Legal wants a documented policy before we sign the two enterprise deals in the pipeline." },
    { t: 264,  s: "marcus", text: "Technically we can already delete on a schedule. What we don't have is proof — an audit log that shows a given recording was actually purged, not just marked deleted. Enterprise security reviews always ask for that." },
    { t: 288,  s: "priya",  text: "So the deliverable is really two things: a written retention policy, and a purge audit trail we can show. I can draft the policy with legal this week." },
    { t: 305,  s: "marcus", text: "And I'll add the purge audit log. It's small — a day, maybe two. I'll fold it into the same reconciliation branch since it's all the same event pipeline." },
    { t: 322,  s: "you",    text: "Okay. So everything this quarter funnels through one event-pipeline hardening effort — metering, mobile dedupe, and purge proof. Marcus, that makes you the critical path. Shout the moment it slips." },
    { t: 341,  s: "marcus", text: "Will do. If reconciliation takes longer than the two weeks, pricing is the thing that moves — mobile and retention can trail it." },
    { t: 356,  s: "you",    text: "That's the right call. Let's leave it there. Priya, you've got pricing copy and beta users; Marcus, you've got the pipeline. We'll check in Thursday on the design doc." },
  ];

  const SUMMARY = {
    tldr: "The quarter narrows to one technical bet: hardening the usage-event pipeline so it's trustworthy enough for billing, mobile offline sync, and enterprise purge proof. Usage-based pricing launches only once metering is reconciled; the mobile beta ships Friday with a known-issues note; a data-retention policy plus purge audit log unblocks two enterprise deals. Marcus is the critical path.",
    decisions: [
      "Trustworthy metering (reconciliation + idempotency) is the hard gate for the pricing launch — not a nice-to-have.",
      "Mobile beta ships Friday with a known-issues note; the offline double-count is fixed in the first beta update.",
      "Billing, mobile dedupe, and purge proof are treated as one event-pipeline effort, not three.",
    ],
    actions: [
      { owner: "Marcus", ownerKey: "marcus", task: "Reconciliation design doc + confidence estimate", due: "Thursday" },
      { owner: "Priya",  ownerKey: "priya",  task: "Pricing page copy against the usage tier", due: "This week" },
      { owner: "Priya",  ownerKey: "priya",  task: "Recruit 10 beta users + write known-issues note", due: "Friday" },
      { owner: "Marcus", ownerKey: "marcus", task: "Add purge audit log to the reconciliation branch", due: "Next week" },
    ],
    topics: [
      { label: "Usage-based pricing", weight: 0.9 },
      { label: "Metering reconciliation", weight: 1.0 },
      { label: "Mobile beta / offline sync", weight: 0.7 },
      { label: "Data retention & purge proof", weight: 0.6 },
      { label: "Enterprise deals", weight: 0.4 },
    ],
    email: `Hi team,

Quick recap of the Q3 sync:

We're consolidating the quarter around one effort — hardening the usage-event pipeline — because it unblocks three things at once: billing, mobile offline sync, and enterprise purge proof.

• Pricing: usage-based tier launches once metering is reconciled and trustworthy. Marcus owns reconciliation; design doc Thursday.
• Mobile: beta ships Friday with a known-issues note. Offline dedupe lands in the first beta update.
• Retention: Priya drafts the policy with legal this week; Marcus adds a purge audit log.

Marcus is the critical path — if reconciliation slips past two weeks, pricing moves first. Next check-in Thursday.

Thanks,`,
  };

  const INSIGHTS = [
    { label: "Talk time",        value: "24:18",  sub: "3 speakers" },
    { label: "Decisions",        value: "3",      sub: "all owned" },
    { label: "Action items",     value: "4",      sub: "2 due Thu/Fri" },
    { label: "Questions raised", value: "5",      sub: "4 answered" },
  ];

  // Sample Ask thread
  const ASK = [
    { role: "user", text: "What did we actually decide about pricing?" },
    { role: "assistant", text: "You decided that usage-based pricing can't launch until metering is reconciled and trustworthy — reconciliation is the hard gate, not a nice-to-have. Marcus owns it and will bring a design doc Thursday plus an estimate of how confident the numbers can be by end of quarter." },
    { role: "user", text: "Who's on the critical path?" },
    { role: "assistant", text: "Marcus. Everything this quarter funnels through one event-pipeline hardening effort — metering, mobile dedupe, and the purge audit log all share that branch — so if reconciliation slips, pricing is the piece that moves first." },
  ];

  // Live-recording sample (for demos that show a recording state)
  const LIVE = {
    elapsed: 372, // 6:12
    rows: [
      { who: "You",         you: true,  text: "So the main thing I want to lock today is whether metering is a launch blocker." },
      { who: "Priya Nair",  you: false, text: "I think it has to be — support can't absorb billing disputes at this stage." },
      { who: "Marcus Bell", you: false, text: "Give me two weeks on reconciliation and I can tell you how confident we'll be." },
      { who: "You",         you: true,  text: "Okay, let's treat it as the gate then.", partial: true },
    ],
    cues: [
      { text: "Decide if metering blocks the pricing launch", covered: true },
      { text: "Confirm mobile beta ship date", covered: true },
      { text: "Assign the data-retention policy owner", covered: true },
      { text: "Set the next check-in", covered: false },
    ],
  };

  // Past meetings for the sidebar list, grouped by recency. The first (active) one
  // is the Q3 Roadmap Sync shown in the main view.
  const MEETINGS = [
    { group: "Today", items: [
      { id: "demo-q3-roadmap", title: "Q3 Roadmap Sync",        time: "2:14 PM",  duration: "24:18", speakers: 3, mode: "online",   active: true },
      { id: "m-design-mobile", title: "Design review — mobile", time: "11:05 AM", duration: "38:52", speakers: 4, mode: "online" },
    ]},
    { group: "Yesterday", items: [
      { id: "m-priya-1on1",    title: "1:1 with Priya",          time: "4:30 PM",  duration: "27:41", speakers: 2, mode: "online" },
      { id: "m-northwind",     title: "Customer call — Northwind", time: "1:00 PM", duration: "44:09", speakers: 5, mode: "online" },
      { id: "m-standup",       title: "Eng standup",             time: "9:30 AM",  duration: "12:03", speakers: 6, mode: "inperson" },
    ]},
    { group: "This week", items: [
      { id: "m-pricing-wksp",  title: "Pricing workshop",        time: "Wed",      duration: "58:20", speakers: 4, mode: "inperson" },
      { id: "m-board-prep",    title: "Board prep",              time: "Tue",      duration: "1:02:11", speakers: 3, mode: "online" },
      { id: "m-vendor-audio",  title: "Vendor call — audio",     time: "Mon",      duration: "19:47", speakers: 2, mode: "online" },
    ]},
    { group: "Earlier", items: [
      { id: "m-q3-kickoff",    title: "Kickoff — Q3 planning",   time: "Jul 18",   duration: "51:33", speakers: 7, mode: "inperson" },
      { id: "m-retro-june",    title: "June retro",              time: "Jul 11",   duration: "33:16", speakers: 5, mode: "online" },
    ]},
  ];

  function fmtTs(s) {
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60), sec = s % 60;
    return m + ":" + String(sec).padStart(2, "0");
  }
  function speakerSeconds(key) { return Math.round(MEETING.duration * (SHARE[key] || 0)); }

  return { SPEAKERS, SHARE, MEETING, MEETINGS, TRANSCRIPT, SUMMARY, INSIGHTS, ASK, LIVE, fmtTs, speakerSeconds };
})();
