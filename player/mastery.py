"""Independent mastery exams backed by actual production and business facts."""
from copy import deepcopy
import hashlib
import hmac
import json
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.components import copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from .models import (PlayerMasteryChallenge, PlayerGrowthState, PlayerMilestoneProgress,
                     PlayerState, PlayerTitle, PlayerCombatReceipt, SkillRewardReceipt)
from .progression import ensure_growth, growth_snapshot
from .progression_rules import SKILLS
from village.professional_catalog import (PROFESSIONAL_MUSHROOM, MOUNTAIN_HERB, FOREST_BERRY,
    PROFESSIONAL_WOOD, PROFESSIONAL_RECIPES, batch_evidence)

RULE_VERSION = 1
SKILL_NAMES = dict(SKILLS)


class MasteryError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code, message):
    raise MasteryError(code, message)


def _requirement(code, title, items, quantity, tier, **kwargs):
    return dict(code=code, title=title, item_ids=list(items), quantity=quantity, minimum_tool_tier=tier,
                minimum_quality=0, minimum_remaining=0, **kwargs)


def _rules(skill):
    requirements, npc, location = [], None, None
    if skill == "gathering":
        npc, location = "10015", "GENERAL_STORE"
        requirements = [_requirement(str(i), name, [i], 1, 3, fresh=True) for i, name in
            [(PROFESSIONAL_MUSHROOM, "亲采深林菌"), (MOUNTAIN_HERB, "亲采山香草"), (FOREST_BERRY, "亲采林莓")]]
        for row in requirements:
            row["minimum_quality"] = 4
    elif skill == "farming":
        npc, location = "10011", "BILL_FARM"
        requirements = [_requirement("pumpkin", "亲自收获专业南瓜", [1001], 2, 3, fresh=True, crop_code="PUMPKIN"),
                        _requirement("other_crop", "亲自收获另一种作物", [1030, 1005, 1025, 1042, 1038, 1043], 2, 1, fresh=True)]
        requirements[0]["minimum_quality"], requirements[1]["minimum_quality"] = 4, 3
    elif skill == "forestry":
        npc, location = "10012", "BUTCHER_YARD"
        requirements = [_requirement("durable", "亲伐韧木", [PROFESSIONAL_WOOD], 2, 3, species="DURABLE_OAK"),
                        _requirement("hardwood", "亲伐硬木", [29306], 2, 2, species="HARDWOOD")]
        requirements[0]["minimum_quality"] = 4
    elif skill == "cooking":
        npc, location = "10014", "RESTAURANT"
        requirements = [_requirement(str(key), f"本人正确制作{spec['name']}", [spec["item_id"]], 1, 3,
            recipe_id=key, fresh=True) for key, spec in PROFESSIONAL_RECIPES.items()]
        for row in requirements:
            row.update(minimum_quality=4, minimum_remaining=60)
    title = f"{SKILL_NAMES[skill]}大师"
    descriptions = {
        "gathering": "亲采三种专业原料，各交一份给Nora。",
        "farming": "亲自收获专业南瓜和另一种作物，各交两份给Bill。",
        "forestry": "以对应工具亲伐不同节点的韧木和硬木，各交两份给Jack。",
        "cooking": "正确制作三种专业料理，各交一份优秀且至少剩余60分钟鲜期的样品给Lucy。",
        "hunting": "同一只真实野猪完成可信格挡或翻滚、本人最终击杀，并领取本人加工产物。",
        "business": "完成专业合同，并有至少三份采购成本可追溯的合格终端销售，收入覆盖这些采购成本。",
    }
    return dict(code=f"{skill}_mastery", title=f"{SKILL_NAMES[skill]}专业考核", description=descriptions[skill],
        requirements=requirements, npc_id=npc, location_code=location,
        recipient_name={"10015": "Nora", "10011": "Bill", "10012": "Jack", "10014": "Lucy"}.get(npc),
        title_reward=dict(code=f"master_{skill}", name=title), sample_policy="实物交样不付费、不退样；进入对应NPC真实库存。")


def _production_proofs(player, skill, since):
    result = {}
    for receipt in SkillRewardReceipt.objects.filter(player=player, skill_code=skill):
        facts = receipt.result.get("facts") or {}
        source, minute = facts.get("source_request_id"), facts.get("produced_world_minute")
        if not source or type(minute) is not int or minute < since:
            continue
        for output in facts.get("outputs", []):
            if (isinstance(output, dict) and output.get("instance_id") and output.get("source_request_id") == source
                    and output.get("produced_world_minute") == minute):
                result[(source, output["instance_id"])] = (receipt, facts, output)
    return result


def _sample_proof(slot, requirement, proofs, minute, skill):
    proof = proofs.get((str(slot.source_request_id), str(slot.instance_id)))
    if not proof:
        return None
    receipt, facts, output = proof
    if (slot.item_id not in requirement["item_ids"] or facts.get("tool_tier", 0) < requirement["minimum_tool_tier"]
            or type(output.get("quantity")) is not int or slot.item_amount > output["quantity"]
            or slot.quality < requirement["minimum_quality"] or slot.freshness_unknown
            or output.get("item_id") != slot.item_id or output.get("quality") != slot.quality
            or output.get("produced_world_minute") != slot.produced_world_minute
            or output.get("expires_world_minute") != slot.expires_world_minute
            or slot.produced_world_minute is None or slot.produced_world_minute > minute):
        return None
    if requirement.get("fresh") and (slot.expires_world_minute is None or slot.expires_world_minute <= minute
            or slot.expires_world_minute-minute < requirement["minimum_remaining"]):
        return None
    if requirement.get("crop_code") and facts.get("crop_code") != requirement["crop_code"]:
        return None
    if skill == "farming" and not facts.get("batch_id"):
        return None
    if requirement.get("species") and (facts.get("species") != requirement["species"]
            or not facts.get("node_id") or type(facts.get("generation")) is not int):
        return None
    if requirement.get("recipe_id"):
        from village.player_cooking import is_player_cooked
        if facts.get("recipe_id") != requirement["recipe_id"] or not is_player_cooked(slot, receipt.player):
            return None
    return dict(code=requirement["code"], title=requirement["title"], source_id=receipt.source_id,
        **batch_evidence(slot), tool_tier=facts["tool_tier"],
        node_id=facts.get("node_id"), generation=facts.get("generation"), batch_id=facts.get("batch_id"))


def _hunting_evidence(player, since):
    from village.models import VillagePlayerProcessingJob
    results = []
    for kill in SkillRewardReceipt.objects.filter(player=player, skill_code="hunting", source_type="wildlife_kill"):
        facts = kill.result.get("facts") or {}
        death, prey_id = facts.get("produced_world_minute"), facts.get("prey_id")
        if facts.get("species") != "BOAR" or type(death) is not int or death < since or not facts.get("attack_id"):
            continue
        defense = None
        for receipt in PlayerCombatReceipt.objects.filter(player=player, action="damage"):
            data = receipt.response.get("data", {}) if receipt.response.get("success") else {}
            impact = data.get("impact_world_minute")
            if (data.get("prey_id") == prey_id and data.get("outcome") in {"BLOCKED", "DODGED"}
                    and data.get("damage") == 0 and type(impact) is int and since <= impact <= death):
                defense = receipt
                break
        if defense is None:
            continue
        outputs = facts.get("outputs") or []
        for job in VillagePlayerProcessingJob.objects.filter(player=player, status="CLAIMED", claimed_world_minute__gte=death):
            attributes = job.input_snapshot.get("attributes") or {}
            if any(str(job.source_instance_id) == output.get("instance_id")
                   and attributes.get("source_request_id") == output.get("source_request_id")
                   for output in outputs):
                results = [dict(code="boar_chain", title="同一野猪防御、亲杀并领取加工产物", source_id=prey_id,
                    defense_request_id=str(defense.request_id), kill_source_id=kill.source_id,
                    processing_job_id=str(job.pk), quantity=1, complete=True)]
                return results
    return results


def _business_evidence(player, since):
    from village.professional_contracts import completed_professional_contracts
    from restaurant.models import CustomerTransaction
    contracts = list(completed_professional_contracts(player, since_minute=since))
    sales = []
    known_transactions = {r.result.get("facts", {}).get("transaction_id") for r in
                          SkillRewardReceipt.objects.filter(player=player, skill_code="business", source_type="stall_sale")}
    for sale in CustomerTransaction.objects.filter(player=player, world_minute__gte=since,
            feedback_source="RULE_CUSTOMER", unit_cost__gt=0).order_by("pk"):
        batch = sale.batch_snapshot or {}
        expiry = batch.get("expires_world_minute")
        if (sale.pk in known_transactions and sale.dish_quality >= 3 and sale.amount_paid > 0
                and not batch.get("freshness_unknown") and type(expiry) is int and expiry > sale.world_minute):
            sales.append(sale)
    revenue, cost = sum(s.amount_paid for s in sales), sum(s.unit_cost for s in sales)
    evidence = []
    if contracts:
        evidence.append(dict(code="professional_contract", title="本人完成真实专业合同", source_id=str(contracts[0].commission_id),
                             quantity=len(contracts), complete=True))
    evidence.append(dict(code="terminal_sales", title="采购成本可追溯的合格终端销售", source_id=None,
        quantity=len(sales), required_quantity=3, revenue=revenue, purchase_cost=cost,
        transaction_ids=[s.pk for s in sales], complete=len(sales) >= 3 and revenue >= cost))
    return evidence


def _quote(player, challenge, selections, evidence, minute):
    data = dict(player_id=player.pk, challenge_id=challenge.pk, rule_version=challenge.rule_version,
        rules=challenge.rules_snapshot, selections=selections, evidence=evidence,
        contributed=challenge.contributed_world_minute)
    return hmac.new(settings.SECRET_KEY.encode(), json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _project(player, bag, skill, level, challenge, minute, read_only):
    rule = deepcopy(challenge.rules_snapshot if challenge else _rules(skill))
    selections, evidence, requirements, missing = [], [], [], []
    contributed = bool(challenge and challenge.contributed_world_minute is not None)
    if challenge and rule["requirements"]:
        if contributed:
            evidence = deepcopy(challenge.contribution_snapshot)
            for req in rule["requirements"]:
                requirements.append(dict(code=req["code"], title=req["title"], required_quantity=req["quantity"],
                                         current_quantity=req["quantity"], complete=True))
        else:
            proofs = _production_proofs(player, skill, challenge.started_world_minute)
            slots = list(bag.slots.filter(item_amount__gt=0).order_by("slot_index")) if bag.pk else []
            used = {}
            for req in rule["requirements"]:
                remaining = req["quantity"]
                for slot in slots:
                    sample = _sample_proof(slot, req, proofs, minute, skill)
                    available = slot.item_amount-used.get(slot.pk, 0)
                    if sample is None or available <= 0:
                        continue
                    quantity = min(remaining, available)
                    name = Item.objects.filter(pk=slot.item_id).values_list("item_name", flat=True).first() or str(slot.item_id)
                    selections.append(dict(requirement_code=req["code"], instance_id=str(slot.instance_id),
                        item_id=slot.item_id, item_name=name, quantity=quantity, quality=slot.quality,
                        expires_world_minute=slot.expires_world_minute, freshness_unknown=slot.freshness_unknown,
                        produced_world_minute=slot.produced_world_minute, source_request_id=str(slot.source_request_id)))
                    evidence.append(dict(sample, quantity=quantity))
                    used[slot.pk] = used.get(slot.pk, 0)+quantity
                    remaining -= quantity
                    if remaining == 0:
                        break
                complete = remaining == 0
                requirements.append(dict(code=req["code"], title=req["title"], required_quantity=req["quantity"],
                                         current_quantity=req["quantity"]-remaining, complete=complete))
                if not complete:
                    missing.append(f"{req['title']}缺少{remaining}份可核验本人新生产的合格样品。")
    elif challenge:
        evidence = _hunting_evidence(player, challenge.started_world_minute) if skill == "hunting" else _business_evidence(player, challenge.started_world_minute)
        expected = [("boar_chain", "同一野猪完成防御、亲杀、加工领取", 1)] if skill == "hunting" else [
            ("professional_contract", "完成本人专业合同", 1), ("terminal_sales", "至少三份采购成本可追溯且收入覆盖成本的合格终端销售", 3)]
        for code, title, quantity in expected:
            row = next((e for e in evidence if e["code"] == code), {})
            complete = bool(row.get("complete"))
            requirements.append(dict(code=code, title=title, required_quantity=quantity,
                current_quantity=row.get("quantity", 0), complete=complete))
            if not complete:
                missing.append(title)
    else:
        missing.append("7级后开始考核；开始后保留真实生产证据。")
        requirements = [dict(code=r["code"], title=r["title"], required_quantity=r["quantity"], current_quantity=0, complete=False)
                        for r in rule["requirements"]]
    claimed = bool(challenge and challenge.claimed_world_minute is not None)
    complete = bool(challenge and requirements and all(r["complete"] for r in requirements))
    npc_ready = bool(rule["npc_id"] and NpcGlobalInventory.objects.filter(npc__agent_id=rule["npc_id"], npc__is_test_only=False).exists())
    reason = ("MODE_READ_ONLY" if read_only else "ALREADY_CLAIMED" if claimed else "SKILL_LEVEL_REQUIRED" if level < 7
              else "CHALLENGE_NOT_STARTED" if not challenge else "EVIDENCE_REQUIRED" if not complete
              else "SAMPLES_NOT_CONTRIBUTED" if rule["requirements"] and not contributed else "MASTERY_LEVEL_REQUIRED" if level < 10 else "")
    return dict(skill_code=skill, skill_name=SKILL_NAMES[skill], level=level, started=challenge is not None,
        started_world_minute=challenge.started_world_minute if challenge else None,
        rule_version=challenge.rule_version if challenge else RULE_VERSION,
        rule={k: rule[k] for k in ("code", "title", "description", "sample_policy")}, requirements=requirements,
        evidence=evidence, missing=missing, can_start=not read_only and level >= 7 and not challenge,
        can_contribute=not read_only and not contributed and bool(rule["requirements"]) and complete and npc_ready,
        can_claim=not read_only and not claimed and level >= 10 and complete and (contributed or not rule["requirements"]),
        reason=reason, message="；".join(missing), recommended_selections=selections,
        quote_hash=_quote(player, challenge, selections, evidence, minute) if challenge and selections and complete else None,
        location_code=rule["location_code"], recipient_name=rule["recipient_name"], title=rule["title_reward"],
        reward=dict(title=rule["title_reward"], money=0, xp=0), contributed=contributed, claimed=claimed)


def mastery_overview(player, bag, minute, read_only=False):
    levels = {s["code"]: s["level"] for s in growth_snapshot(player, read_only=True)["skills"]}
    challenges = {c.skill_code: c for c in PlayerMasteryChallenge.objects.filter(player=player)}
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, challenges=[_project(player, bag, skill, levels[skill], challenges.get(skill), minute, read_only)
                                                for skill, _ in SKILLS])


@transaction.atomic
def execute_mastery_action(player, bag, action, params, rid, clock):
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    GameAccount.objects.select_for_update().get(pk=player.pk)
    if not isinstance(params, dict) or params.get("skill_code") not in SKILL_NAMES:
        reject("INVALID_PARAMS", "请选择有效的精通路线。")
    skill = params["skill_code"]
    allowed = {"skill_code", "selections", "expected_quote"} if action == "contribute_mastery" else {"skill_code"}
    if set(params) != allowed or action not in {"start_mastery", "contribute_mastery", "claim_mastery"}:
        reject("INVALID_PARAMS", "精通动作参数无效。")
    level = next(s["level"] for s in growth_snapshot(player, read_only=True)["skills"] if s["code"] == skill)
    challenge = PlayerMasteryChallenge.objects.select_for_update().filter(player=player, skill_code=skill).first()
    transferred, title = [], None
    if action == "start_mastery":
        if level < 7:
            reject("SKILL_LEVEL_REQUIRED", "对应路线7级后可开始专业考核。")
        if challenge:
            reject("CHALLENGE_ALREADY_STARTED", "该路线考核已经开始，不会重置历史进度。")
        challenge = PlayerMasteryChallenge.objects.create(player=player, skill_code=skill,
            started_world_minute=clock.world_minute, rule_version=RULE_VERSION, rules_snapshot=_rules(skill))
        status = "STARTED"
    else:
        if not challenge:
            reject("CHALLENGE_NOT_STARTED", "请先开始对应路线的专业考核。")
        view = _project(player, bag, skill, level, challenge, clock.world_minute, False)
        if action == "contribute_mastery":
            if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
                reject("PLAYER_BAG_REQUIRED", "只能交付本人主背包中的样品。")
            if challenge.contributed_world_minute is not None:
                reject("ALREADY_CONTRIBUTED", "该路线样品已经交付，不会再次扣除。")
            if not view["can_contribute"]:
                reject("EVIDENCE_REQUIRED", "需要完整合格样品包及真实生产证据；历史缺证物品需重新生产。")
            expected = [{k: s[k] for k in ("requirement_code", "instance_id", "quantity")} for s in view["recommended_selections"]]
            if params["selections"] != expected or not isinstance(params["expected_quote"], str) or not hmac.compare_digest(params["expected_quote"], view["quote_hash"]):
                reject("QUOTE_CHANGED", "所选真实批次或鲜期已变化，请重新确认完整样品包。")
            target = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=challenge.rules_snapshot["npc_id"], npc__is_test_only=False)
            InventoryBag.objects.select_for_update().get(pk=bag.pk)
            index = target.slots.aggregate(n=Max("slot_index"))["n"]
            index = 0 if index is None else index+1
            for selection in view["recommended_selections"]:
                slot = InventorySlot.objects.select_for_update().get(bag=bag, instance_id=selection["instance_id"])
                count = selection["quantity"]
                if slot.item_amount < count:
                    reject("QUOTE_CHANGED", "所选样品批次已变化。")
                sample = NpcGlobalInventorySlot(bag=target, slot_index=index, item_id=slot.item_id,
                    item_amount=count, item_bag_name=slot.item_bag_name)
                copy_batch_attributes(slot, sample)
                sample.save()
                slot.item_amount -= count
                slot.save(update_fields=["item_amount"])
                transferred.append(dict(selection, recipient_instance_id=str(sample.instance_id), recipient_name=view["recipient_name"]))
                index += 1
            target.asset_version += 1
            target.save(update_fields=["asset_version"])
            challenge.contributed_world_minute = clock.world_minute
            challenge.contribution_snapshot = deepcopy(view["evidence"])
            challenge.save(update_fields=["contributed_world_minute", "contribution_snapshot"])
            status = "CONTRIBUTED"
        else:
            if challenge.claimed_world_minute is not None:
                reject("ALREADY_CLAIMED", "该职业称号已经领取。")
            if not view["can_claim"]:
                reject("MASTERY_LEVEL_REQUIRED" if level < 10 else "EVIDENCE_REQUIRED", "需要本路线10级并完成全部真实考核。")
            state, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
            title = deepcopy(challenge.rules_snapshot["title_reward"])
            PlayerTitle.objects.get_or_create(player=state, title_id=title["code"], defaults=dict(title_name=title["name"], obtained_at=clock.world_minute))
            PlayerMilestoneProgress.objects.get_or_create(player=player, code=f"{skill}_mastery",
                defaults=dict(title=title["name"], source_type="claim_mastery", source_id=str(challenge.pk)))
            challenge.claimed_world_minute = clock.world_minute
            challenge.save(update_fields=["claimed_world_minute"])
            status = "CLAIMED"
    growth = ensure_growth(player)
    growth.revision += 1
    growth.save(update_fields=["revision"])
    return dict(skill_code=skill, status=status, title=title, items_transferred=transferred, money_gained=0, xp_gained=0)
