from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from agents import Agent, Runner

from agent import SITE_CONTEXT, MarketingBrief
from budget import BudgetGuard
from email_tools import EmailMessageSummary


@dataclass(frozen=True)
class MarketingDailyInput:
    brief: MarketingBrief
    inbox_messages: list[EmailMessageSummary]
    scheduled_activities: list[str]
    daily_budget_minutes: int = 60


def _agent(name: str, instructions: str) -> Agent:
    return Agent(
        name=name,
        model="gpt-4.1-mini",
        instructions=instructions,
    )


manager_agent = _agent(
    "Marketing Manager",
    (
        "You manage a small budget-first marketing team for Argowake. "
        "Create a 60-minute operating plan that allocates time, sets priorities, and delegates work "
        "to specialists. Keep the plan practical, measurable, and bounded. "
        "You must account for inbox replies and scheduled activities. "
        "Return a concise operating plan with: priorities, time budget, delegated tasks, success criteria, "
        "and what to defer until tomorrow."
    ),
)

research_seo_agent = _agent(
    "Research and SEO Specialist",
    (
        "You improve discovery and targeting for a B2B local marketing service. "
        "Use the provided brief, inbox context, and manager plan to produce: "
        "SEO observations, keyword themes, audience or segment insights, prospecting angles, "
        "and 3 practical actions that can be completed in a small daily budget."
    ),
)

content_outreach_agent = _agent(
    "Content and Outreach Specialist",
    (
        "You produce concise marketing content and email/outreach drafts for Argowake. "
        "Use the provided brief, inbox context, and manager plan to produce: "
        "one recommended reply for the most important inbound email, one outbound follow-up, "
        "and one short content asset or social post. Keep the tone practical and credible."
    ),
)

analytics_agent = _agent(
    "Analytics Specialist",
    (
        "You review marketing performance for a budget-conscious POC. "
        "Use the provided brief, inbox context, and manager plan to produce: "
        "what to measure today, what signals matter, where the funnel is weak, and one experiment "
        "to run next. Keep it lightweight and decision-oriented."
    ),
)

final_synthesis_agent = _agent(
    "Marketing Director",
    (
        "You are the marketing director responsible for the daily operating decision. "
        "Combine the manager plan and specialist outputs into one final daily action plan. "
        "Keep the result under a one-hour execution budget and call out what should happen next, "
        "what to send today, and what to defer. Be concise and concrete."
    ),
)


def _format_inbox(messages: list[EmailMessageSummary]) -> str:
    if not messages:
        return "No inbox messages were provided."
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        lines.append(
            "\n".join(
                [
                    f"[{index}] From: {message.from_address}",
                    f"Subject: {message.subject}",
                    f"Date: {message.date}",
                    f"Snippet: {message.snippet}",
                ]
            )
        )
    return "\n\n".join(lines)


def _format_scheduled_activities(activities: list[str]) -> str:
    if not activities:
        return "No scheduled activities were provided."
    return "\n".join(f"- {activity}" for activity in activities)


def _run_agent(agent: Agent, prompt: str) -> str:
    try:
        result = Runner.run_sync(agent, prompt)
    except Exception as exc:  # pragma: no cover - surfaced as runtime error for CLI use
        message = str(exc)
        if "insufficient_quota" in message or "exceeded your current quota" in message:
            raise RuntimeError(
                "OpenAI quota is exhausted. Add billing or credits to the account, then rerun the daily workflow."
            ) from exc
        raise
    return str(result.final_output).strip()


def run_daily_marketing_team(input_data: MarketingDailyInput) -> dict[str, str]:
    budget_guard = BudgetGuard.from_env()
    budget_guard.consume(budget_guard.estimated_cost_for_calls(5), "daily marketing team run")

    brief_prompt = input_data.brief.as_prompt()
    inbox_prompt = _format_inbox(input_data.inbox_messages)
    scheduled_prompt = _format_scheduled_activities(input_data.scheduled_activities)

    manager_prompt = (
        "Create the daily operating plan for this marketing team.\n\n"
        f"DAILY BUDGET: {input_data.daily_budget_minutes} minutes\n\n"
        f"SITE CONTEXT:\n{SITE_CONTEXT}\n\n"
        f"BRIEF:\n{brief_prompt}\n\n"
        f"INBOX:\n{inbox_prompt}\n\n"
        f"SCHEDULED ACTIVITIES:\n{scheduled_prompt}"
    )
    manager_plan = _run_agent(manager_agent, manager_prompt)

    def run_research() -> str:
        return _run_agent(
            research_seo_agent,
            "Use the manager plan and the brief to produce the research/SEO work for today.\n\n"
            f"BRIEF:\n{brief_prompt}\n\n"
            f"INBOX:\n{inbox_prompt}\n\n"
            f"SCHEDULED ACTIVITIES:\n{scheduled_prompt}\n\n"
            f"MANAGER PLAN:\n{manager_plan}",
        )

    def run_content() -> str:
        return _run_agent(
            content_outreach_agent,
            "Use the manager plan, brief, and inbox to draft today's content/outreach work.\n\n"
            f"BRIEF:\n{brief_prompt}\n\n"
            f"INBOX:\n{inbox_prompt}\n\n"
            f"SCHEDULED ACTIVITIES:\n{scheduled_prompt}\n\n"
            f"MANAGER PLAN:\n{manager_plan}",
        )

    def run_analytics() -> str:
        return _run_agent(
            analytics_agent,
            "Use the manager plan and brief to produce today's analytics priorities.\n\n"
            f"BRIEF:\n{brief_prompt}\n\n"
            f"INBOX:\n{inbox_prompt}\n\n"
            f"SCHEDULED ACTIVITIES:\n{scheduled_prompt}\n\n"
            f"MANAGER PLAN:\n{manager_plan}",
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        research_future = executor.submit(run_research)
        content_future = executor.submit(run_content)
        analytics_future = executor.submit(run_analytics)
        research = research_future.result()
        content = content_future.result()
        analytics = analytics_future.result()

    synthesis_prompt = (
        "Combine the manager plan and specialist output into a final daily action plan. "
        "Keep it optimized for a 60-minute run and make the next steps explicit.\n\n"
        f"BRIEF:\n{brief_prompt}\n\n"
        f"INBOX:\n{inbox_prompt}\n\n"
        f"SCHEDULED ACTIVITIES:\n{scheduled_prompt}\n\n"
        f"MANAGER PLAN:\n{manager_plan}\n\n"
        f"RESEARCH / SEO:\n{research}\n\n"
        f"CONTENT / OUTREACH:\n{content}\n\n"
        f"ANALYTICS:\n{analytics}"
    )
    synthesis = _run_agent(final_synthesis_agent, synthesis_prompt)

    return {
        "manager_plan": manager_plan,
        "research_seo": research,
        "content_outreach": content,
        "analytics": analytics,
        "synthesis": synthesis,
    }


def render_daily_marketing_team(output: dict[str, str]) -> str:
    sections: list[str] = []
    for title, key in (
        ("Manager Plan", "manager_plan"),
        ("Research / SEO", "research_seo"),
        ("Content / Outreach", "content_outreach"),
        ("Analytics", "analytics"),
        ("Final Synthesis", "synthesis"),
    ):
        sections.append(f"## {title}\n\n{output[key]}")
    return "\n\n".join(sections)
