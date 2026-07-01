from __future__ import annotations

import argparse
import os
import sys
from textwrap import indent

from agent import MarketingBrief, generate_marketing_bundle, render_bundle
from email_agent import draft_reply_with_site_context
from email_tools import EmailConfig, build_reply_message, fetch_recent_messages, send_email
from oauth_tools import MsalTokenStore


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

    email = subparsers.add_parser("email", help="Check inbound email and create replies.")
    email_subparsers = email.add_subparsers(dest="email_command")

    auth = email_subparsers.add_parser("auth", help="Sign in with Edge and cache the Office 365 token locally.")

    inbox = email_subparsers.add_parser("sync", help="Fetch recent inbound messages from help@argowake.com.")
    inbox.add_argument("--limit", type=int, default=10, help="Maximum messages to fetch.")

    draft = email_subparsers.add_parser("draft", help="Draft a reply for a selected inbound message.")
    draft.add_argument("--limit", type=int, default=10, help="Fetch recent messages before selecting one.")
    draft.add_argument("--message-index", type=int, default=1, help="1-based index of the message to reply to.")
    draft.add_argument("--send", action="store_true", help="Send the drafted reply after printing it.")

    return parser


def _require_openai_api_key() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY before running this agent workflow.")


def _run_marketing(args: argparse.Namespace) -> int:
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
    bundle = generate_marketing_bundle(brief)
    print(render_bundle(bundle))
    return 0


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
    config = EmailConfig.from_env()
    messages = fetch_recent_messages(config, limit=args.limit)
    print(_render_messages(messages))
    return 0


def _run_email_auth() -> int:
    token_store = MsalTokenStore.from_env()
    access_token, from_cache = token_store.acquire_token()
    print("Microsoft sign-in complete.")
    print(f"Token cache: {token_store.token_cache_path}")
    print(f"Access token source: {'cache' if from_cache else 'interactive Edge sign-in'}")
    print(f"Access token length: {len(access_token)}")
    return 0


def _run_email_draft(args: argparse.Namespace) -> int:
    _require_openai_api_key()
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
        print("Sent.")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        args = parser.parse_args(["marketing"])
    if args.command == "marketing":
        return _run_marketing(args)
    if args.command == "email":
        if args.email_command == "auth":
            return _run_email_auth()
        if args.email_command == "sync":
            return _run_email_sync(args)
        if args.email_command == "draft":
            return _run_email_draft(args)
        parser.error("email requires a subcommand: auth, sync, or draft")
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
