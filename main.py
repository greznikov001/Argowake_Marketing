from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from textwrap import indent

import structlog

from agent import MarketingBrief, generate_marketing_bundle, render_bundle
from budget import BudgetGuard
from marketing_team import MarketingDailyInput, render_daily_marketing_team, run_daily_marketing_team
from hubspot_tools import (
    HubSpotClient,
    HubSpotConfig,
    HubSpotOAuthConfig,
    HubSpotTokenStore,
    ProspectRecord,
    load_prospect_records,
    sync_records,
    sync_contacts,
)
from email_agent import draft_reply_with_site_context
from email_tools import EmailConfig, build_reply_message, fetch_recent_messages, send_email
from logging_setup import configure_logging, set_transaction_id
from oauth_tools import MsalTokenStore
from web_render import (
    render_rendered_web_pages_json,
    render_rendered_web_pages_text,
    render_site_for_llm,
    save_rendered_web_pages,
)
from web_gliner import extract_with_gliner, render_gliner_json, render_gliner_text, save_gliner_extraction
from web_email import scrape_emails, render_email_scrape_json, render_email_scrape_text, save_email_scrape
from web_tools import parse_website, render_website_parse_json, render_website_parse_result, save_website_parse_json


LOGGER = structlog.get_logger(__name__).bind(source="main.py")


def _load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Argowake marketing and email assistant.")
    subparsers = parser.add_subparsers(dest="command")

    marketing = subparsers.add_parser("marketing", help="Generate marketing assets.")
    marketing.add_argument("--service-name", default="Argowake", help="Service name to market.")
    marketing.add_argument(
        "--description",
        default=(
            "Fractional IT leadership for small and mid-sized professional firms, focused on cost "
            "optimization, cybersecurity and compliance oversight, vendor and project management, "
            "workflow automation, and practical AI guidance."
        ),
        help="Short description of the service.",
    )
    marketing.add_argument(
        "--audience",
        default="Small and mid-sized professional firms, especially 10-50 employee businesses",
        help="Primary audience.",
    )
    marketing.add_argument(
        "--objective",
        default="Book free cybersecurity and IT cost reviews",
        help="Primary marketing objective.",
    )
    marketing.add_argument(
        "--channels",
        default="Website, referral follow-up, email, and local outreach",
        help="Channels to focus on.",
    )
    marketing.add_argument("--voice", default="Plain-language, practical, and trustworthy", help="Brand voice.")
    marketing.add_argument(
        "--constraints",
        default="Avoid hype, avoid unverified claims, and emphasize cost savings, security, and business continuity.",
        help="Messaging constraints.",
    )
    marketing_subparsers = marketing.add_subparsers(dest="marketing_command")

    marketing_bundle = marketing_subparsers.add_parser("bundle", help="Generate a standard marketing bundle.")
    marketing_bundle.set_defaults(marketing_command="bundle")

    marketing_daily = marketing_subparsers.add_parser(
        "daily",
        help="Run the budget-first daily marketing team workflow.",
    )
    marketing_daily.add_argument(
        "--daily-budget-minutes",
        type=int,
        default=60,
        help="Time budget for the day.",
    )
    marketing_daily.add_argument(
        "--email-limit",
        type=int,
        default=10,
        help="How many recent inbox messages to review.",
    )
    marketing_daily.add_argument(
        "--scheduled-activity",
        action="append",
        default=[],
        help="Scheduled activity to include in the daily plan. Repeat for multiple items.",
    )
    marketing_daily.add_argument(
        "--skip-email",
        action="store_true",
        help="Run the daily workflow without reading inbox messages.",
    )
    marketing_daily.set_defaults(marketing_command="daily")

    email = subparsers.add_parser("email", help="Check inbound email and create replies.")
    email_subparsers = email.add_subparsers(dest="email_command")

    auth = email_subparsers.add_parser("auth", help="Sign in with Edge and cache the Office 365 token locally.")

    inbox = email_subparsers.add_parser("sync", help="Fetch recent inbound messages from help@argowake.com.")
    inbox.add_argument("--limit", type=int, default=10, help="Maximum messages to fetch.")

    draft = email_subparsers.add_parser("draft", help="Draft a reply for a selected inbound message.")
    draft.add_argument("--limit", type=int, default=10, help="Fetch recent messages before selecting one.")
    draft.add_argument("--message-index", type=int, default=1, help="1-based index of the message to reply to.")
    draft.add_argument("--send", action="store_true", help="Send the drafted reply after printing it.")

    hubspot = subparsers.add_parser("hubspot", help="Sync prospects into HubSpot.")
    hubspot_subparsers = hubspot.add_subparsers(dest="hubspot_command")

    hubspot_auth = hubspot_subparsers.add_parser("auth", help="Sign in to HubSpot and cache the token locally.")

    company = hubspot_subparsers.add_parser("company", help="Create or update one HubSpot company.")
    company.add_argument("--name", required=True, help="Company name.")
    company.add_argument("--website", help="Company website.")
    company.add_argument("--domain", help="Company domain.")
    company.add_argument("--phone", help="Company phone number.")
    company.add_argument("--city", help="Company city.")
    company.add_argument("--state", help="Company state.")
    company.add_argument("--industry", help="Company industry.")
    company.add_argument("--description", help="Short company description or notes.")

    contact = hubspot_subparsers.add_parser("contact", help="Create or update one HubSpot contact.")
    contact.add_argument("--email", required=True, help="Contact email.")
    contact.add_argument("--first-name", help="First name.")
    contact.add_argument("--last-name", help="Last name.")
    contact.add_argument("--company", help="Company name.")
    contact.add_argument("--phone", help="Phone number.")
    contact.add_argument("--job-title", help="Job title.")
    contact.add_argument("--city", help="City.")
    contact.add_argument("--state", help="State.")

    sync = hubspot_subparsers.add_parser("import", help="Import prospects from CSV, JSON, or markdown.")
    sync.add_argument("--input", required=True, help="Path to a prospect file.")

    contact_sync = hubspot_subparsers.add_parser(
        "contacts",
        help="Import contacts from a prospect file using identified or fallback contact names.",
    )
    contact_sync.add_argument("--input", required=True, help="Path to a prospect file.")

    web = subparsers.add_parser("web", help="Parse a website for contact and about details.")
    web_subparsers = web.add_subparsers(dest="web_command")
    web_parse = web_subparsers.add_parser(
        "parse",
        help="Find contact/about pages and extract founder names and contact addresses.",
    )
    web_parse.add_argument("--website", required=True, help="Company website or bare domain.")
    web_parse.add_argument("--max-pages", type=int, default=3, help="Maximum pages to inspect.")
    web_parse.add_argument("--json", action="store_true", help="Return structured JSON output.")

    web_render = web_subparsers.add_parser(
        "render",
        help="Render website pages to text plus an LLM-ready prompt without calling a model.",
    )
    web_render.add_argument("--website", required=True, help="Company website or bare domain.")
    web_render.add_argument("--max-pages", type=int, default=3, help="Maximum pages to inspect.")
    web_render.add_argument("--max-blocks", type=int, default=12, help="Maximum content blocks to keep per page.")
    web_render.add_argument("--json", action="store_true", help="Return structured JSON output.")

    web_gliner = web_subparsers.add_parser(
        "gliner",
        help="Render website pages and extract entities with a local GLiNER model.",
    )
    web_gliner.add_argument("--website", required=True, help="Company website or bare domain.")
    web_gliner.add_argument("--max-pages", type=int, default=3, help="Maximum pages to inspect.")
    web_gliner.add_argument("--max-blocks", type=int, default=12, help="Maximum content blocks to keep per page.")
    web_gliner.add_argument("--json", action="store_true", help="Return structured JSON output.")

    web_email = web_subparsers.add_parser(
        "email",
        help="Find email addresses on the homepage and contact/about pages.",
    )
    web_email.add_argument("--website", required=True, help="Company website or bare domain.")
    web_email.add_argument("--max-pages", type=int, default=3, help="Maximum pages to inspect.")
    web_email.add_argument("--json", action="store_true", help="Return structured JSON output.")
    web_email.add_argument("--verbose", action="store_true", help="Print the full raw JSON extraction output.")

    budget = subparsers.add_parser("budget", help="Inspect the local OpenAI budget guard.")
    budget_subparsers = budget.add_subparsers(dest="budget_command")
    budget_status = budget_subparsers.add_parser("status", help="Show the current estimated spend and remaining budget.")
    budget_status.set_defaults(budget_command="status")

    return parser


def _require_openai_api_key() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY before running this agent workflow.")


def _run_marketing(args: argparse.Namespace) -> int:
    LOGGER.info("Starting marketing command", command=getattr(args, "marketing_command", "bundle"))
    try:
        _require_openai_api_key()
        brief = MarketingBrief(
            service_name=args.service_name,
            description=args.description,
            audience=args.audience,
            objective=args.objective,
            channels=args.channels,
            voice=args.voice,
            constraints=args.constraints,
        )
        if getattr(args, "marketing_command", "bundle") == "daily":
            inbox_messages = []
            if not args.skip_email:
                config = EmailConfig.from_env()
                inbox_messages = fetch_recent_messages(config, limit=args.email_limit)
            scheduled_activities = args.scheduled_activity or [
                "Review inbox replies and draft responses",
                "Check SEO and research signals for the 25 target companies",
                "Draft one outbound message and one content asset",
                "Review analytics and decide what to defer",
            ]
            daily_input = MarketingDailyInput(
                brief=brief,
                inbox_messages=inbox_messages,
                scheduled_activities=scheduled_activities,
                daily_budget_minutes=args.daily_budget_minutes,
            )
            daily_output = run_daily_marketing_team(daily_input)
            LOGGER.info("Completed marketing daily workflow")
            print(render_daily_marketing_team(daily_output))
            return 0

        budget_guard = BudgetGuard.from_env()
        budget_guard.consume(budget_guard.estimated_cost_for_calls(4), "marketing bundle run")
        bundle = generate_marketing_bundle(brief)
        LOGGER.info("Completed marketing bundle workflow")
        print(render_bundle(bundle))
        return 0
    except Exception:
        LOGGER.exception("Marketing command failed")
        return 1


def _render_messages(messages) -> str:
    if not messages:
        return "No messages found."

    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        lines.append(
            "\n".join(
                [
                    f"[{index}] From: {message.from_address}",
                    f"    Subject: {message.subject}",
                    f"    Date: {message.date}",
                    f"    Snippet: {message.snippet}",
                ]
            )
        )
    return "\n\n".join(lines)


def _run_email_sync(args: argparse.Namespace) -> int:
    LOGGER.info("Starting email sync", limit=args.limit)
    try:
        config = EmailConfig.from_env()
        messages = fetch_recent_messages(config, limit=args.limit)
        LOGGER.info("Fetched email messages", count=len(messages))
        print(_render_messages(messages))
        return 0
    except Exception:
        LOGGER.exception("Email sync failed")
        return 1


def _run_email_auth() -> int:
    LOGGER.info("Starting email auth")
    try:
        token_store = MsalTokenStore.from_env()
        access_token, from_cache = token_store.acquire_token()
        LOGGER.info("Completed email auth", from_cache=from_cache)
        print("Microsoft sign-in complete.")
        print(f"Token cache: {token_store.token_cache_path}")
        print(f"Access token source: {'cache' if from_cache else 'interactive Edge sign-in'}")
        print(f"Access token length: {len(access_token)}")
        return 0
    except Exception:
        LOGGER.exception("Email auth failed")
        return 1


def _run_email_draft(args: argparse.Namespace) -> int:
    LOGGER.info("Starting email draft", limit=args.limit, message_index=args.message_index)
    try:
        _require_openai_api_key()
        budget_guard = BudgetGuard.from_env()
        budget_guard.consume(budget_guard.estimated_cost_for_calls(1), "email draft run")
        config = EmailConfig.from_env()
        messages = fetch_recent_messages(config, limit=args.limit)
        if not messages:
            print("No inbound messages found.", file=sys.stderr)
            return 1

        if args.message_index < 1 or args.message_index > len(messages):
            print(
                f"message-index must be between 1 and {len(messages)}.",
                file=sys.stderr,
            )
            return 2

        message = messages[args.message_index - 1]
        draft = draft_reply_with_site_context(message)

        print(f"Reply To: {message.from_address}")
        print(f"Subject: {draft.subject}")
        print("Body:")
        print(indent(draft.body, "  "))
        print("Rationale:")
        print(indent(draft.rationale, "  "))

        if args.send:
            reply_message = build_reply_message(
                config=config,
                to_address=message.from_address,
                subject=draft.subject,
                body_text=draft.body,
            )
            send_email(config, reply_message)
            LOGGER.info("Sent drafted email reply")
            print("Sent.")
        return 0
    except Exception:
        LOGGER.exception("Email draft failed")
        return 1


def _render_hubspot_result(result: dict[str, object]) -> str:
    properties = result.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    summary_name = (
        properties.get("name")
        or properties.get("email")
        or properties.get("firstname")
        or properties.get("company")
        or result.get("id")
    )
    return f"{result['action'].title()} {result['object_type']} {summary_name} (id={result['id']})"


def _run_hubspot_company(args: argparse.Namespace) -> int:
    LOGGER.info("Starting HubSpot company upsert", name=args.name)
    try:
        client = HubSpotClient(HubSpotConfig.from_env())
        record = ProspectRecord(
            company=args.name,
            website=args.website,
            domain=args.domain,
            phone=args.phone,
            city=args.city,
            state=args.state,
            industry=args.industry,
            description=args.description,
        )
        result = client.upsert_company(record)
        LOGGER.info("Completed HubSpot company upsert", action=result["action"], record_id=result["id"])
        print(_render_hubspot_result(result))
        return 0
    except Exception:
        LOGGER.exception("HubSpot company upsert failed")
        return 1


def _run_hubspot_auth() -> int:
    LOGGER.info("Starting HubSpot auth")
    try:
        token_store = HubSpotTokenStore.from_env()
        access_token, from_cache = token_store.acquire_token()
        config = HubSpotOAuthConfig.from_env()
        LOGGER.info("Completed HubSpot auth", from_cache=from_cache)
        print("HubSpot sign-in complete.")
        print(f"Token cache: {config.token_cache_path}")
        print(f"Access token source: {'cache' if from_cache else 'interactive OAuth sign-in'}")
        print(f"Access token length: {len(access_token)}")
        return 0
    except Exception:
        LOGGER.exception("HubSpot auth failed")
        return 1


def _run_hubspot_contact(args: argparse.Namespace) -> int:
    LOGGER.info("Starting HubSpot contact upsert", email=args.email)
    try:
        client = HubSpotClient(HubSpotConfig.from_env())
        record = ProspectRecord(
            email=args.email,
            first_name=args.first_name,
            last_name=args.last_name,
            company=args.company,
            phone=args.phone,
            job_title=args.job_title,
            city=args.city,
            state=args.state,
        )
        result = client.upsert_contact(record)
        LOGGER.info("Completed HubSpot contact upsert", action=result["action"], record_id=result["id"])
        print(_render_hubspot_result(result))
        return 0
    except Exception:
        LOGGER.exception("HubSpot contact upsert failed")
        return 1


def _run_hubspot_import(args: argparse.Namespace) -> int:
    LOGGER.info("Starting HubSpot import", input=args.input)
    try:
        client = HubSpotClient(HubSpotConfig.from_env())
        records = load_prospect_records(args.input)
        results = sync_records(client, records)
        created = sum(1 for result in results if result["action"] == "created")
        updated = sum(1 for result in results if result["action"] == "updated")
        LOGGER.info("Completed HubSpot import", total=len(results), created=created, updated=updated)
        print(f"Synced {len(results)} record(s): {created} created, {updated} updated.")
        for result in results[:10]:
            print(f"- {_render_hubspot_result(result)}")
        if len(results) > 10:
            print(f"- ... {len(results) - 10} more")
        return 0
    except Exception:
        LOGGER.exception("HubSpot import failed")
        return 1


def _run_hubspot_contacts(args: argparse.Namespace) -> int:
    LOGGER.info("Starting HubSpot contacts import", input=args.input)
    try:
        client = HubSpotClient(HubSpotConfig.from_env())
        records = load_prospect_records(args.input)
        results = sync_contacts(client, records)
        created = sum(1 for result in results if result["action"] == "created")
        updated = sum(1 for result in results if result["action"] == "updated")
        LOGGER.info("Completed HubSpot contacts import", total=len(results), created=created, updated=updated)
        print(f"Synced {len(results)} contact record(s): {created} created, {updated} updated.")
        for result in results[:10]:
            print(f"- {_render_hubspot_result(result)}")
        if len(results) > 10:
            print(f"- ... {len(results) - 10} more")
        return 0
    except Exception:
        LOGGER.exception("HubSpot contacts import failed")
        return 1


def _run_budget_status() -> int:
    LOGGER.info("Starting budget status")
    try:
        budget_guard = BudgetGuard.from_env()
        status = budget_guard.status()
        LOGGER.info("Completed budget status")
        print(f"Month: {status['month']}")
        print(f"Estimated spend: ${status['estimated_spend_usd']:.2f}")
        print(f"Remaining budget: ${status['remaining_usd']:.2f} / ${status['monthly_budget_usd']:.2f}")
        return 0
    except Exception:
        LOGGER.exception("Budget status failed")
        return 1


def _run_web_parse(args: argparse.Namespace) -> int:
    LOGGER.info("Starting web parse", website=args.website, max_pages=args.max_pages)
    try:
        result = parse_website(args.website, max_pages=args.max_pages)
        LOGGER.info(
            "Completed web parse",
            pages=len(result.discovered_pages),
            founders=len(result.founder_names),
            addresses=len(result.contact_addresses),
        )
        output_file = save_website_parse_json(result)
        LOGGER.info("Saved web parse output", file=str(output_file))
        if args.json:
            print(render_website_parse_json(result))
        else:
            print(render_website_parse_result(result))
        print(f"Saved JSON: {output_file}")
        return 0
    except Exception:
        LOGGER.exception("Web parse failed")
        return 1


def _run_web_render(args: argparse.Namespace) -> int:
    LOGGER.info("Starting web render", website=args.website, max_pages=args.max_pages)
    try:
        result = render_site_for_llm(args.website, max_pages=args.max_pages, max_blocks=args.max_blocks)
        LOGGER.info("Completed web render", pages=len(result.discovered_pages))
        output_file = save_rendered_web_pages(result)
        LOGGER.info("Saved web render output", file=str(output_file))
        if args.json:
            print(render_rendered_web_pages_json(result))
        else:
            print(render_rendered_web_pages_text(result))
        print(f"Saved JSON: {output_file}")
        return 0
    except Exception:
        LOGGER.exception("Web render failed")
        return 1


def _run_web_gliner(args: argparse.Namespace) -> int:
    LOGGER.info("Starting web gliner", website=args.website, max_pages=args.max_pages)
    try:
        result = extract_with_gliner(args.website, max_pages=args.max_pages, max_blocks=args.max_blocks)
        LOGGER.info("Completed web gliner", pages=len(result.discovered_pages))
        output_file = save_gliner_extraction(result)
        LOGGER.info("Saved web gliner output", file=str(output_file))
        if args.json:
            print(render_gliner_json(result))
        else:
            print(render_gliner_text(result))
        print(f"Saved JSON: {output_file}")
        return 0
    except Exception:
        LOGGER.exception("Web gliner failed")
        return 1


def _run_web_email(args: argparse.Namespace) -> int:
    LOGGER.info("Starting web email scrape", website=args.website, max_pages=args.max_pages)
    try:
        result = scrape_emails(args.website, max_pages=args.max_pages)
        LOGGER.info("Completed web email scrape", pages=len(result.discovered_pages), emails=len(result.emails))
        output_file = save_email_scrape(result)
        LOGGER.info("Saved web email output", file=str(output_file))
        if args.verbose:
            print(json.dumps(result.full_json or json.loads(render_email_scrape_json(result)), indent=2, sort_keys=True))
            if result.warnings:
                print("Warnings:")
                for warning in result.warnings:
                    print(f"- {warning['tag']}: {warning['message']}")
        elif args.json:
            print(render_email_scrape_json(result))
        else:
            print(render_email_scrape_text(result))
        print(f"Saved JSON: {output_file}")
        return 0
    except Exception:
        LOGGER.exception("Web email scrape failed")
        return 1


def main() -> int:
    _load_env_file()
    configure_logging()
    set_transaction_id(os.getenv("ARGOWAKE_TRANSACTION_ID"))
    LOGGER.info("Command start", argv=" ".join(sys.argv[1:]) or "default")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        args = parser.parse_args(["marketing"])
    if args.command == "marketing":
        exit_code = _run_marketing(args)
        LOGGER.info("Command end", exit_code=exit_code)
        return exit_code
    if args.command == "email":
        if args.email_command == "auth":
            exit_code = _run_email_auth()
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.email_command == "sync":
            exit_code = _run_email_sync(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.email_command == "draft":
            exit_code = _run_email_draft(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        parser.error("email requires a subcommand: auth, sync, or draft")
    if args.command == "hubspot":
        if args.hubspot_command == "auth":
            exit_code = _run_hubspot_auth()
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.hubspot_command == "company":
            exit_code = _run_hubspot_company(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.hubspot_command == "contact":
            exit_code = _run_hubspot_contact(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.hubspot_command == "import":
            exit_code = _run_hubspot_import(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.hubspot_command == "contacts":
            exit_code = _run_hubspot_contacts(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        parser.error("hubspot requires a subcommand: company, contact, contacts, or import")
    if args.command == "budget":
        if args.budget_command == "status":
            exit_code = _run_budget_status()
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        parser.error("budget requires a subcommand: status")
    if args.command == "web":
        if args.web_command == "parse":
            exit_code = _run_web_parse(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.web_command == "render":
            exit_code = _run_web_render(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.web_command == "gliner":
            exit_code = _run_web_gliner(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        if args.web_command == "email":
            exit_code = _run_web_email(args)
            LOGGER.info("Command end", exit_code=exit_code)
            return exit_code
        parser.error("web requires a subcommand: parse, render, gliner, or email")
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
