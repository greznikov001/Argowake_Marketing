# Argowake Marketing Agents

Minimal OpenAI Agents SDK starter for marketing Argowake with a small specialist team:

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
