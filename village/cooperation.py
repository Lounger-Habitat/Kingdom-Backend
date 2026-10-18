"""Small player/NPC cooperation, sourced only from real completed commissions."""
from django.db import models

from account.models import GameAccount
from inventory.models import NpcDefinition
from player.models import PlayerGrowthState
from .models import (VillageAffair, VillageAffairContribution, VillageCaravanVisit,
    VillageDailyEvent, VillagePlayerNpcRelationship, VillagePlayerNpcRelationshipChange,
    VillagePersonalCommission, VillageStoryFact)

FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")
DAILY_GAIN_CAP, COMPLETION_GAIN = 4, 2


def cooperation_score(player, npc_id):
    return VillagePlayerNpcRelationship.objects.filter(player=player, npc__agent_id=str(npc_id)).values_list("score", flat=True).first() or 0


def record_commission_completion(row, minute):
    """Called inside the existing successful commission transaction, never on reads."""
    if (row.status != "COMPLETED" or row.actor_type != "PLAYER"
            or str(row.publisher.agent_id) not in FORMAL_NPC_IDS):
        return None
    prior = VillagePlayerNpcRelationshipChange.objects.filter(commission=row).first()
    if prior:
        return prior
    player = GameAccount.objects.filter(pk=row.actor_id, is_active=True, is_ai=False).first()
    if player is None:
        return None
    relation, _ = VillagePlayerNpcRelationship.objects.select_for_update().get_or_create(player=player, npc=row.publisher)
    day = minute // 1440
    used = relation.changes.filter(world_day=day).aggregate(n=models.Sum("applied_delta"))["n"] or 0
    before = relation.score
    delta = min(COMPLETION_GAIN, max(0, DAILY_GAIN_CAP - used), 100 - before)
    change = VillagePlayerNpcRelationshipChange.objects.create(relationship=relation, commission=row,
        before_score=before, after_score=before + delta, applied_delta=delta, world_minute=minute, world_day=day)
    relation.score += delta
    relation.revision += 1
    relation.last_changed_world_minute = minute
    relation.save(update_fields=["score", "revision", "last_changed_world_minute"])
    from player.progression import ensure_growth
    growth = ensure_growth(player)
    growth.revision += 1
    growth.save(update_fields=["revision"])
    return change


def post_bridge_recap(player, minute):
    """Permanent world facts plus current opportunities; no old rewards or seeded assets."""
    from .caravan import projection
    from .daily_events import project_daily_event
    bridge = VillageAffair.objects.filter(pk="BRIDGE_REPAIR").first()
    fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
    completed = bool(bridge and bridge.status == "COMPLETED" or fact and fact.bridge_condition == "REPAIRED")
    finished = bridge.completed_world_minute if bridge and bridge.status == "COMPLETED" else None
    rows = VillageAffairContribution.objects.filter(affair_id="BRIDGE_REPAIR")
    totals = list(rows.values("kind", "item_id").annotate(quantity=models.Sum("quantity")).order_by("kind", "item_id"))
    mine = list(rows.filter(actor_type="PLAYER", actor_id=str(player.pk)).values("kind", "item_id")
                .annotate(quantity=models.Sum("quantity")).order_by("kind", "item_id"))
    recent = list(rows.order_by("-world_minute", "-pk").values("actor_type", "actor_id", "kind", "item_id", "quantity", "world_minute")[:20])
    from items.models import Item
    names = dict(Item.objects.filter(pk__in={r["item_id"] for r in [*totals, *mine, *recent] if r["item_id"]}).values_list("pk", "item_name"))
    for row in [*totals, *mine, *recent]:
        row["item_name"] = names.get(row["item_id"], "")
    first = VillageCaravanVisit.objects.filter(route="NORMAL", created_world_minute__gte=finished).order_by("day_index").first() if finished is not None else None
    caravan = projection(minute)
    current_affairs = list(VillageAffair.objects.filter(status__in=("ACTIVE", "PREPARING"))
                          .values("code", "title", "status", "version"))
    recaps = VillageDailyEvent.objects.filter(affair_id="BRIDGE_REPAIR").order_by("-opened_world_minute")[:10]
    return dict(observed_world_minute=minute,
        bridge=dict(completed=completed, completed_world_minute=finished,
                    completion_effect="旧桥通行与后续正常商路已恢复。" if completed else "修桥完成后恢复旧桥通行与后续正常商路。"),
        contributions=dict(totals=totals, mine=mine, recent=recent),
        first_normal_visit=dict(day_index=first.pk, created_world_minute=first.created_world_minute) if first else None,
        current_caravan=caravan, active_affairs=current_affairs,
        recaps=[project_daily_event(row) for row in recaps],
        can_prepare_professional_contracts=bool(caravan["present"] and caravan["route"] == "NORMAL"),
        old_rewards_claimable=False)


def cooperation_overview(player, minute, read_only=False):
    relations = {str(row.npc.agent_id): row for row in VillagePlayerNpcRelationship.objects.filter(player=player).select_related("npc")}
    npcs = []
    for npc in NpcDefinition.objects.filter(agent_id__in=FORMAL_NPC_IDS, is_test_only=False).order_by("agent_id"):
        relation = relations.get(str(npc.agent_id))
        score = relation.score if relation else 0
        changes = relation.changes.select_related("commission").order_by("-world_minute", "-pk") if relation else []
        history = [dict(commission_id=str(change.commission_id), title=change.commission.title,
            world_minute=change.world_minute, applied_delta=change.applied_delta,
            before_score=change.before_score, after_score=change.after_score) for change in changes[:20]]
        total = changes.count() if relation else 0
        used = relation.changes.filter(world_day=minute // 1440).aggregate(n=models.Sum("applied_delta"))["n"] or 0 if relation else 0
        npcs.append(dict(npc_id=str(npc.agent_id), npc_name=npc.name, score=score,
            label="稳定合作" if score >= 4 else "初次合作" if score else "尚无履约合作",
            completed_count=total, changes=history, daily_gain_remaining=max(0, DAILY_GAIN_CAP - used),
            cooperation_available=score >= 4 if str(npc.agent_id) == "10015" else False,
            cooperation_hint="4分开放Nora整备专业合同；首种鲜供合同只需经营7级。" if str(npc.agent_id) == "10015"
                else "记录真实履约；不提供未实现的折扣、评分或经验加成。"))
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, npcs=npcs, daily_gain_cap=DAILY_GAIN_CAP, gain_per_commission=COMPLETION_GAIN,
        score_limit=100, post_bridge_recap=post_bridge_recap(player, minute))


def npc_cooperation_history(npc):
    """NPC-query adapter: only actual cooperation with this NPC, no bag or other-NPC records."""
    return [dict(player_id=str(row.player_id), player_name=row.player.username, score=row.score,
        completed_count=row.changes.count(), last_changed_world_minute=row.last_changed_world_minute)
        for row in VillagePlayerNpcRelationship.objects.filter(npc=npc).select_related("player")
        .order_by("-last_changed_world_minute")[:20]]
