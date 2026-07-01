from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, Runner

from agent import SITE_CONTEXT
from email_tools import EmailMessageSummary


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    body: str
    rationale: str


email_agent = Agent(
    name="Email Response Agent",
    model="gpt-4.1-mini",
    instructions=(
        "You draft clear, concise business email replies for help@argowake.com. "
        "Read the inbound message carefully, identify the customer's request, and produce: "
        "a recommended reply subject, a reply body, and a short rationale. "
        "If the message is vague, ask a clarifying question in the reply rather than inventing facts. "
        "Keep the tone professional, practical, and consistent with a fractional IT leadership firm."
    ),
)


def draft_reply(message: EmailMessageSummary, site_context: str) -> EmailDraft:
    prompt = (
        "Draft a customer reply based on this inbound email and the site context.\n\n"
        f"SITE CONTEXT:\n{site_context}\n\n"
        f"INBOUND EMAIL:\n{message.as_prompt()}\n\n"
        "Return plain text in this exact format:\n"
        "Subject: <subject>\n"
        "Body: <reply body>\n"
        "Rationale: <brief rationale>"
    )
    result = Runner.run_sync(email_agent, prompt)
    subject = ""
    body = ""
    rationale = ""
    for line in str(result.final_output).splitlines():
        if line.startswith("Subject:"):
            subject = line.partition(":")[2].strip()
        elif line.startswith("Body:"):
            body = line.partition(":")[2].strip()
        elif line.startswith("Rationale:"):
            rationale = line.partition(":")[2].strip()
    return EmailDraft(subject=subject, body=body, rationale=rationale)


def draft_reply_with_site_context(message: EmailMessageSummary) -> EmailDraft:
    return draft_reply(message, SITE_CONTEXT)
