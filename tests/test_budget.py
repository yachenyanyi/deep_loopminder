from src.middlewares.execution.budget import BudgetContext, UsageConfidence, UsageRecord


def test_child_budget_can_only_narrow_parent_remaining_capacity():
    parent = BudgetContext(scope_id="parent", max_tokens=1000, consumed_tokens=250)

    oversized = parent.narrow(scope_id="child-a", requested_max_tokens=5000)
    narrower = parent.narrow(scope_id="child-b", requested_max_tokens=300)

    assert oversized.max_tokens == 750
    assert narrower.max_tokens == 300


def test_unknown_usage_fails_closed_instead_of_becoming_free_capacity():
    budget = BudgetContext(scope_id="workflow", max_tokens=1000)

    charged = budget.charge(
        UsageRecord(tokens=None, confidence=UsageConfidence.UNVERIFIABLE)
    )

    assert charged.consumed_tokens == 0
    assert charged.remaining_tokens == 0
    assert charged.exhausted is True
    assert charged.usage_unverifiable is True

    child = charged.narrow(scope_id="child", requested_max_tokens=500)
    assert child.max_tokens == 0
    assert child.remaining_tokens == 0


def test_partial_usage_charges_measured_tokens_but_fails_closed_on_unknown_remainder():
    budget = BudgetContext(scope_id="workflow", max_tokens=1000)

    charged = budget.charge(UsageRecord(tokens=120, confidence=UsageConfidence.PARTIAL))

    assert charged.consumed_tokens == 120
    assert charged.remaining_tokens == 0
    assert charged.exhausted is True
    assert charged.usage_unverifiable is True


def test_compound_workflow_charges_every_measurable_leg():
    budget = BudgetContext(scope_id="workflow", max_tokens=1000)

    charged = budget.charge_many(
        [
            UsageRecord(tokens=100),  # draft
            UsageRecord(tokens=50),  # critique
            UsageRecord(tokens=80),  # revision
            UsageRecord(tokens=20),  # retry/fallback leg
        ]
    )

    assert charged.consumed_tokens == 250
    assert charged.remaining_tokens == 750
    assert charged.usage_unverifiable is False


def test_charge_never_reports_negative_remaining_capacity():
    budget = BudgetContext(scope_id="workflow", max_tokens=100)

    charged = budget.charge(UsageRecord(tokens=140))

    assert charged.consumed_tokens == 140
    assert charged.remaining_tokens == 0
    assert charged.exhausted is True
