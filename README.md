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
- HubSpot OAuth prospect sync
- daily marketing manager + specialist workflow

## What it does

Given a short business brief, the workflow generates:

- a positioning memo
- channel-specific copy
- outbound email and DM sequences
- a final QA pass for credibility and tone
- inbox checks and drafted replies for `help@argowake.com`
- HubSpot company/contact upserts for prospect tracking
- a budget-first daily marketing workflow with one manager agent and three specialists

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
ARGOWAKE_HUBSPOT_CLIENT_ID=replace_with_client_id
ARGOWAKE_HUBSPOT_CLIENT_SECRET=replace_with_client_secret
ARGOWAKE_HUBSPOT_REDIRECT_URI=http://localhost:8400
ARGOWAKE_HUBSPOT_SCOPES=crm.objects.contacts.read,crm.objects.contacts.write,crm.objects.companies.read,crm.objects.companies.write
ARGOWAKE_HUBSPOT_TOKEN_CACHE_FILE=.state/hubspot_token_cache.json
ARGOWAKE_HUBSPOT_ACCESS_TOKEN=
ARGOWAKE_HUBSPOT_BASE_URL=https://api.hubapi.com
ARGOWAKE_HUBSPOT_TIMEOUT_SECONDS=30
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

HubSpot commands:

```bash
uv run python main.py hubspot auth
uv run python main.py hubspot company --name "Example Co" --website https://example.com
uv run python main.py hubspot contact --email person@example.com --first-name Pat --company "Example Co"
uv run python main.py hubspot import --input prospects.csv
uv run python main.py hubspot contacts --input prospects.csv
```

Daily marketing workflow:

```bash
uv run python main.py marketing daily
uv run python main.py marketing daily --daily-budget-minutes 60 --scheduled-activity "Follow up on inbound leads" --scheduled-activity "Publish one post"
uv run python main.py marketing daily --skip-email
```

Budget guard:

```bash
uv run python main.py budget status
```

By default the local guard tracks an estimated monthly OpenAI spend against a `$5.00` cap in `.state/openai_budget_ledger.json`. Override with:

- `ARGOWAKE_OPENAI_MONTHLY_BUDGET_USD`
- `ARGOWAKE_OPENAI_ESTIMATED_CALL_COST_USD`
- `ARGOWAKE_OPENAI_BUDGET_LEDGER_FILE`

The daily workflow:

- reads recent inbox messages
- has the manager agent allocate the 60-minute budget
- runs Research/SEO, Content/Outreach, and Analytics in parallel
- returns one final daily action plan
- refuses new runs when the estimated monthly budget would be exceeded

## HubSpot setup

1. Create the HubSpot **MCP Auth App** and copy the `Client ID` and `Client secret`.
2. Set `ARGOWAKE_HUBSPOT_CLIENT_ID`, `ARGOWAKE_HUBSPOT_CLIENT_SECRET`, and `ARGOWAKE_HUBSPOT_REDIRECT_URI` in your local `.env` file at `C:\Users\Gene\OneDrive\Documents\Argowake 2\.env`.
3. Run `uv run python main.py hubspot auth` once to open OAuth in Edge and cache the token locally under `C:\Users\Gene\OneDrive\Documents\Argowake 2\.state\hubspot_token_cache.json`.
4. Use `hubspot company` for company-only prospects and `hubspot contact` when you have an email address.
5. Use `hubspot import` for CSV, JSON, or markdown prospect lists.

Do not commit `.env` or any HubSpot secret to GitHub. The repo already ignores `.env`.

Do not commit `.env` or any client secret to GitHub. The repository already ignores `.env` in `C:\Users\Gene\OneDrive\Documents\Argowake 2\.gitignore`.

## Recurring inbox check

A local automation is configured to check the inbox twice a day and summarize new mail:

- 9:00 AM
- 5:00 PM

It runs `py main.py email sync --limit 20` from this workspace.

## Files

- `agent.py` defines the agents and workflow.
- `main.py` is the CLI entrypoint.
- `hubspot_tools.py` handles HubSpot upserts and file imports.
- `docs/prompt.md` contains the runtime brief template.
