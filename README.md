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
- email intake and reply agent

## What it does

Given a short business brief, the workflow generates:

- a positioning memo
- channel-specific copy
- outbound email and DM sequences
- a final QA pass for credibility and tone
- inbox checks and drafted replies for `help@argowake.com`

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
3. Copy `.env.example` to `.env` and set the email env vars if you want inbox sync or reply drafting:

```text
ARGOWAKE_EMAIL_ADDRESS=help@argowake.com
ARGOWAKE_OAUTH_CLIENT_ID=149074f2-4df2-4589-9352-6ddf1dc95244
ARGOWAKE_OAUTH_TENANT_ID=3d2a33ec-963c-4a36-aef6-b9401a859744
ARGOWAKE_IMAP_HOST=outlook.office365.com
ARGOWAKE_IMAP_PORT=993
ARGOWAKE_IMAP_USERNAME=help@argowake.com
ARGOWAKE_SMTP_HOST=smtp-mail.outlook.com
ARGOWAKE_SMTP_PORT=587
ARGOWAKE_SMTP_USERNAME=help@argowake.com
# optional
ARGOWAKE_OAUTH_SCOPES=offline_access,https://outlook.office.com/IMAP.AccessAsUser.All,https://outlook.office.com/SMTP.Send
ARGOWAKE_OAUTH_REDIRECT_PORT=8400
ARGOWAKE_TOKEN_CACHE_FILE=.state/office365_token_cache.json
```

4. Authenticate once in Edge and save the token cache locally:

```bash
uv run python main.py email auth
```

If Entra still shows `AADSTS500113`, add this redirect URI to the app registration:

- `http://localhost:8400`

Use the app registration's Authentication page, enable public client/native redirect support, and retry the auth command.

5. Run:

```bash
uv run python main.py --service-name Argowake
```

You can override the inputs:

```bash
uv run python main.py --service-name Argowake --audience "Startup founders" --objective "Book discovery calls"
```

Email commands:

```bash
uv run python main.py email auth
uv run python main.py email sync --limit 10
uv run python main.py email draft --limit 10 --message-index 1
uv run python main.py email draft --limit 10 --message-index 1 --send
```

## Recurring inbox check

A local automation is configured to check the inbox twice a day and summarize new mail:

- 9:00 AM
- 5:00 PM

It runs `py main.py email sync --limit 20` from this workspace.

## Files

- `agent.py` defines the agents and workflow.
- `main.py` is the CLI entrypoint.
- `docs/prompt.md` contains the runtime brief template.
