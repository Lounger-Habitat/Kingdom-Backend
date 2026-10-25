"""Truthful source/use guide; projections never initialize inventory or stock."""
from inventory.models import CollectionPoint
from player.progression import growth_snapshot
from .advanced_gathering import LOCATION, resource_overview
from .phase3_catalog import FARM_CROP_RULES


def production_overview(player, bag, minute, *, read_only=False, tool_state=None):
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
    resources.extend([
        dict(code="basic_hunting", title="\u57fa\u7840\u730e\u573a", location_code="HUNTING_GATE",
            description="\u771f\u5b9e\u72e9\u730e\u9e21\u3001\u732a\u4e0e\u725b\uff0c\u51fb\u6740\u540e\u83b7\u5f97\u5f85\u52a0\u5de5\u730e\u7269\u3002",
            requirements=["\u5148\u9886\u53d6\u4e00\u6b21\u6027\u57fa\u7840\u6b66\u5668\u3001\u6301\u6709\u5e76\u4fdd\u6301\u53ef\u7528\u8010\u4e45\u3002"],
            uses=["Jack\u52a0\u5de5\u540e\u53ef\u7528\u4e8e\u6599\u7406\u3001\u4ea4\u6613\u4e0e\u771f\u5b9e\u8ba2\u5355\u3002"], available=True,
            remaining_quantity=None, next_restore_world_minute=None),
        dict(code="deer", title="\u6df1\u6797\u9e7f", location_code="DEEP_FOREST",
            description="\u9e7f\u4f1a\u63d0\u524d\u611f\u77e5\u8fd1\u5904\u73a9\u5bb6\u5e76\u5feb\u901f\u9003\u8dd1\u3002",
            requirements=["\u63a8\u8350\u72e9\u730e4\u7ea7\u4e0eT2\u6b66\u5668\uff0c\u533a\u57df\u4e0d\u8bbe\u4e2a\u4eba\u7a7a\u6c14\u5899\u3002"],
            uses=["\u9e7f\u8089\u53ef\u5236\u4f5c\u73b0\u6709\u4e24\u79cd\u8089\u6c64\uff1b\u7c97\u76ae\u7528\u4e8e\u6b66\u5668\u5347\u7ea7\u3002"], available=True,
            remaining_quantity=None, next_restore_world_minute=None)])
    from .professional_catalog import PROFESSIONAL_COLLECT_TYPES
    from .advanced_gathering import professional_resource_overview
    for collect_type, spec in PROFESSIONAL_COLLECT_TYPES.items():
        resources.append(dict(code=spec["code"], title=spec["title"], location_code="DEEP_FOREST",
            description="深林专业共享资源，每次两份，品质由技能与工具共同决定。",
            requirements=["\u91c7\u96c67\u7ea7\u3001T3\u91c7\u96c6\u5305\u300120AP\u30011\u8010\u4e45\u3002"],
            uses=["\u4e13\u4e1a\u6599\u7406\u3001\u5b9e\u9645\u5408\u540c\u3001\u672c\u4eba\u7cbe\u901a\u8003\u6838\u3002"], available=levels["gathering"] >= 7,
            **professional_resource_overview(collect_type, minute)))
    resources.extend([
        dict(code="durable_wood", title="\u97e7\u6728", location_code="DEEP_FOREST", description="\u4e24\u68f5\u72ec\u7acb\u4e13\u4e1a\u6811\uff0c10\u6b21\u6709\u6548\u547d\u4e2d\u4ea7\u51fa3\u4efd\u3002",
            requirements=["\u4f10\u67287\u7ea7\u3001T3\u65a7\u3002"], uses=["T3\u7ef4\u4fee\u3001\u5b9e\u9645\u8d38\u6613\u4e0e\u8003\u6838\u3002"], available=levels["forestry"] >= 7),
        dict(code="boar", title="\u5317\u5761\u91ce\u732a", location_code="NORTH_SLOPE", description="\u6709\u8d77\u624b\u63d0\u793a\u7684\u76f4\u7ebf\u51b2\u649e\uff0c\u547d\u4e2d\u626330\u751f\u547d\u3002",
            requirements=["\u63a8\u8350T3\u5f13\u77db\uff0cG\u6b63\u9762\u683c\u6321\u6216R\u7ffb\u6eda\u3002"], uses=["Jack\u6536\u8d398\u91d1\u52a0\u5de5\u51fa\u771f\u5b9e\u8089\u76ae\u3002"], available=True)])
    for code, rule in FARM_CROP_RULES.items():
        professional = code == "PUMPKIN"
        advanced = code in {"ONION", "GREEN_RADISH"}
        resources.append(dict(code="crop_" + code, title=rule["display_name"], location_code="BILL_FARM",
            description=f"基础生长 {rule['growth_minutes']} 世界分钟；水渠效果以新播种批次为准。",
            requirements=["本人有效租田、种子和可用农具。",
                "\u79cd\u690d7\u7ea7\u53caT3\u519c\u5177\u3002" if professional else "种植 4 级及 T2 农具。" if advanced else "基础作物在种植 1 级开放。"],
            uses=["可按真实报价出售、交付合格订单或用于已有配方。"],
            available=levels["farming"] >= (7 if professional else 4 if advanced else 1),
            remaining_quantity=None, next_restore_world_minute=None))
    point = CollectionPoint.objects.filter(collect_type="采集林下食材").first()
    if point is None:
        resources[1].update(available=False, description="林下采集点目录尚未就绪。", remaining_quantity=None)
    return dict(revision=growth["revision"], tools=tool_state if tool_state is not None else tools_overview(player, bag, minute, read_only=read_only), resources=resources)
