"""Public account reconciliation and Mark's earned daily wage."""

from django.db.models import F

from .models import DailyPriceSnapshot, EconomicLedgerEntry, MarkDailySalary, NpcRuntimeState, VillageAccount
from .phase3_catalog import BASE_PRICES, RECIPE_WHITELIST


def mark_unavailable(world_minute):
    runtime = NpcRuntimeState.objects.select_for_update().filter(npc__agent_id="10013").first()
    if runtime is None:
        return "STORY_FORBIDDEN_ACTOR"
    if runtime.action_status == "AWAY" or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > world_minute):
        return "NPC_AWAY"
    if runtime.health_fp <= 300 or runtime.action_status == "SLEEPING":
        return "MARK_UNAVAILABLE"
    return None


def meal_reference(world_minute):
    """Free emergency rations are not the reference for a paid daily meal."""
    from recipes.models import Recipe
    from .lucy_catalog import RELIEF_PACK, MEALS
    ids = list(Recipe.objects.filter(pk__in=RECIPE_WHITELIST).exclude(item_id=RELIEF_PACK).values_list("item_id", flat=True))
    if not ids:
        ids = [1053, 1067]
    snapshot = DailyPriceSnapshot.objects.filter(
        status="PUBLISHED", day_index__lte=world_minute // 1440,
    ).order_by("-day_index").first()
    prices = (snapshot.prices or {}) if snapshot else {}
    candidates = []
    for item_id in ids:
        value = prices.get(str(item_id), BASE_PRICES.get(item_id, MEALS.get(item_id, (None, None))[1]))
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            candidates.append((value, item_id, "PUBLISHED" if str(item_id) in prices else "BASE_PRICE"))
    if not candidates:
        return None
    price, item_id, source = min(candidates)
    return {"amount": price, "item_id": item_id, "source": source,
            "price_day": snapshot.day_index if source == "PUBLISHED" else None}


def pay_daily_salary(account, mark_inv, changes, world_minute, request_id, *, earn=True):
    day = world_minute // 1440
    entitlement = MarkDailySalary.objects.select_for_update().filter(day_index=day).first()
    already_earned = entitlement is not None
    if earn and entitlement is None:
        reference = meal_reference(world_minute)
        if reference:
            entitlement = MarkDailySalary.objects.create(
                day_index=day, amount_due=reference["amount"], reference=reference,
                earned_request_id=request_id, earned_world_minute=world_minute,
            )
    paid = 0
    paid_days = []
    # All callers hold the public account lock; settle oldest debt first, in
    # whole daily amounts. No world timer creates or pays entitlements.
    for row in MarkDailySalary.objects.select_for_update().filter(
        amount_paid__lt=F("amount_due"), day_index__lte=day,
    ).order_by("day_index"):
        remaining = row.amount_due - row.amount_paid
        if remaining > account.balance - paid:
            break
        paid += remaining
        paid_days.append(row.day_index)
        row.amount_paid = row.amount_due
        row.paid_world_minute = world_minute
        row.save(update_fields=["amount_paid", "paid_world_minute"])
    if paid:
        account.balance -= paid
        account.asset_version += 1
        account.save(update_fields=["balance", "asset_version", "updated_at"])
        mark_inv.money += paid
        mark_inv.asset_version += 1
        mark_inv.save(update_fields=["money", "asset_version", "updated_at"])
    outstanding = sum(row.amount_due - row.amount_paid for row in MarkDailySalary.objects.filter(amount_paid__lt=F("amount_due")))
    # Receipt facts only: the entitlement and payment rules above are unchanged.
    paid_for_today = MarkDailySalary.objects.filter(day_index=day).values_list("amount_paid", flat=True).first() or 0
    changes.update({"mark_salary": paid, "salary_skipped": paid == 0,
                    "salary": {"day_index": day, "already_earned_today": already_earned,
                               "reference": entitlement.reference if entitlement else None,
                               "amount_due_today": entitlement.amount_due if entitlement else 0,
                               "new_amount_due": entitlement.amount_due if entitlement and not already_earned else 0,
                               "amount_paid_for_today": paid_for_today,
                               "paid": paid, "paid_days": paid_days, "outstanding": outstanding,
                               "personal_balance": mark_inv.money, "public_fund_balance": account.balance}})
    for key, delta in (("PUBLIC_FUND", -paid), ("10013", paid)):
        values = changes.setdefault(key, {})
        values["money"] = int(values.get("money", 0)) + delta
    return paid > 0


def public_fund_delta(entry):
    """Read canonical entries, with explicit adapters for preserved history."""
    changes = entry.changes or {}
    canonical = changes.get("PUBLIC_FUND")
    if isinstance(canonical, dict) and "money" in canonical:
        return int(canonical.get("money", 0) or 0)
    delta = -int(changes.get("mark_salary", 0) or 0)
    traveler = changes.get("traveler_sales")
    if isinstance(traveler, dict):
        delta += int(traveler.get("fee_amount", 0))
    eat = changes.get("eat") or {}
    if eat.get("source") == "COMMUNITY_MEAL":
        delta -= int(eat.get("payment", 0) or 0)
    if entry.entry_type == "EXPORT_GOODS" and not changes.get("income_account"):
        delta += int(entry.external_delta or 0)
    if changes.get("action_code") in {"DONATE_REPAIR_RESOURCES", "PURCHASE_REPAIR_MATERIALS"}:
        delta += int(changes.get("gold", 0) or 0)
    return delta


def reconcile(account):
    entries = EconomicLedgerEntry.objects.filter(id__gt=account.audit_after_ledger_id).order_by("id")
    expected = account.audit_opening_balance + sum(public_fund_delta(row) for row in entries)
    return {"expected_balance": expected, "actual_balance": account.balance,
            "difference": account.balance - expected, "balance_matches": expected == account.balance,
            "opening_balance": account.audit_opening_balance,
            "after_ledger_id": account.audit_after_ledger_id,
            "scope": "SINCE_MIGRATION" if account.audit_baseline_kind == "MIGRATION" else "SINCE_INITIAL_BALANCE",
            "historical_verified": account.audit_baseline_kind != "MIGRATION"}


def public_finance_query(world_minute):
    account = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    rows = []
    for entry in EconomicLedgerEntry.objects.order_by("-id")[:200]:
        delta = public_fund_delta(entry)
        if delta:
            rows.append({"entry_id": entry.id, "request_id": str(entry.request_id),
                         "entry_type": entry.entry_type, "world_minute": entry.world_minute,
                         "public_fund_delta": delta, "actor_npc_id": str(entry.actor_npc.agent_id) if entry.actor_npc else None})
        if len(rows) >= 30:
            break
    salary = MarkDailySalary.objects.filter(day_index=world_minute // 1440).first()
    return {"reconciliation": reconcile(account) if account else None, "public_fund_entries": rows,
            "public_fund_entries_scope": "RECENT_30_PUBLIC_ENTRIES_WITHIN_LAST_200_LEDGER_ROWS",
            "salary_policy": "ONE_MEAL_REFERENCE_PER_WORKED_GAME_DAY",
            "current_meal_reference": meal_reference(world_minute),
            "salary_today": None if salary is None else {"amount_due": salary.amount_due, "amount_paid": salary.amount_paid, "reference": salary.reference},
            "salary_outstanding": sum(r.amount_due-r.amount_paid for r in MarkDailySalary.objects.filter(amount_paid__lt=F("amount_due")))}
