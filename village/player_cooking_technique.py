"""Small deterministic cooking technique rules; never accepts client scores or time."""
import math

from items.quality import NEUTRAL_INGREDIENT_IDS

CUTS = {"WHOLE": "整份", "CHUNK": "切块", "SLICE": "切片"}
HEATS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
QUALITY_NAMES = {1: "略逊", 2: "一般", 3: "正常", 4: "优秀", 5: "极品", 6: "绝品"}


def ingredient_feedback(average):
    return "主料整体品质接近" + QUALITY_NAMES[max(1, min(6, math.floor(average + .5)))] + "，影响料理品质。"


def technique_for(recipe, *, ingredients=None, level=1, tier=1):
    name = recipe.recipe_name if recipe else ""
    steam = "蒸" in name
    cut = "WHOLE" if steam else "CHUNK" if any(word in name for word in ("汤", "炖", "羹")) else "SLICE"
    rows = ingredients if ingredients is not None else [dict(item_id=row.item_id, item_name=row.item_name, quantity=row.count)
        for row in recipe.ingredients.all()] if recipe else []
    main = next((row.get("item_name", "主料") for row in rows
        if row.get("item_id") not in NEUTRAL_INGREDIENT_IDS and row.get("item_type") != "调料类"), "主料")
    tolerance = min(6, max(0, level - 1) * .4 + max(0, tier - 1) * 1.2)
    finish = "MEDIUM" if steam else "LOW"
    return dict(recommended_cut=cut, main_ingredient_name=main, boil_progress=30,
        optimal_start=round(68 - tolerance, 1), optimal_end=round(85 + tolerance, 1),
        heat_rates={"LOW": 5.6, "MEDIUM": 7.5, "HIGH": 10}, duration_seconds=15,
        first_heat="HIGH", finish_heat=finish,
        instruction_start="先用大火加热，留意沸腾刻度。",
        instruction_finish="沸腾后转中火蒸熟，在最佳区间出锅。" if steam else "沸腾后转小火慢煮，在最佳区间出锅。")


def advance(state, technique, elapsed):
    """Integrate each old heat segment, including a boil boundary crossed within it."""
    current = dict(state)
    duration = technique["duration_seconds"]
    target_elapsed = max(current.get("elapsed_seconds", 0), min(duration, max(0, elapsed)))
    seconds = target_elapsed - current.get("elapsed_seconds", 0)
    progress = float(current.get("progress", 0))
    error = float(current.get("heat_error", 0))
    heat = current.get("heat", technique["first_heat"])
    rate = technique["heat_rates"][heat]
    while seconds > 1e-8 and progress < 100:
        first = progress < technique["boil_progress"] - 1e-8
        boundary = technique["boil_progress"] if first else 100
        step = min(boundary - progress, rate * seconds)
        wanted = technique["first_heat"] if first else technique["finish_heat"]
        error += step * abs(HEATS[heat] - HEATS[wanted]) / 2
        progress += step
        seconds -= step / rate
    current.update(progress=100 if elapsed >= duration else min(100, progress),
        elapsed_seconds=target_elapsed, heat_error=error)
    return current


def evaluate(session, average, exact, recipe):
    technique, state = session.plan["technique"], session.state
    progress = state["progress"]
    cut_ok = session.plan["cut_style"] == technique["recommended_cut"]
    heat_ratio = max(0, 1 - state.get("heat_error", 0) / max(1, progress))
    heat_penalty = 0 if heat_ratio >= .9 else 1 if heat_ratio >= .35 else 2
    gap = max(technique["optimal_start"] - progress, progress - technique["optimal_end"], 0)
    timing_penalty = 0 if gap == 0 else 1 if gap <= 12 else 2
    quality = max(1, min(6, math.floor(4 + .6 * (average - 3) + .5)
        - int(not cut_ok) - heat_penalty - timing_penalty - int(not exact))) if recipe else 1
    timing = "在最佳区间出锅，熟度恰好。" if gap == 0 else "出锅偏早，食材尚未充分熟透。" if progress < technique["optimal_start"] else "出锅偏晚，料理已经过火。"
    suggestions = []
    if not cut_ok:
        suggestions.append("下锅将主料" + CUTS[technique["recommended_cut"]] + "。")
    if heat_penalty:
        suggestions.append("先大火，沸腾后及时转" + ("中火。" if technique["finish_heat"] == "MEDIUM" else "小火。"))
    if timing_penalty:
        suggestions.append("等游标进入最佳区间后及时出锅。")
    if not exact:
        suggestions.append("核对食材配比。")
    feedback = dict(ingredients=ingredient_feedback(average),
        cut=("主料切配符合本菜做法。" if cut_ok else "主料切配与推荐做法不同，影响口感。"),
        heat="先旺后缓，火力衔接稳定。" if not heat_penalty else "火力与烹饪阶段不匹配，影响风味。",
        timing=timing, suggestion="".join(suggestions) or "切配与火候已经掌握，下锅可尝试更高品质主料。")
    return quality, round(state["elapsed_seconds"]), feedback
