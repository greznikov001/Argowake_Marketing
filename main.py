from __future__ import annotations

import argparse
import os
import sys

from agent import MarketingBrief, generate_marketing_bundle, render_bundle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a marketing agent bundle for Argowake.")
    parser.add_argument("--service-name", default="Argowake", help="Service name to market.")
    parser.add_argument(
        "--description",
        default=(
            "Fractional IT leadership for small and mid-sized professional firms, focused on cost "
            "optimization, cybersecurity and compliance oversight, vendor and project management, "
            "workflow automation, and practical AI guidance."
        ),
        help="Short description of the service.",
    )
    parser.add_argument(
        "--audience",
        default="Small and mid-sized professional firms, especially 10–50 employee businesses",
        help="Primary audience.",
    )
    parser.add_argument(
        "--objective",
        default="Book free cybersecurity and IT cost reviews",
        help="Primary marketing objective.",
    )
    parser.add_argument(
        "--channels",
        default="Website, referral follow-up, email, and local outreach",
        help="Channels to focus on.",
    )
    parser.add_argument("--voice", default="Plain-language, practical, and trustworthy", help="Brand voice.")
    parser.add_argument(
        "--constraints",
        default="Avoid hype, avoid unverified claims, and emphasize cost savings, security, and business continuity.",
        help="Messaging constraints.",
    )
    return parser.parse_args()


def main() -> int:
    if not os.getenv("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY before running this agent workflow.", file=sys.stderr)
        return 2

    args = parse_args()
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


if __name__ == "__main__":
    raise SystemExit(main())
