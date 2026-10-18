"""Truthful source/use guide; projections never initialize inventory or stock."""
from inventory.models import CollectionPoint
from player.progression import growth_snapshot
from .advanced_gathering import LOCATION, resource_overview
from .phase3_catalog import FARM_CROP_RULES


def production_overview(player, bag, minute, *, read_only=False):
    from .production_tools import tools_overview
    growth = growth_snapshot(player, read_only=True)
    levels = {s["code"]: s["level"] for s in growth["skills"]}
    resources = [dict(code="basic_ingredients", title="森林边缘食材", location_code="FOREST_EDGE",
        description="现有食材池，保留个人采集等级和覆盖权益。",
        requirements=["基础采集无需工具；消耗现场显示的行动力。"],
        uses=["公共供货、杂货店出售、已知料理。"], available=True,
        remaining_quantity=None, next_restore_world_minute=None),
        dict(code="undergrowth", title="林下食材点", location_code=LOCATION,
        description="蘑菇、洋葱和葱的进阶来源，所有玩家共享储量。",
        requirements=["采集 4 级、T2 采集工具包；20 行动力，每次成功磨损 1。"],
        uses=["蘑菇炖鸡、洋葱炒火腿及其他已知料理原料；也可出售。"],
        available=levels["gathering"] >= 4, **resource_overview(minute)),
        dict(code="wood", title="普通木材", location_code="FOREST_LOGGING",
        description="当前普通树木掉落原木，按共享树木状态与五游戏日再生结算。",
        requirements=["本人可用斧头；第一次可免费领取 T1 斧。"],
        uses=["工具升级、T1 维修、修桥及水渠材料。"], available=True,
        remaining_quantity=None, next_restore_world_minute=None),
        dict(code="hardwood", title="硬木", location_code="FOREST_LOGGING",
        description="森林中有独立标识的硬木树，八次有效砍击后产出四份。",
        requirements=["伐木 4 级、本人可用 T2 斧。"],
        uses=["T2 工具维修；交易或按实际报价出售。"], available=levels["forestry"] >= 4,
        remaining_quantity=None, next_restore_world_minute=None)]
    for code, rule in FARM_CROP_RULES.items():
        advanced = code in {"ONION", "GREEN_RADISH"}
        resources.append(dict(code="crop_" + code, title=rule["display_name"], location_code="BILL_FARM",
            description=f"基础生长 {rule['growth_minutes']} 世界分钟；水渠效果以新播种批次为准。",
            requirements=["本人有效租田、种子和可用农具。",
                "种植 4 级及 T2 农具。" if advanced else "基础作物在种植 1 级开放。"],
            uses=["可按真实报价出售、交付合格订单或用于已有配方。"],
            available=not advanced or levels["farming"] >= 4,
            remaining_quantity=None, next_restore_world_minute=None))
    point = CollectionPoint.objects.filter(collect_type="采集林下食材").first()
    if point is None:
        resources[1].update(available=False, description="林下采集点目录尚未就绪。", remaining_quantity=None)
    return dict(revision=growth["revision"], tools=tools_overview(player, bag, minute, read_only=read_only), resources=resources)
