from django.db import migrations, models


MARKETPLACE_ACTIONS = [
    "BUY_FROM_MARKETPLACE",
    "SELL_ON_MARKETPLACE",
    "DELIST_FROM_MARKETPLACE",
    "BROWSE_MARKETPLACE",
    "CHECK_MY_MARKETPLACE_LISTINGS",
    "CHECK_MARKETPLACE_HISTORY",
]


def add_marketplace_capabilities(apps, schema_editor):
    NpcProfile = apps.get_model("village", "NpcProfile")
    for profile in NpcProfile.objects.select_related("npc"):
        npc_id = str(profile.npc.agent_id)
        if npc_id not in {"10011", "10012", "10013", "10014", "10015"}:
            continue
        capabilities = list(profile.capabilities or [])
        for action in MARKETPLACE_ACTIONS:
            if action not in capabilities:
                capabilities.append(action)
        if capabilities != profile.capabilities:
            profile.capabilities = capabilities
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0023_npc_dialogue_session")]

    operations = [
        migrations.AlterField(
            model_name="villageactiondurationconfig",
            name="action_name",
            field=models.CharField(
                choices=[
                    ("MOVE", "移动（MOVE）"),
                    ("WAIT", "等待（WAIT）"),
                    ("GO_HOME", "回家（GO_HOME）"),
                    ("EAT", "进食（EAT）"),
                    ("REST", "休息（REST）"),
                    ("SLEEP", "睡眠（SLEEP）"),
                    ("SOCIALIZE", "社交（SOCIALIZE）"),
                    ("WORK_PLACEHOLDER", "工作（WORK_PLACEHOLDER）"),
                    ("PLANT", "播种（PLANT）"),
                    ("HARVEST", "收获（HARVEST）"),
                    ("HUNT", "狩猎（HUNT）"),
                    ("COOK", "烹饪（COOK）"),
                    ("CREATE_ORDER", "创建订单（CREATE_ORDER）"),
                    ("CANCEL_ORDER", "取消订单（CANCEL_ORDER）"),
                    ("START_DELIVERY", "开始配送（START_DELIVERY）"),
                    ("IMPORT_GOODS", "外部进货（IMPORT_GOODS）"),
                    ("EXPORT_GOODS", "外部出货（EXPORT_GOODS）"),
                    ("RATE_DISH", "菜品评分（RATE_DISH）"),
                    ("PUBLISH_PRICE_INDEX", "发布价格指数（PUBLISH_PRICE_INDEX）"),
                    ("AUDIT_PUBLIC_FUND", "审计公款（AUDIT_PUBLIC_FUND）"),
                    ("PROPOSE_TRADE", "发起交易报价（PROPOSE_TRADE）"),
                    ("RESPOND_TRADE", "响应交易报价（RESPOND_TRADE）"),
                    ("CANCEL_TRADE_OFFER", "取消交易报价（CANCEL_TRADE_OFFER）"),
                    ("LIST_STORE_ITEM", "上架商品（LIST_STORE_ITEM）"),
                    ("UPDATE_STORE_PRICE", "修改商品价格（UPDATE_STORE_PRICE）"),
                    ("DELIST_STORE_ITEM", "下架商品（DELIST_STORE_ITEM）"),
                    ("BUY_FROM_STORE", "商店购买（BUY_FROM_STORE）"),
                    ("BUY_FROM_MARKETPLACE", "购买全服商品（BUY_FROM_MARKETPLACE）"),
                    ("SELL_ON_MARKETPLACE", "上架全服商品（SELL_ON_MARKETPLACE）"),
                    ("DELIST_FROM_MARKETPLACE", "下架全服商品（DELIST_FROM_MARKETPLACE）"),
                    ("BROWSE_MARKETPLACE", "浏览全服市场（BROWSE_MARKETPLACE）"),
                    ("CHECK_MY_MARKETPLACE_LISTINGS", "查看我的全服挂单（CHECK_MY_MARKETPLACE_LISTINGS）"),
                    ("CHECK_MARKETPLACE_HISTORY", "查看全服成交历史（CHECK_MARKETPLACE_HISTORY）"),
                    ("INSPECT_STORE_INVENTORY", "查看商店库存（INSPECT_STORE_INVENTORY）"),
                    ("JOINT_WORK", "共同工作（JOINT_WORK）"),
                    ("MEETING", "见面（MEETING）"),
                    ("CREATE_COMMITMENT", "创建承诺（CREATE_COMMITMENT）"),
                    ("CANCEL_COMMITMENT", "取消承诺（CANCEL_COMMITMENT）"),
                    ("PERFORM_COMMITMENT", "履行承诺（PERFORM_COMMITMENT）"),
                ],
                max_length=32,
                unique=True,
                verbose_name="动作",
            ),
        ),
        migrations.RunPython(add_marketplace_capabilities, migrations.RunPython.noop),
    ]
