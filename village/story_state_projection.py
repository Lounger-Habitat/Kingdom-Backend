"""NPC-facing INSPECT_STORY_STATE; does not change story/UI projections."""
from copy import deepcopy


def project_story_state(detail, npc_id, capabilities, world_minute, outcome=None):
    npc_id = str(npc_id)
    capabilities = {str(value).upper() for value in capabilities}
    manager = npc_id == "10013"
    data = {key: deepcopy(detail[key]) for key in (
        "story_id", "title", "status", "phase", "plan", "story_version",
        "bridge_condition", "progress", "available_actions",
        "available_actions_explanation", "known_facts",
    ) if key in detail}
    if outcome:
        data["outcome"] = outcome
    if npc_id == "10015" and "emergency_route_rule" in detail:
        data["emergency_route_rule"] = detail["emergency_route_rule"]

    # Empty ration configuration has no effect yet. Keep real public progress,
    # including zero resources, so the NPC can judge what is still needed.
    progress = data.get("progress", {})
    if not progress.get("ration_daily_limits"):
        progress.pop("ration_daily_limits", None)
        progress.pop("ration_limits_until_world_minute", None)

    explanation = data.get("available_actions_explanation", "")
    if data.get("phase") == "AFTERMATH" and "日结评估满足任一条件" in explanation:
        activities = [label for action, label in (
            ("PLANT", "种植"), ("HUNT", "狩猎"), ("COOK", "做饭"),
            ("LIST_STORE_ITEM", "备货上架"), ("EAT", "就餐"), ("WAIT", "等待"),
        ) if action in capabilities]
        data["available_actions_explanation"] = (
            "当前尚未进入公共行动阶段，日结评估满足条件后会推进。"
            + ("你可先处理日常生计：" + "、".join(activities) + "。" if activities else "")
        )

    meetings = []
    if manager or "STORY_VOTE" in capabilities:
        for meeting in detail.get("meetings", []):
            if manager:
                meetings.append(deepcopy(meeting))
            else:
                # Public proposal terms are needed to make an informed vote;
                # meeting settlement controls belong to the chairperson.
                meetings.append({key: deepcopy(meeting[key]) for key in (
                    "meeting_id", "meeting_status", "location_code", "starts_world_minute",
                    "closes_day", "candidate_plans", "proposals", "voting_open",
                    "voting_unavailable_reason", "own_vote", "npc_vote_counts",
                    "valid_npc_vote_count", "required_npc_vote_count",
                ) if key in meeting})
    if meetings:
        data["meetings"] = meetings

    decisions = []
    for decision in detail.get("public_decisions", []):
        if manager:
            decisions.append(deepcopy(decision))
            continue
        terms = decision.get("terms") or {}
        if (decision.get("published_world_minute") is None
                or decision.get("revoked_world_minute") is not None
                or int(terms.get("expires_world_minute", 0)) <= world_minute):
            continue
        meal = terms.get("meal_service") or {}
        executor = str(terms.get("executor_npc_id")) == npc_id
        meal_executor = str(meal.get("executor_npc_id")) == npc_id
        row = {key: deepcopy(decision[key]) for key in (
            "decision_id", "plan_code", "published_world_minute",
        ) if key in decision}
        # Everyone needs the rules affecting their own purchases and meals.
        # Only actual executors need procurement instructions and spending data.
        visible_terms = {key: deepcopy(terms[key]) for key in (
            "expires_world_minute", "ration_daily_limits",
        ) if key in terms}
        if executor:
            visible_terms = deepcopy(terms)
        if meal:
            visible_terms["meal_service"] = deepcopy(meal) if meal_executor else {
                key: deepcopy(meal[key]) for key in (
                    "store_code", "menu_codes", "starts_world_minute", "ends_world_minute",
                    "per_npc_daily_limit", "village_daily_limit",
                ) if key in meal
            }
            if not meal_executor:
                visible_terms["meal_service"]["eligible"] = (
                    npc_id in {str(value) for value in meal["beneficiaries"]}
                    if "beneficiaries" in meal else None
                )
        if executor or meal_executor:
            for key in ("remaining_budget", "spent_gold"):
                if key in decision:
                    row[key] = decision[key]
        row["terms"] = visible_terms
        decisions.append(row)
    if decisions:
        data["public_decisions"] = decisions
        data["public_decisions_scope"] = (
            "LATEST_10" if manager else "PUBLISHED_ACTIVE_FROM_LATEST_10"
        )
    if detail.get("has_more_public_decisions"):
        data["has_more_public_decisions"] = True
    if manager and detail.get("unreviewed_project_entries"):
        for key in ("unreviewed_project_entries", "unreviewed_project_entries_scope"):
            if detail.get(key):
                data[key] = deepcopy(detail[key])
    return data
