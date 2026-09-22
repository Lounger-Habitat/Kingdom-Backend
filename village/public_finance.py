"""Public account reconciliation and the external dish reviewer's fixed daily wage."""

import uuid

from django.db.models import F

from .models import DailyPriceSnapshot, EconomicLedgerEntry, MarkDailySalary, NpcRuntimeState, VillageAccount, VillageSimulationConfig, VillageWorldEvent
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


def settle_mark_daily_salary(account, day_index, world_minute, request_id):
    """Run inside day-close's transaction and public-account lock, once per closed day."""
    from inventory.models import NpcGlobalInventory
    from .phase4_visibility import enqueue_event_deliveries

    config = VillageSimulationConfig.objects.filter(config_key="default").first()
    salary = {"day_index": day_index, "new_amount_due": 0, "paid": 0, "paid_days": [], "outstanding": 0}
    if config is None or config.mark_employed_from_minute is None:
        return salary
    start = config.mark_employed_from_minute
    if world_minute < start or day_index < start // 1440:
        return salary

    # Policy is part of the unique key: a legacy record on the hiring date must
    # neither block this salary nor become eligible for repayment.
    entitlement, created = MarkDailySalary.objects.get_or_create(
        day_index=day_index, policy="EXTERNAL_REVIEWER",
        defaults={"amount_due": config.mark_daily_salary,
                  "reference": {"policy": "FIXED_DAILY", "employed_from_minute": start},
                  "earned_request_id": uuid.uuid5(uuid.NAMESPACE_URL, f"mark-external-salary:{day_index}"),
                  "earned_world_minute": world_minute})
    salary["new_amount_due"] = entitlement.amount_due if created else 0
    salary["amount_due_today"] = entitlement.amount_due
    mark = NpcGlobalInventory.objects.select_for_update().filter(
        npc__agent_id="10013", npc__is_test_only=False).first()
    rows = MarkDailySalary.objects.select_for_update().filter(
        policy="EXTERNAL_REVIEWER", day_index__lte=day_index,
        amount_paid__lt=F("amount_due")).order_by("day_index")
    for row in rows:
        remaining = row.amount_due - row.amount_paid
        if mark is None or remaining > account.balance - salary["paid"]:
            break
        salary["paid"] += remaining
        salary["paid_days"].append(row.day_index)
        row.amount_paid = row.amount_due
        row.paid_world_minute = world_minute
        row.save(update_fields=["amount_paid", "paid_world_minute"])
    if salary["paid"]:
        account.balance -= salary["paid"]
        account.asset_version += 1
        account.save(update_fields=["balance", "asset_version", "updated_at"])
        mark.money += salary["paid"]
        mark.asset_version += 1
        mark.save(update_fields=["money", "asset_version", "updated_at"])
    salary["outstanding"] = sum(row.amount_due - row.amount_paid for row in
        MarkDailySalary.objects.filter(policy="EXTERNAL_REVIEWER", amount_paid__lt=F("amount_due")))
    if mark is not None:
        salary["personal_balance"] = mark.money
        summary = (f"外聘品鉴师工资结算：当日应付{entitlement.amount_due}金币，"
                   f"本次到账{salary['paid']}金币，新制累计欠薪{salary['outstanding']}金币。旧制欠薪不补发。")
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"mark-daily-salary:{day_index}", event_type="MARK_DAILY_SALARY",
            world_minute=world_minute, time_revision=world_minute, request_id=request_id,
            participant_npc_ids=["10013"], visibility="PARTICIPANTS",
            private_data={"npc_id": "10013", **salary}, effects={"salary": salary},
            player_summary="外聘品鉴师工资已结算。", hub_summary=summary)
        enqueue_event_deliveries(event)
    return salary



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
    if not changes.get("affair_id") and changes.get("action_code") in {"DONATE_REPAIR_RESOURCES", "PURCHASE_REPAIR_MATERIALS"}:
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
    salary = MarkDailySalary.objects.filter(day_index=world_minute // 1440, policy="EXTERNAL_REVIEWER").first()
    return {"reconciliation": reconcile(account) if account else None, "public_fund_entries": rows,
            "public_fund_entries_scope": "RECENT_30_PUBLIC_ENTRIES_WITHIN_LAST_200_LEDGER_ROWS",
            "salary_policy": "FIXED_DAILY_EXTERNAL_REVIEWER",
            "current_meal_reference": meal_reference(world_minute),
            "salary_today": None if salary is None else {"amount_due": salary.amount_due, "amount_paid": salary.amount_paid, "reference": salary.reference},
            "salary_outstanding": sum(r.amount_due-r.amount_paid for r in MarkDailySalary.objects.filter(policy="EXTERNAL_REVIEWER", amount_paid__lt=F("amount_due")))}
