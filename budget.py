from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class BudgetConfig:
    monthly_budget_usd: float
    estimated_call_cost_usd: float
    ledger_path: Path

    @classmethod
    def from_env(cls) -> "BudgetConfig":
        monthly_budget_usd = float(os.getenv("ARGOWAKE_OPENAI_MONTHLY_BUDGET_USD", "5.0"))
        estimated_call_cost_usd = float(os.getenv("ARGOWAKE_OPENAI_ESTIMATED_CALL_COST_USD", "0.05"))
        ledger_path = Path(os.getenv("ARGOWAKE_OPENAI_BUDGET_LEDGER_FILE", ".state/openai_budget_ledger.json"))
        return cls(
            monthly_budget_usd=monthly_budget_usd,
            estimated_call_cost_usd=estimated_call_cost_usd,
            ledger_path=ledger_path,
        )


@dataclass
class BudgetLedger:
    month: str
    estimated_spend_usd: float

    @classmethod
    def load(cls, path: Path) -> "BudgetLedger":
        if not path.exists():
            return cls(month=_current_month(), estimated_spend_usd=0.0)
        payload = json.loads(path.read_text(encoding="utf-8"))
        month = str(payload.get("month") or _current_month())
        spend = float(payload.get("estimated_spend_usd") or 0.0)
        if month != _current_month():
            return cls(month=_current_month(), estimated_spend_usd=0.0)
        return cls(month=month, estimated_spend_usd=spend)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "month": self.month,
                    "estimated_spend_usd": round(self.estimated_spend_usd, 4),
                    "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


class BudgetGuard:
    def __init__(self, config: BudgetConfig, ledger: BudgetLedger | None = None) -> None:
        self.config = config
        self.ledger = ledger or BudgetLedger.load(config.ledger_path)

    @classmethod
    def from_env(cls) -> "BudgetGuard":
        return cls(BudgetConfig.from_env())

    def estimated_cost_for_calls(self, call_count: int) -> float:
        return round(call_count * self.config.estimated_call_cost_usd, 4)

    def assert_can_spend(self, estimated_cost_usd: float, label: str) -> None:
        projected = self.ledger.estimated_spend_usd + estimated_cost_usd
        if projected > self.config.monthly_budget_usd:
            remaining = max(0.0, self.config.monthly_budget_usd - self.ledger.estimated_spend_usd)
            raise RuntimeError(
                f"Monthly OpenAI budget guard blocked '{label}'. "
                f"Estimated cost ${estimated_cost_usd:.2f} would exceed the remaining "
                f"${remaining:.2f} of the ${self.config.monthly_budget_usd:.2f} cap."
            )

    def consume(self, estimated_cost_usd: float, label: str) -> None:
        self.assert_can_spend(estimated_cost_usd, label)
        self.ledger.estimated_spend_usd += estimated_cost_usd
        self.ledger.month = _current_month()
        self.ledger.save(self.config.ledger_path)

    def status(self) -> dict[str, float | str]:
        return {
            "month": self.ledger.month,
            "estimated_spend_usd": round(self.ledger.estimated_spend_usd, 4),
            "remaining_usd": round(max(0.0, self.config.monthly_budget_usd - self.ledger.estimated_spend_usd), 4),
            "monthly_budget_usd": round(self.config.monthly_budget_usd, 4),
        }


def _current_month() -> str:
    return date.today().strftime("%Y-%m")
