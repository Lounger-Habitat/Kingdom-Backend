"""NPC-authored commissions, independent of shops, trades and public affairs.

All mutations share the authoritative clock lock. A reward is removed from the
publisher's spendable wallet at publication; deliveries remain in escrow until
the entire immutable sequence finishes or is refunded.
"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import uuid

from django.db import models, transaction

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.components import BATCH_COPY_FIELDS, copy_batch_attributes, same_batch_attributes
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from .affairs import AffairError, active_stage, condition_progress, normalize_definition, project_affair, resolve_condition
from .models import (NpcProfile, VillagePersonalCommission, VillagePersonalCommissionEscrow,
                     VillagePersonalCommissionReceipt, VillagePlayerAffairLabor, VillageWorldEvent)
from .phase3_asset_service import get_available_money, get_available_slot_amount

OPEN_STATUSES = ("OPEN", "ACTIVE")
MAX_OPEN_PER_NPC = 10
MAX_ACTIVE_PER_ACTOR = 5
PLAYER_ONLY_ACTIONS = {"ACCEPT_PERSONAL_COMMISSION", "ACT_PERSONAL_COMMISSION", "ABANDON_PERSONAL_COMMISSION"}


class CommissionError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _fail(code, message):
    raise CommissionError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _fail("INVALID_PARAMS", "委托或请求标识无效。")


def _positive(value, name, maximum=1000000):
    if type(value) is not int or not 0 < value <= maximum:
        _fail("INVALID_PARAMS", f"{name}必须为 1 至 {maximum} 的整数。")
    return value


def _lock_clock(world_minute):
    if type(world_minute) is not int or world_minute < 0:
        _fail("INVALID_PARAMS", "世界时间无效。")
    GlobalGameTimeState.get_instance()
    return GlobalGameTimeState.objects.select_for_update().get(pk=1)


def _actor(actor_type, actor_id, capability=None):
    if capability in PLAYER_ONLY_ACTIONS:
        _require_player(actor_type)
    actor_id = str(actor_id)
    if actor_type == "NPC":
        profile = NpcProfile.objects.select_related("npc").filter(
            npc_id=actor_id, is_active=True, npc__is_test_only=False).first() if actor_id.isdigit() else None
        if not profile or (capability and capability not in profile.capabilities):
            _fail("COMMISSION_FORBIDDEN", "该 NPC 没有执行此委托操作的权限。")
        return profile.npc
    if actor_type == "PLAYER":
        try:
            player = GameAccount.objects.filter(pk=actor_id).first()
        except (ValueError, TypeError):
            player = None
        if player and player.is_active and not player.is_ai:
            return player
    _fail("COMMISSION_FORBIDDEN", "委托参与者身份无效。")


def _require_player(actor_type):
    if actor_type != "PLAYER":
        _fail("COMMISSION_PLAYER_ONLY", "个人委托仅限玩家认领和履约，NPC 可以发布、查询或撤回自己的委托。")


def _bag(actor_type, actor_id):
    if actor_type == "NPC":
        bag = NpcGlobalInventory.objects.select_for_update().filter(npc_id=actor_id).first()
        if not bag:
            _fail("COMMISSION_FORBIDDEN", "NPC 库存尚未初始化。")
        return bag
    player = GameAccount.objects.get(pk=actor_id)
    bag, _ = InventoryBag.objects.get_or_create(player=player, character_id=player.username)
    return InventoryBag.objects.select_for_update().get(pk=bag.pk)


def _cash(bag, amount):
    if amount < 0 and (get_available_money(bag) if isinstance(bag, NpcGlobalInventory) else bag.money) < -amount:
        _fail("INSUFFICIENT_GOLD", "可用金币不足。")
    bag.money += amount
    _save_bag(bag)


def _save_bag(bag):
    if isinstance(bag, NpcGlobalInventory):
        bag.asset_version += 1
        bag.save(update_fields=["money", "asset_version", "updated_at"])
    else:
        bag._money_change_source = "personal_commission"
        bag.save(update_fields=["money"])


def _row(commission_id):
    row = VillagePersonalCommission.objects.select_for_update().select_related("publisher").filter(pk=_uuid(commission_id)).first()
    if not row:
        _fail("COMMISSION_NOT_FOUND", "个人委托不存在。")
    return row


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _replay(actor_type, actor_id, request_id, action, payload):
    receipt = VillagePersonalCommissionReceipt.objects.filter(actor_type=actor_type, actor_id=str(actor_id),
                                                              request_id=_uuid(request_id)).first()
    fingerprint = _hash(dict(action=action, **payload))
    if receipt:
        if receipt.payload_hash != fingerprint:
            _fail("REQUEST_CONFLICT", "该请求标识已用于不同的委托操作。")
        return deepcopy(receipt.response), fingerprint
    return None, fingerprint


def _record(row, actor_type, actor_id, request_id, action, fingerprint, world_minute, *, earned_gold=0):
    data = project_commission(row, actor_type=actor_type, actor_id=actor_id)
    data["earned_gold"] = earned_gold
    VillagePersonalCommissionReceipt.objects.create(commission=row, actor_type=actor_type, actor_id=str(actor_id),
        request_id=_uuid(request_id), action=action, payload_hash=fingerprint, response=data, world_minute=world_minute)
    return data


def _event(row, action, request_id, world_minute):
    labels = {"PUBLISH": "发布了个人委托", "ACCEPT": "个人委托已被认领", "COMPLETED": "个人委托已完成",
              "WITHDRAWN": "撤回了个人委托", "ABANDONED": "个人委托已放弃", "EXPIRED": "个人委托已到期"}
    clock = GlobalGameTimeState.objects.get(pk=1)
    data = project_commission(row)
    event, created = VillageWorldEvent.objects.get_or_create(dedupe_key=f"commission:{row.pk}:{action}", defaults=dict(
        request_id=request_id, event_type="PERSONAL_COMMISSION_" + action, world_minute=world_minute,
        time_revision=clock.time_revision, participant_npc_ids=data["affected_npc_ids"],
        player_ids=[row.actor_id] if row.actor_type == "PLAYER" else [],
        location_code=(row.locations or [""])[0], visibility="PUBLIC",
        player_summary=f"{row.publisher.name}：{labels[action]}「{row.title}」", hub_summary=f"{labels[action]}：{row.title}",
        public_data={"commission_id": str(row.pk), "publisher_npc_id": row.publisher.agent_id,
                     "title": row.title, "status": row.status, "reward_gold": row.reward_gold}))
    if created:
        from .phase4_visibility import enqueue_event_deliveries
        enqueue_event_deliveries(event)


def _definition(params, npc):
    if not isinstance(params, dict) or set(params) - {"title", "description", "reward_gold", "valid_for_minutes", "default_location_code", "stages"}:
        _fail("INVALID_PARAMS", "个人委托配置字段无效。")
    title, description = params.get("title"), params.get("description", "")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        _fail("INVALID_PARAMS", "委托名称不能为空或超过 100 个字符。")
    if not isinstance(description, str) or len(description) > 2000:
        _fail("INVALID_PARAMS", "委托说明不能超过 2000 个字符。")
    reward = _positive(params.get("reward_gold"), "报酬")
    duration = _positive(params.get("valid_for_minutes"), "有效分钟", 43200)
    place = params.get("default_location_code", npc.village_profile.work_location_code)
    if not isinstance(place, str):
        _fail("INVALID_PARAMS", "委托默认地点无效。")
    stages = deepcopy(params.get("stages"))
    if not isinstance(stages, list) or not 1 <= len(stages) <= 20:
        _fail("INVALID_PARAMS", "委托需要 1 至 20 个阶段。")
    count = 0
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("conditions"), list):
            _fail("INVALID_PARAMS", "每个阶段需要配置完成条件。")
        stage.setdefault("id", "stage_" + uuid.uuid4().hex)
        for condition in stage["conditions"]:
            count += 1
            if not isinstance(condition, dict):
                _fail("INVALID_PARAMS", "完成条件无效。")
            condition.setdefault("id", "condition_" + uuid.uuid4().hex)
            _positive(condition.get("target"), "条件目标")
            if condition.get("type") == "labor":
                _positive(condition.get("unit_minutes", 60), "每份劳动分钟", 1440)
    if count > 100:
        _fail("INVALID_PARAMS", "委托最多包含 100 个完成条件。")
    try:
        requirements = normalize_definition({"stages": stages}, [place] if place else [])
    except AffairError as exc:
        raise CommissionError(exc.code, exc.message) from exc
    return title.strip(), description.strip(), reward, duration, [place] if place else [], requirements


@transaction.atomic
def publish_commission(npc, params, request_id, world_minute):
    _lock_clock(world_minute)
    _actor("NPC", npc.pk, "PUBLISH_PERSONAL_COMMISSION")
    replay, fingerprint = _replay("NPC", npc.pk, request_id, "PUBLISH", {"params": params})
    if replay is not None:
        return replay
    title, description, reward, duration, locations, requirements = _definition(params, npc)
    if VillagePersonalCommission.objects.filter(publisher=npc, status__in=OPEN_STATUSES).count() >= MAX_OPEN_PER_NPC:
        _fail("COMMISSION_LIMIT", "同时开放的个人委托已达上限。")
    bag = _bag("NPC", npc.pk)
    _cash(bag, -reward)
    row = VillagePersonalCommission.objects.create(publisher=npc, title=title, description=description,
        requirements=requirements, locations=locations, progress={"conditions": {}}, reward_gold=reward,
        reserved_reward_gold=reward, opened_world_minute=world_minute, expires_world_minute=world_minute + duration,
        created_request_id=_uuid(request_id))
    _event(row, "PUBLISH", request_id, world_minute)
    return _record(row, "NPC", npc.pk, request_id, "PUBLISH", fingerprint, world_minute)


def _assert_live(row, world_minute):
    if row.status not in OPEN_STATUSES or row.expires_world_minute <= world_minute:
        _fail("COMMISSION_CLOSED", "该委托已关闭或到期。")


@transaction.atomic
def accept_commission(commission_id, actor_type, actor_id, request_id, world_minute):
    _lock_clock(world_minute)
    _actor(actor_type, actor_id, "ACCEPT_PERSONAL_COMMISSION")
    replay, fingerprint = _replay(actor_type, actor_id, request_id, "ACCEPT", {"commission_id": str(commission_id)})
    if replay is not None:
        return replay
    row = _row(commission_id)
    _assert_live(row, world_minute)
    if row.status != "OPEN":
        _fail("COMMISSION_ALREADY_ACCEPTED", "该委托已被其他人认领。")
    if actor_type == "NPC" and str(actor_id) == str(row.publisher_id):
        _fail("COMMISSION_SELF_ACCEPT", "不能认领自己发布的委托。")
    if VillagePersonalCommission.objects.filter(actor_type=actor_type, actor_id=str(actor_id), status="ACTIVE").count() >= MAX_ACTIVE_PER_ACTOR:
        _fail("COMMISSION_LIMIT", "正在承接的个人委托已达上限。")
    _bag(actor_type, actor_id)
    row.actor_type, row.actor_id, row.status = actor_type, str(actor_id), "ACTIVE"
    row.version += 1
    row.save(update_fields=["actor_type", "actor_id", "status", "version"])
    _event(row, "ACCEPT", request_id, world_minute)
    return _record(row, actor_type, actor_id, request_id, "ACCEPT", fingerprint, world_minute)


def _assert_owner(row, actor_type, actor_id, world_minute):
    _assert_live(row, world_minute)
    if row.status != "ACTIVE" or row.actor_type != actor_type or row.actor_id != str(actor_id):
        _fail("COMMISSION_NOT_ASSIGNEE", "只有当前承接者可以完成或放弃该委托。")


def validate_commission_condition(commission_id, actor_type, actor_id, condition_id, world_minute, *, location="", kind=None):
    """Read-only validation. Mutating callers hold the clock and row locks."""
    _require_player(actor_type)
    row = VillagePersonalCommission.objects.select_related("publisher").filter(pk=_uuid(commission_id)).first()
    if not row:
        _fail("COMMISSION_NOT_FOUND", "个人委托不存在。")
    _assert_owner(row, actor_type, actor_id, world_minute)
    try:
        condition = resolve_condition(row, condition_id=condition_id, kind=kind)
    except AffairError as exc:
        raise CommissionError(exc.code, exc.message) from exc
    if condition["locations"] and location not in condition["locations"]:
        _fail("COMMISSION_LOCATION_INVALID", "请到当前阶段指定的地点完成委托。")
    return row, condition


def _take_items(row, condition, bag, quantity, item_id, instance_id, world_minute):
    if type(item_id) is not int or item_id not in condition["item_ids"]:
        _fail("INVALID_MATERIAL", "该条件不接受这种物品。")
    slot_model = NpcGlobalInventorySlot if isinstance(bag, NpcGlobalInventory) else InventorySlot
    slots = slot_model.objects.select_for_update().filter(bag=bag, item_id=item_id, item_amount__gt=0)
    if instance_id is not None:
        slots = slots.filter(instance_id=_uuid(instance_id))
    usable = []
    for slot in slots.order_by(models.F("expires_world_minute").asc(nulls_last=True), "slot_index"):
        if slot.freshness_unknown or slot.quality < condition["min_quality"]:
            continue
        expires = slot.expires_world_minute
        if expires is None and (condition["requires_freshness"] or condition["min_remaining_minutes"] > 0):
            continue
        if expires is not None and (expires <= world_minute or expires < world_minute + condition["min_remaining_minutes"]):
            continue
        available = get_available_slot_amount(slot, world_minute) if isinstance(bag, NpcGlobalInventory) else slot.item_amount
        if available:
            usable.append((slot, available))
    if sum(amount for _, amount in usable) < quantity:
        _fail("INSUFFICIENT_QUALIFIED_ITEMS", "符合数量、品质和保鲜要求的可用物品不足。")
    for slot, available in usable:
        amount = min(quantity, available)
        snapshot = {field: deepcopy(getattr(slot, field)) for field in BATCH_COPY_FIELDS}
        if snapshot["source_request_id"] is not None:
            snapshot["source_request_id"] = str(snapshot["source_request_id"])
        VillagePersonalCommissionEscrow.objects.create(commission=row, condition_id=condition["id"], item_id=item_id,
            quantity=amount, source_instance_id=slot.instance_id, batch_snapshot=snapshot)
        slot.item_amount -= amount
        slot.save(update_fields=["item_amount"])
        quantity -= amount
        if not quantity:
            break
    _save_bag(bag)


def _release_items(row, bag, status):
    slot_model = NpcGlobalInventorySlot if isinstance(bag, NpcGlobalInventory) else InventorySlot
    for batch in row.escrow_batches.select_for_update().filter(status="HELD").order_by("pk"):
        snapshot = deepcopy(batch.batch_snapshot)
        if snapshot.get("source_request_id"):
            snapshot["source_request_id"] = uuid.UUID(snapshot["source_request_id"])
        source = SimpleNamespace(**snapshot)
        slot = None
        if status == "REFUNDED":
            slot = slot_model.objects.select_for_update().filter(bag=bag, instance_id=batch.source_instance_id,
                                                                item_id=batch.item_id).first()
            if slot and not same_batch_attributes(slot, source):
                slot = None
        if slot is None:
            maximum = slot_model.objects.filter(bag=bag).aggregate(index=models.Max("slot_index"))["index"]
            slot = slot_model(bag=bag, slot_index=(maximum + 1 if maximum is not None else 0), item_id=batch.item_id,
                              item_amount=0, instance_id=uuid.uuid4())
            copy_batch_attributes(source, slot)
        slot.item_amount += batch.quantity
        slot.save()
        batch.status = status
        batch.save(update_fields=["status"])
    _save_bag(bag)


def _stop_labor(row, reason):
    VillagePlayerAffairLabor.objects.filter(commission=row, status="ACTIVE").update(
        status="COMPLETED" if reason == "COMPLETED" else "CANCELLED", stop_reason="COMMISSION_" + reason,
        accumulated_minutes=0)


def _advance(row, condition, quantity, request_id, world_minute):
    progress = condition_progress(row)
    progress[condition["id"]] = progress.get(condition["id"], 0) + quantity
    row.progress = {"conditions": progress}
    row.version += 1
    earned = 0
    if active_stage(row) is None:
        publisher = _bag("NPC", row.publisher_id)
        assignee = _bag(row.actor_type, row.actor_id)
        _release_items(row, publisher, "DELIVERED")
        if row.escrow_gold:
            _cash(publisher, row.escrow_gold)
        earned = row.reserved_reward_gold
        _cash(assignee, earned)
        row.escrow_gold, row.reserved_reward_gold, row.status = 0, 0, "COMPLETED"
        row.completed_world_minute = world_minute
        _stop_labor(row, "COMPLETED")
    row.save(update_fields=["progress", "version", "escrow_gold", "reserved_reward_gold", "status", "completed_world_minute"])
    if row.status == "COMPLETED":
        _event(row, "COMPLETED", request_id, world_minute)
    return earned


@transaction.atomic
def contribute_commission(commission_id, actor_type, actor_id, condition_id, quantity, request_id, world_minute,
                          *, item_id=None, instance_id=None, location=""):
    _lock_clock(world_minute)
    _actor(actor_type, actor_id, "ACT_PERSONAL_COMMISSION")
    payload = dict(commission_id=str(commission_id), condition_id=condition_id, quantity=quantity,
                   item_id=item_id, instance_id=str(instance_id) if instance_id is not None else None, location=location)
    replay, fingerprint = _replay(actor_type, actor_id, request_id, "CONTRIBUTE", payload)
    if replay is not None:
        return replay
    _positive(quantity, "交付数量")
    row = _row(commission_id)
    _, condition = validate_commission_condition(row.pk, actor_type, actor_id, condition_id, world_minute, location=location)
    if condition["type"] not in ("item", "gold"):
        _fail("COMMISSION_LABOR_REQUIRES_TIME", "劳动必须通过可信现场计时完成。")
    remaining = condition["target"] - condition_progress(row).get(condition["id"], 0)
    if quantity > remaining:
        _fail("PROJECT_NEED_FILLED", "交付数量超过当前条件的剩余需求。")
    bag = _bag(actor_type, actor_id)
    if condition["type"] == "gold":
        if item_id is not None or instance_id is not None:
            _fail("INVALID_PARAMS", "金币交付不接受物品参数。")
        _cash(bag, -quantity)
        row.escrow_gold += quantity
    else:
        _take_items(row, condition, bag, quantity, item_id, instance_id, world_minute)
    earned = _advance(row, condition, quantity, request_id, world_minute)
    return _record(row, actor_type, actor_id, request_id, "CONTRIBUTE", fingerprint, world_minute, earned_gold=earned)


@transaction.atomic
def complete_commission_labor(commission_id, actor_type, actor_id, condition_id, request_id, world_minute, *, location=""):
    """Only trusted elapsed-time settlement invokes this; one call credits one unit."""
    _lock_clock(world_minute)
    _actor(actor_type, actor_id, "ACT_PERSONAL_COMMISSION")
    replay, fingerprint = _replay(actor_type, actor_id, request_id, "LABOR", dict(
        commission_id=str(commission_id), condition_id=condition_id, location=location))
    if replay is not None:
        return replay
    row = _row(commission_id)
    _, condition = validate_commission_condition(row.pk, actor_type, actor_id, condition_id, world_minute, location=location, kind="labor")
    earned = _advance(row, condition, 1, request_id, world_minute)
    return _record(row, actor_type, actor_id, request_id, "LABOR", fingerprint, world_minute, earned_gold=earned)


def _refund(row, status, request_id, world_minute):
    publisher = _bag("NPC", row.publisher_id)
    if row.actor_id:
        assignee = _bag(row.actor_type, row.actor_id)
        _release_items(row, assignee, "REFUNDED")
        if row.escrow_gold:
            _cash(assignee, row.escrow_gold)
    _cash(publisher, row.reserved_reward_gold)
    row.status, row.reserved_reward_gold, row.escrow_gold = status, 0, 0
    row.completed_world_minute = world_minute
    row.version += 1
    row.save(update_fields=["status", "reserved_reward_gold", "escrow_gold", "completed_world_minute", "version"])
    _stop_labor(row, status)
    _event(row, status, request_id, world_minute)


@transaction.atomic
def withdraw_commission(commission_id, actor_type, actor_id, request_id, world_minute):
    _lock_clock(world_minute)
    _actor(actor_type, actor_id, "WITHDRAW_PERSONAL_COMMISSION")
    replay, fingerprint = _replay(actor_type, actor_id, request_id, "WITHDRAW", {"commission_id": str(commission_id)})
    if replay is not None:
        return replay
    row = _row(commission_id)
    _assert_live(row, world_minute)
    if actor_type != "NPC" or str(row.publisher_id) != str(actor_id):
        _fail("COMMISSION_NOT_PUBLISHER", "只有发布者可以撤回委托。")
    if row.status != "OPEN":
        _fail("COMMISSION_ALREADY_ACCEPTED", "已认领的委托不能由发布者单方面撤回。")
    _refund(row, "WITHDRAWN", _uuid(request_id), world_minute)
    return _record(row, actor_type, actor_id, request_id, "WITHDRAW", fingerprint, world_minute)


@transaction.atomic
def abandon_commission(commission_id, actor_type, actor_id, request_id, world_minute):
    _lock_clock(world_minute)
    _actor(actor_type, actor_id, "ABANDON_PERSONAL_COMMISSION")
    replay, fingerprint = _replay(actor_type, actor_id, request_id, "ABANDON", {"commission_id": str(commission_id)})
    if replay is not None:
        return replay
    row = _row(commission_id)
    _assert_owner(row, actor_type, actor_id, world_minute)
    _refund(row, "ABANDONED", _uuid(request_id), world_minute)
    return _record(row, actor_type, actor_id, request_id, "ABANDON", fingerprint, world_minute)


@transaction.atomic
def expire_commissions(world_minute):
    _lock_clock(world_minute)
    expired = []
    for row in VillagePersonalCommission.objects.select_for_update().select_related("publisher").filter(
            status__in=OPEN_STATUSES, expires_world_minute__lte=world_minute).order_by("commission_id"):
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"personal-commission:{row.pk}:expired")
        _refund(row, "EXPIRED", request_id, world_minute)
        expired.append(project_commission(row))
    return expired


def project_commission(row, *, actor_type=None, actor_id=None):
    # Pure public-affair projection is reused; no public reducer/event is called.
    proxy = SimpleNamespace(code=str(row.pk), kind="PERSONAL_COMMISSION", title=row.title,
        status=row.status, requirements=row.requirements, locations=row.locations, progress=row.progress,
        version=row.version, opened_world_minute=row.opened_world_minute, completed_world_minute=row.completed_world_minute)
    data = project_affair(proxy)
    mine = actor_type == row.actor_type and str(actor_id) == row.actor_id
    publisher = actor_type == "NPC" and str(actor_id) == str(row.publisher_id)
    affected = [row.publisher.agent_id]
    actor_name = ""
    if row.actor_type == "NPC":
        npc = NpcDefinition.objects.filter(pk=row.actor_id).first()
        if npc:
            affected.append(npc.agent_id)
            actor_name = npc.name
    elif row.actor_type == "PLAYER":
        actor_name = GameAccount.objects.filter(pk=row.actor_id).values_list("username", flat=True).first() or "玩家"
    for stage in data["stages"]:
        for condition in stage["conditions"]:
            condition["available"] = condition["available"] and row.status == "ACTIVE"
    if row.status not in OPEN_STATUSES:
        data["conditions"] = []
        data["active_stage_id"] = None
        data["current_stage_title"] = "已完成" if row.status == "COMPLETED" else "已关闭"
    data.update(commission_id=str(row.pk), status=row.status, stage=row.status, description=row.description,
        publisher_npc_id=row.publisher.agent_id, publisher_id=str(row.publisher_id), publisher_name=row.publisher.name,
        actor_type=row.actor_type, actor_id=row.actor_id, actor_name=actor_name, reward_gold=row.reward_gold,
        assignee_type=row.actor_type, assignee_id=row.actor_id,
        expires_world_minute=row.expires_world_minute, affected_npc_ids=list(dict.fromkeys(affected)),
        is_mine=mine, is_publisher=publisher, can_accept=actor_type == "PLAYER" and row.status == "OPEN",
        can_act=actor_type == "PLAYER" and row.status == "ACTIVE" and mine,
        can_withdraw=row.status == "OPEN" and publisher, can_abandon=actor_type == "PLAYER" and row.status == "ACTIVE" and mine,
        available=row.status in OPEN_STATUSES, labor_available=row.status == "ACTIVE" and data["labor_available"])
    if mine or publisher:
        data.update(reserved_reward_gold=row.reserved_reward_gold, escrow_gold=row.escrow_gold)
    return data


def list_commissions(world_minute, actor_type=None, actor_id=None):
    base = VillagePersonalCommission.objects.select_related("publisher")
    rows = list(base.filter(status="OPEN", expires_world_minute__gt=world_minute)
                .order_by("-opened_world_minute", "-commission_id")[:100])
    if actor_type in ("PLAYER", "NPC") and actor_id is not None:
        owned = models.Q(actor_type=actor_type, actor_id=str(actor_id))
        if actor_type == "NPC":
            owned |= models.Q(publisher_id=actor_id)
        # A busy public board must never hide an older accepted promise.
        rows.extend(base.filter(owned, status__in=OPEN_STATUSES).order_by("-opened_world_minute", "-commission_id"))
        rows.extend(base.filter(owned).exclude(status__in=OPEN_STATUSES)
                    .order_by("-completed_world_minute", "-commission_id")[:30])
    unique = {row.pk: row for row in rows}
    return [project_commission(row, actor_type=actor_type, actor_id=actor_id) for row in unique.values()]
