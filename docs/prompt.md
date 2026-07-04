# Runtime brief template

Use this structure when generating marketing work:

- Service name
- Description
- Audience
- Objective
- Channels
- Voice
- Constraints

For Argowake, anchor the brief in the public site positioning:

- fractional IT leadership
- cost optimization
- cybersecurity/compliance oversight
- vendor and project management
- workflow automation
- AI guidance

For email replies, keep the tone consistent with the site:

- practical and plain-language
- focused on security and cost control
- aligned to small and mid-sized business needs
- specific about next steps

Office 365 email integration uses OAuth2/Modern Auth via:

- IMAP host `outlook.office365.com`
- IMAP port `993`
- SMTP host `smtp-mail.outlook.com`
- SMTP port `587`
- `ARGOWAKE_OAUTH_CLIENT_ID` for the public client app registration
- default client ID `149074f2-4df2-4589-9352-6ddf1dc95244`
- default tenant ID `3d2a33ec-963c-4a36-aef6-b9401a859744`
- redirect URI `http://localhost:8400`
- `ARGOWAKE_TOKEN_CACHE_FILE` for the local serialized MSAL token cache
- `python main.py email auth` to open Edge sign-in and save the cache locally

The workflow should optimize for:

- specificity over broad claims
- short, testable messaging
- credible proof points
- channel-appropriate format
- a 60-minute daily operating loop
- manager-led delegation with three parallel specialists
- inbox replies and scheduled marketing activities as the daily trigger
- an estimated monthly OpenAI budget guard that blocks over-cap runs
