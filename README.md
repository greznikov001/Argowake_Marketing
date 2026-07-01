# Argowake Marketing Agents

Minimal OpenAI Agents SDK starter for marketing Argowake using the positioning on the current site:

- Fractional IT leadership for small businesses
- cost optimization
- cybersecurity and compliance oversight
- vendor and project management
- workflow automation
- AI guidance

- strategy agent
- copywriting agent
- outreach agent
- brand QA agent

## What it does

Given a short business brief, the workflow generates:

- a positioning memo
- channel-specific copy
- outbound email and DM sequences
- a final QA pass for credibility and tone

## Site-derived initial content

The default brief and messaging are based on the public site copy at `argowake.com`, which emphasizes:

- Campbell-based fractional IT leadership
- 10–50 employee firms
- cybersecurity and IT cost reviews
- Microsoft 365 and Google Workspace security/migrations
- vendor selection and systems oversight

Common industries listed on the site:

- retail
- insurance
- construction
- public sector
- financial services

## Run locally

1. Set `OPENAI_API_KEY`.
2. Install dependencies with `uv sync`.
3. Run:

```bash
uv run python main.py --service-name Argowake
```

You can override the inputs:

```bash
uv run python main.py --service-name Argowake --audience "Startup founders" --objective "Book discovery calls"
```

## Files

- `agent.py` defines the agents and workflow.
- `main.py` is the CLI entrypoint.
- `docs/prompt.md` contains the runtime brief template.
