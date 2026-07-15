"""Vocabulary biasing for speech recognition.

Apple's SpeechAnalyzer accepts "contextual strings" — words/phrases it
should favor when audio is ambiguous. Meetings in tech are full of terms
the general model mangles ("GitHub" -> "get hub"), so a curated default
list ships with the app. Priority order when the list is capped: the
user's own config vocabulary, then calendar attendee names, then these
defaults. Short, correctly-cased words and 2-word phrases work best;
huge lists degrade recognition, so callers cap the merged list.
"""

# ~120 terms people actually say in product/engineering meetings where the
# stock recognizer typically fails or mis-cases.
DEFAULT_VOCABULARY = [
    # code & collaboration
    "GitHub", "GitLab", "Bitbucket", "repo", "pull request", "merge conflict",
    "commit", "rebase", "monorepo", "code review", "changelog",
    # languages & runtimes
    "Python", "TypeScript", "JavaScript", "Swift", "Kotlin", "Rust", "Golang",
    "Node.js", "React", "Next.js", "Vite", "Tailwind", "SwiftUI", "Flutter",
    # infra & cloud
    "Kubernetes", "Docker", "Terraform", "AWS", "GCP", "Azure", "Vercel",
    "Cloudflare", "PostgreSQL", "Postgres", "MongoDB", "Redis", "SQLite",
    "GraphQL", "gRPC", "webhook", "microservices", "serverless", "DevOps",
    "CI CD", "staging", "prod", "rollback", "canary", "load balancer",
    "latency", "throughput", "uptime", "observability", "Datadog", "Grafana",
    # AI
    "Claude", "ChatGPT", "OpenAI", "Anthropic", "Gemini", "Copilot", "LLM",
    "GenAI", "RAG", "fine-tuning", "embeddings", "inference", "quantization",
    "prompt engineering", "agentic", "MCP", "tokens", "context window",
    "hallucination", "on-device", "Neural Engine", "Whisper", "transformer",
    # product & work
    "roadmap", "sprint", "standup", "retro", "backlog", "OKR", "KPI", "MVP",
    "beta", "launch", "onboarding", "churn", "retention", "conversion",
    "A/B test", "analytics", "dashboard", "wireframe", "Figma", "mockup",
    "user research", "PRD", "spec", "stakeholder", "deliverable",
    # tools
    "Jira", "Linear", "Notion", "Slack", "Zoom", "Google Meet", "Teams",
    "Confluence", "Salesforce", "HubSpot", "Stripe", "Shopify", "Airtable",
    "Zapier", "Calendly", "Miro", "Asana",
    # formats & misc
    "API", "SDK", "CLI", "JSON", "YAML", "OAuth", "SSO", "JWT", "npm",
    "regex", "endpoint", "middleware", "backend", "frontend", "full-stack",
    "TestFlight", "App Store", "InsForge", "MeetingScribe",
]

# Post-pass corrections for mishearings the bias list alone can't always fix.
# ONLY unambiguous multi-word mishearings belong here — anything that could
# plausibly be real speech in a meeting (e.g. "jason", "get up" alone) must
# stay out. Applied case-insensitively on word boundaries to the batch
# transcript only.
ALIASES = {
    "get hub": "GitHub",
    "git hub": "GitHub",
    "get lab": "GitLab",
    "pool request": "pull request",
    "cooper netties": "Kubernetes",
    "kuber netties": "Kubernetes",
    "post gress": "Postgres",
    "no js": "Node.js",
    "view js": "Vue.js",
    "type script": "TypeScript",
    "java script": "JavaScript",
    "chat gpt": "ChatGPT",
    "an thropic": "Anthropic",
}


def merge_context(user_vocabulary, calendar_names, cap=100):
    """User terms first, then attendee names, then defaults — deduped, capped."""
    merged, seen = [], set()
    for source in (user_vocabulary or []), (calendar_names or []), DEFAULT_VOCABULARY:
        for term in source:
            term = str(term).strip()
            key = term.lower()
            if term and key not in seen:
                seen.add(key)
                merged.append(term)
    return merged[:cap]
