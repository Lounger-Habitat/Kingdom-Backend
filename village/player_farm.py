"""Player leases and crops; callers hold the shared world lock before any bags.

Geometry is authenticated by Env before it sends BILL_FARM context. All state,
prices, ownership, produce and tool wear are validated here in the receipt savepoint.
"""
import uuid

from django.db.models import Max
from django.utils import timezone

from inventory.components import copy_batch_attributes
from inventory.models import InventorySlot, NpcGlobalInventory
from inventory.serializers import serialize_bag
from .models import EconomicLedgerEntry, VillageFarmPlot, VillageSimulationConfig, VillageWorldEvent
from .phase3_catalog import FARM_CROP_RULES, FARM_TOOL_ITEM_ID, farm_crop_catalog
from .phase3_asset_service import deterministic_rng

BILL_ID = "10011"
DAY = 1440
ACTIONS = {
    "lease_farm_plot": {"plot_code", "days", "expected_plot_version", "expected_daily_price"},
    "renew_farm_plot": {"plot_code", "days", "expected_plot_version", "expected_daily_price"},
    "plant_farm_plot": {"plot_code", "crop_code", "expected_plot_version"},
    "harvest_farm_plot": {"plot_code", "expected_plot_version", "expected_batch_id"},
}


def rules(config=None):
    config = config or VillageSimulationConfig.objects.filter(config_key="default").first()
    return {name: getattr(config, "farm_" + name, default) for name, default in {
        "daily_price": 10, "min_days": 1, "max_days": 30, "max_plots": 3,
        "plant_action_cost": 5, "harvest_action_cost": 5, "grace_minutes": 1440,
    }.items()}


def lease_state(plot, minute):
    if plot.lessee_player_id is None:
        return "AVAILABLE", None
    end = plot.lease_ends_world_minute
    if end is None:
        return "AVAILABLE", None
    deadline = max(end, plot.mature_world_minute or end) + plot.lease_grace_minutes
    if minute < end:
        return "ACTIVE", deadline if plot.status != "EMPTY" else None
    if plot.status != "EMPTY" and minute < deadline:
        return "HARVEST_ONLY", deadline
    return "AVAILABLE", deadline


def project_plot(plot, minute):
    state, deadline = lease_state(plot, minute)
    released = plot.lessee_player_id is not None and state == "AVAILABLE"
    owned = plot.lessee_player_id is not None and not released
    status = "EMPTY" if released else plot.status
    if status == "GROWING" and plot.mature_world_minute is not None and minute >= plot.mature_world_minute:
        status = "MATURE"
    return dict(plot_code=plot.plot_code, status=status,
        crop_code="" if released else plot.crop_code or "",
        batch_id=str(plot.batch_id) if plot.batch_id and not released else None,
        planted_world_minute=None if released else plot.planted_world_minute,
        mature_world_minute=None if released else plot.mature_world_minute,
        lease_enabled=plot.lease_enabled, operator_type="PLAYER" if owned else "NPC",
        lessee_player_id=str(plot.lessee_player_id) if owned else None,
        lessee_player_name=plot.lessee_player.username if owned else "",
        lease_state=state, leased_world_minute=plot.leased_world_minute if owned else None,
        lease_ends_world_minute=plot.lease_ends_world_minute if owned else None,
        harvest_deadline_world_minute=deadline if owned else None,
        plot_version=plot.plot_version)


def clear_crop(plot):
    plot.status, plot.crop_code = "EMPTY", ""
    plot.batch_id = plot.planted_world_minute = plot.mature_world_minute = None
    plot.yield_seed = plot.source_request_id = None


def clear_lease(plot):
    plot.lessee_player = None
    plot.leased_world_minute = plot.lease_ends_world_minute = None
    plot.lease_price_paid = 0
    plot.lease_expiry_notified = False


def _event(plot, minute, revision, kind, summary, *, player_id=None, request_id=None, public=None,
           dedupe_key=None, hub_summary=None, private=None):
    data = {"plot": project_plot(plot, minute), **(public or {})}
    event = VillageWorldEvent.objects.create(
        dedupe_key=dedupe_key or f"farm:{plot.pk}:{plot.plot_version}:{kind}", event_type=kind,
        request_id=request_id, world_minute=minute, time_revision=revision,
        location_code="BILL_FARM", participant_npc_ids=[BILL_ID],
        player_ids=[str(player_id)] if player_id else [], visibility="PUBLIC",
        player_summary=summary, hub_summary=hub_summary or summary, public_data=data,
        private_data=private or {})
    from .phase4_visibility import enqueue_event_deliveries
    enqueue_event_deliveries(event)
    return event


def maintain_leases(minute, revision=0):
    """Only under the world lock. Release once; fixed deadlines never refresh."""
    for plot in VillageFarmPlot.objects.filter(lessee_player__isnull=False,
            lease_ends_world_minute__gt=minute, lease_ends_world_minute__lte=minute+DAY):
        dedupe = f"farm-warning:{plot.pk}:{plot.leased_world_minute}:{plot.lease_ends_world_minute}"
        if not VillageWorldEvent.objects.filter(dedupe_key=dedupe).exists():
            _event(plot, minute, revision, "FARM_LEASE_EXPIRING", f"{plot.plot_code}租期不足一个游戏天，可找 Bill 续租。",
                player_id=plot.lessee_player_id, dedupe_key=dedupe)
    for plot in VillageFarmPlot.objects.select_for_update().filter(
            lessee_player__isnull=False, lease_ends_world_minute__lte=minute).order_by("plot_code"):
        state, _ = lease_state(plot, minute)
        player_id = plot.lessee_player_id
        if state == "AVAILABLE":
            clear_crop(plot)
            clear_lease(plot)
            plot.plot_version += 1
            plot.save()
            _event(plot, minute, revision, "FARM_LEASE_RELEASED", f"{plot.plot_code}租约结束，田地已归还。", player_id=player_id)
        elif not plot.lease_expiry_notified:
            plot.lease_expiry_notified = True
            plot.plot_version += 1
            plot.save(update_fields=["lease_expiry_notified", "plot_version", "updated_at"])
            _event(plot, minute, revision, "FARM_LEASE_HARVEST_ONLY", f"{plot.plot_code}租约已到期，仅保留本批收获权。", player_id=player_id)


def _usable(slot, minute):
    return not slot.freshness_unknown and (slot.expires_world_minute is None or slot.expires_world_minute > minute)


def farm_overview(player, bag, minute, *, read_only=False):
    from .farm_irrigation import project_irrigation
    irrigation = project_irrigation()
    cfg = VillageSimulationConfig.objects.filter(config_key="default").first()
    limits = rules(cfg)
    plots = [project_plot(p, minute) for p in VillageFarmPlot.objects.select_related("lessee_player").order_by("plot_code")]
    count = sum(p["lessee_player_id"] == str(player.pk) for p in plots)
    tools = list(bag.slots.filter(item_id=FARM_TOOL_ITEM_ID, item_amount__gt=0)) if bag.pk else []
    remaining = sum(s.remaining_uses * s.item_amount for s in tools
        if s.remaining_uses is not None and s.remaining_uses > 0 and _usable(s, minute))
    enabled = bool(cfg and cfg.village_economy_enabled and cfg.farm_leasing_enabled)
    for p in plots:
        mine = p["lessee_player_id"] == str(player.pk)
        p.update(is_mine=mine,
            can_lease=enabled and not read_only and p["lease_enabled"] and not p["lessee_player_id"] and p["status"] == "EMPTY" and count < limits["max_plots"],
            can_renew=enabled and not read_only and mine and (p["lease_ends_world_minute"] - minute) <= (limits["max_days"]-limits["min_days"]) * DAY,
            can_plant=not read_only and mine and p["lease_state"] == "ACTIVE" and p["status"] == "EMPTY",
            can_harvest=not read_only and mine and p["status"] == "MATURE")
        p["unavailable_reason"] = ("观察模式只可查看。" if read_only else
            "该田地由其他玩家承租。" if p["lessee_player_id"] and not mine else
            "租约已到期，仅可收获本批作物。" if mine and p["lease_state"] == "HARVEST_ONLY" else
            "田地由 Bill 经营。" if not mine and not p["lease_enabled"] else
            "Bill 的本批作物收获后开放租赁。" if not mine and p["status"] != "EMPTY" else
            "已达到个人承租上限。" if not mine and count >= limits["max_plots"] else
            "租赁暂未开放。" if not enabled and not mine else "")
    return dict(rules=limits, plots=plots, my_plot_count=count, tool_remaining_uses=remaining,
                crops=farm_crop_catalog(irrigation), irrigation=irrigation)


def _wear_tool(bag, minute):
    from .player_actions import fail
    slots = list(InventorySlot.objects.select_for_update().filter(
        bag=bag, item_id=FARM_TOOL_ITEM_ID, item_amount__gt=0).order_by("slot_index"))
    usable = [s for s in slots if s.remaining_uses is not None and s.remaining_uses > 0 and _usable(s, minute)]
    if not usable:
        if not slots:
            fail("FARM_TOOL_MISSING", "背包中没有农具。")
        if any(s.remaining_uses is None for s in slots):
            fail("TOOL_DURABILITY_UNKNOWN", "农具耐久尚未同步，不能使用。")
        fail("TOOL_BROKEN", "没有耐久可用的农具。")
    slot = min(usable, key=lambda s: (s.remaining_uses, s.slot_index))
    if slot.item_amount > 1:
        slot.item_amount -= 1
        slot.save(update_fields=["item_amount"])
        target = InventorySlot(bag=bag, slot_index=(bag.slots.aggregate(n=Max("slot_index"))["n"] or 0)+1,
            item_id=slot.item_id, item_amount=1, item_bag_name=slot.item_bag_name)
        copy_batch_attributes(slot, target)
        target.save()
        slot = target
    slot.remaining_uses -= 1
    slot.save(update_fields=["remaining_uses"])
    return dict(item_id=FARM_TOOL_ITEM_ID, instance_id=str(slot.instance_id), uses=1, remaining_uses=slot.remaining_uses)


def execute_farm_action(player, bag, action, p, rid, clock):
    from .player_actions import fail, _consume
    from player.models import PlayerState
    from player.services import recover_action_points
    minute = clock.world_minute
    cfg = VillageSimulationConfig.objects.get(config_key="default")
    limits = rules(cfg)
    code = p.get("plot_code")
    if not isinstance(code, str) or len(code) > 32:
        fail("INVALID_PARAMS", "田地编号无效。")
    plot = VillageFarmPlot.objects.select_for_update().filter(plot_code=code).first()
    if plot is None:
        fail("PLOT_NOT_FOUND", "田地尚未同步。")
    if type(p.get("expected_plot_version")) is not int or p["expected_plot_version"] != plot.plot_version:
        fail("PLOT_VERSION_CHANGED", "田地状态已变化，请刷新后重试。")
    state, _ = lease_state(plot, minute)
    result = dict(items_consumed=[], items_added=[], spent_gold=0, affected_npc_ids=[BILL_ID])
    changes = {"player_id": str(player.pk), "player_display_name": player.username, "plot_code": code}
    effect = None
    lease_notice = None
    lease_details = None
    if action in {"lease_farm_plot", "renew_farm_plot"}:
        if not cfg.farm_leasing_enabled:
            fail("FEATURE_DISABLED", "田地租赁尚未开放。")
        days = p.get("days")
        if type(days) is not int or not limits["min_days"] <= days <= limits["max_days"]:
            fail("INVALID_PARAMS", f"请输入 {limits['min_days']}–{limits['max_days']} 的整数天数。")
        if type(p.get("expected_daily_price")) is not int or p["expected_daily_price"] != limits["daily_price"]:
            fail("PRICE_CHANGED", "租金已变化，请刷新报价后确认。")
        if action == "lease_farm_plot":
            if not plot.lease_enabled or plot.lessee_player_id is not None or plot.status != "EMPTY":
                fail("PLOT_NOT_LEASABLE", "这块田地当前不可出租。")
            if VillageFarmPlot.objects.filter(lessee_player=player).count() >= limits["max_plots"]:
                fail("LEASE_LIMIT_REACHED", "已达到个人承租上限。")
            plot.lessee_player = player
            plot.leased_world_minute = minute
            plot.lease_grace_minutes = limits["grace_minutes"]
            end = minute + days * DAY
        else:
            if plot.lessee_player_id != player.pk or state == "AVAILABLE":
                fail("PLOT_NOT_OWNED", "这块田地已不属于你。")
            end = max(minute, plot.lease_ends_world_minute) + days * DAY
            if end - minute > limits["max_days"] * DAY:
                fail("LEASE_DURATION_LIMIT", "续租后剩余租期不能超过可选天数上限。")
        total = days * limits["daily_price"]
        if bag.money < total:
            fail("INSUFFICIENT_MONEY", f"金币不足，还差 {total-bag.money} 金币。")
        bill = NpcGlobalInventory.objects.select_for_update().filter(npc__agent_id=BILL_ID).first()
        if bill is None:
            fail("FARM_UNAVAILABLE", "Bill 的结算账户尚未就绪。")
        bag.money -= total
        bag._money_change_source = "farm_lease"
        bag.save(update_fields=["money"])
        bill.money += total
        bill.asset_version += 1
        bill.save(update_fields=["money", "asset_version", "updated_at"])
        from .runtime_services import _world_time_text
        previous_end = plot.lease_ends_world_minute
        lease_details = dict(player_id=str(player.pk), player_name=player.username, plot_code=code,
            days=days, daily_price=limits["daily_price"], rent_paid=total, payment_status="SETTLED",
            credited_npc_id=BILL_ID, bill_balance_after=bill.money,
            lease_period_starts_world_minute=end-days*DAY, lease_ends_world_minute=end,
            previous_lease_ends_world_minute=previous_end)
        short_code = code.replace("FARM_PLOT_", "").replace("_0", "")
        operation = "租赁" if action == "lease_farm_plot" else "续租"
        period = (f"本次租期从游戏时间{_world_time_text(end-days*DAY)}开始。" if action == "lease_farm_plot"
            else f"原到期时间为{_world_time_text(previous_end)}，"
                 + ("本次从原到期时间追加。" if state == "ACTIVE" else "已进入只收获期，本次从当前游戏时间重新起算。"))
        lease_notice = (f"玩家{player.username}（玩家ID：{player.pk}）已成功{operation}你的田地{short_code}（{code}），"
            f"本次{days}个游戏天，单价{limits['daily_price']}金币/游戏天。"
            f"你（Bill）已收到租金{total}金币，入账后余额为{bill.money}金币。"
            f"{period}租约到期时间为{_world_time_text(end)}（世界分钟{end}）。"
            "后端已完成扣款、入账和租约登记，无需再次收款或审批。"
            "租期内由该玩家经营，作物归玩家；你不可播种或收获该田。"
            "到期有作物时保留玩家本批收获权，直至收获或宽限截止后田地归还。")
        plot.lease_ends_world_minute = end
        plot.lease_price_paid += total
        plot.lease_expiry_notified = False
        result.update(days=days, daily_price=limits["daily_price"], spent_gold=total, total_price=total)
        changes.update(days=days, daily_price=limits["daily_price"], **{f"PLAYER:{player.pk}": {"money": -total}, BILL_ID: {"money": total}})
    else:
        if plot.lessee_player_id != player.pk:
            fail("PLOT_NOT_OWNED", "只能操作自己承租的田地。")
        if state == "AVAILABLE":
            fail("HARVEST_WINDOW_CLOSED", "最后收获期限已结束。")
        if action == "plant_farm_plot":
            if state != "ACTIVE":
                fail("LEASE_EXPIRED", "租约已到期，请先找 Bill 续租。")
            if plot.status != "EMPTY":
                fail("PLOT_NOT_EMPTY", "田地已有作物。")
            crop = p.get("crop_code")
            rule = FARM_CROP_RULES.get(crop) if isinstance(crop, str) else None
            if rule is None:
                fail("INVALID_PARAMS", "请选择有效作物。")
            cost = limits["plant_action_cost"]
        else:
            if not plot.batch_id or str(plot.batch_id) != p.get("expected_batch_id"):
                fail("PLOT_BATCH_CHANGED", "作物批次已变化，请刷新后重试。")
            if plot.status not in {"GROWING", "MATURE"} or plot.mature_world_minute is None or minute < plot.mature_world_minute:
                fail("PLOT_NOT_MATURE", "本批作物尚未成熟。")
            crop = plot.crop_code
            rule = FARM_CROP_RULES.get(crop)
            if rule is None:
                fail("FARM_UNAVAILABLE", "作物配置尚未就绪。")
            cost = limits["harvest_action_cost"]
        player_state, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
        player_state = PlayerState.objects.select_for_update().get(pk=player_state.pk)
        recover_action_points(player_state)
        if player_state.action_points < cost:
            fail("INSUFFICIENT_ACTION_POINTS", "行动力不足。")
        result["tool_used"] = _wear_tool(bag, minute)
        player_state.action_points -= cost
        player_state.action_points_recovered_at = timezone.now()
        player_state.save(update_fields=["action_points", "action_points_recovered_at"])
        result.update(action_cost=cost, action_points=player_state.action_points)
        rng = deterministic_rng(rid, action, minute // DAY)
        if action == "plant_farm_plot":
            _consume(bag, rule["seed_item_id"], 1, minute)
            result["items_consumed"] = [{"item_id": rule["seed_item_id"], "amount": 1}]
            plot.batch_id, plot.crop_code, plot.status = uuid.uuid4(), crop, "GROWING"
            from .farm_irrigation import effective_growth_minutes
            plot.planted_world_minute, plot.mature_world_minute = minute, minute + effective_growth_minutes(rule["growth_minutes"])
            plot.yield_seed = rng.randint(rule["yield_min"], rule["yield_max"])
            plot.source_request_id = rid
            effect = dict(action="PLANT")
        else:
            output = plot.yield_seed if plot.yield_seed is not None else rng.randint(rule["yield_min"], rule["yield_max"])
            seeds = rule["guaranteed_seed_return"] + int(rng.random() < rule["bonus_seed_chance"])
            harvested = dict(harvested_crop_code=crop, harvested_batch_id=str(plot.batch_id),
                harvested_planted_world_minute=plot.planted_world_minute,
                harvested_mature_world_minute=plot.mature_world_minute,
                output=dict(item_id=rule["output_item_id"], amount=output),
                seed_return=dict(item_id=rule["seed_item_id"], amount=seeds))
            next_index = (bag.slots.aggregate(n=Max("slot_index"))["n"] or 0) + 1
            for item_id, amount in ((rule["output_item_id"], output), (rule["seed_item_id"], seeds)):
                InventorySlot.objects.create(bag=bag, slot_index=next_index, item_id=item_id,
                    item_amount=amount, quality=3, produced_world_minute=minute, source_request_id=rid)
                next_index += 1
                result["items_added"].append(dict(item_id=item_id, amount=amount))
            clear_crop(plot)
            if state == "HARVEST_ONLY":
                clear_lease(plot)
            effect = dict(action="HARVEST", harvested=harvested)
        changes[f"PLAYER:{player.pk}"] = {str(i["item_id"]): i["amount"] for i in result["items_added"]}
        changes[f"PLAYER:{player.pk}"].update({str(i["item_id"]): -i["amount"] for i in result["items_consumed"]})
        changes["tool_used"] = result["tool_used"]
    plot.plot_version += 1
    plot.save()
    result["plot"] = project_plot(plot, minute)
    public = {}
    if effect:
        effect_plot = {**result["plot"], **effect.pop("harvested", {})}
        public["farm_action_effect"] = dict(request_id=str(rid), player_id=str(player.pk),
            action=effect["action"], world_minute=minute, plot=effect_plot)
        result["farm_action_effect"] = public["farm_action_effect"]
    labels = {"lease_farm_plot": "租赁", "renew_farm_plot": "续租", "plant_farm_plot": "播种", "harvest_farm_plot": "收获"}
    event = _event(plot, minute, clock.time_revision, "PLAYER_" + action.upper(),
        f"{player.username}{labels[action]}了{code}。", player_id=player.pk, request_id=rid, public=public,
        hub_summary=lease_notice, private={"farm_lease": lease_details} if lease_details else None)
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"player:{player.pk}:{rid}"),
        entry_type=action.upper(), changes=changes, world_minute=minute, event=event)
    result["bag"] = serialize_bag(bag)
    return result
