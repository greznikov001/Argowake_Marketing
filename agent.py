from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, Runner


SITE_CONTEXT = (
    "Argowake is a Campbell-based fractional IT leadership practice for small and mid-sized professional firms. "
    "The public site emphasizes cost optimization, cybersecurity and compliance oversight, vendor and project management, "
    "workflow automation, and practical AI guidance. It also highlights support for Microsoft 365 and Google Workspace security "
    "and migrations, systems stability, and plain-language guidance for 10-50 employee businesses."
)


@dataclass(frozen=True)
class MarketingBrief:
    service_name: str
    description: str
    audience: str
    objective: str
    channels: str
    voice: str
    constraints: str

    def as_prompt(self) -> str:
        return (
            f"Service name: {self.service_name}\n"
            f"Description: {self.description}\n"
            f"Audience: {self.audience}\n"
            f"Objective: {self.objective}\n"
            f"Channels: {self.channels}\n"
            f"Voice: {self.voice}\n"
            f"Constraints: {self.constraints}"
        )


def _agent(name: str, instructions: str) -> Agent:
    return Agent(
        name=name,
        model="gpt-4.1-mini",
        instructions=instructions,
    )


strategy_agent = _agent(
    "Marketing Strategy Agent",
    (
        "You are a senior marketing strategist for a B2B service business. "
        "Turn the brief into a positioning memo with: target segment, core pain, "
        "value proposition, differentiators, offer framing, proof points to collect, "
        "top objections, and a 2-week campaign angle. Be concrete and avoid generic advice."
    ),
)

copy_agent = _agent(
    "Copywriting Agent",
    (
        "You write concise conversion copy for landing pages, ads, emails, and social posts. "
        "Use the strategy memo and produce channel-specific copy variants. Keep claims tight, "
        "specific, and easy to test. Write in the requested voice."
    ),
)

outreach_agent = _agent(
    "Outreach Agent",
    (
        "You create outbound sequences for warm and cold prospects. "
        "Draft a short email sequence, a LinkedIn DM sequence, and a follow-up cadence. "
        "Personalization should be lightweight and based on observable facts only."
    ),
)

qa_agent = _agent(
    "Brand QA Agent",
    (
        "You review marketing copy for clarity, credibility, and compliance risk. "
        "Flag vague language, unsupported claims, and tone mismatches. "
        "Then provide a tightened final version of the key messaging."
    ),
)


def run_agent(agent: Agent, prompt: str) -> str:
    result = Runner.run_sync(agent, prompt)
    return result.final_output


def generate_marketing_bundle(brief: MarketingBrief) -> dict[str, str]:
    brief_prompt = brief.as_prompt()

    strategy = run_agent(
        strategy_agent,
        "Create the strategy memo for this brief:\n\n" + brief_prompt,
    )
    copy = run_agent(
        copy_agent,
        "Use this strategy memo and brief to draft channel copy.\n\n"
        f"BRIEF:\n{brief_prompt}\n\nSTRATEGY MEMO:\n{strategy}",
    )
    outreach = run_agent(
        outreach_agent,
        "Use this strategy memo and brief to draft outreach sequences.\n\n"
        f"BRIEF:\n{brief_prompt}\n\nSTRATEGY MEMO:\n{strategy}",
    )
    qa = run_agent(
        qa_agent,
        "Review the strategy, copy, and outreach for brand fit and credibility. "
        "Return a concise audit plus any corrected wording.\n\n"
        f"BRIEF:\n{brief_prompt}\n\nSTRATEGY MEMO:\n{strategy}\n\nCOPY:\n{copy}\n\nOUTREACH:\n{outreach}",
    )

    return {
        "strategy": strategy,
        "copy": copy,
        "outreach": outreach,
        "qa": qa,
    }


def render_bundle(bundle: dict[str, str]) -> str:
    sections: list[str] = []
    for title in ("strategy", "copy", "outreach", "qa"):
        sections.append(f"## {title.title()}\n\n{bundle[title]}")
    return "\n\n".join(sections)
