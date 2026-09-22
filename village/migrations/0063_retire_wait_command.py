from django.db import migrations, models


def retire_wait_command(apps, schema_editor):
    alias = schema_editor.connection.alias
    NpcProfile = apps.get_model("village", "NpcProfile")
    Duration = apps.get_model("village", "VillageActionDurationConfig")
    Runtime = apps.get_model("village", "NpcRuntimeState")
    Command = apps.get_model("village", "NpcCommandState")
    Receipt = apps.get_model("village", "VillageActionReceipt")
    Outbox = apps.get_model("village", "VillageDeliveryOutbox")
    Reservation = apps.get_model("village", "NpcActionReservation")
    Clock = apps.get_model("game_time", "GlobalGameTimeState")

    clock = Clock.objects.using(alias).order_by("pk").first()
    world_minute = clock.world_minute if clock else 0
    time_revision = clock.time_revision if clock else 0

    for profile in NpcProfile.objects.using(alias).all().iterator():
        capabilities = [
            value for value in (profile.capabilities or [])
            if str(value).strip().upper() != "WAIT"
        ]
        if capabilities != profile.capabilities:
            profile.capabilities = capabilities
            profile.save(using=alias, update_fields=["capabilities"])

    Duration.objects.using(alias).filter(action_name="WAIT").delete()

    for runtime in Runtime.objects.using(alias).filter(
        models.Q(current_action="WAIT") | models.Q(current_goal="WAIT")
    ).iterator():
        runtime.current_action = ""
        runtime.current_goal = "NONE"
        runtime.action_status = "IDLE"
        runtime.action_started_world_minute = None
        runtime.action_ends_world_minute = None
        runtime.active_command_id = None
        runtime.last_simulated_world_minute = world_minute
        runtime.time_revision = time_revision
        runtime.state_version += 1
        runtime.save(using=alias, update_fields=[
            "current_action", "current_goal", "action_status",
            "action_started_world_minute", "action_ends_world_minute",
            "active_command_id", "last_simulated_world_minute",
            "time_revision", "state_version",
        ])

    Reservation.objects.using(alias).filter(
        action_kind="WAIT", status="HELD"
    ).update(
        status="RELEASED", active_lock_key=None, released_world_minute=world_minute
    )

    commands = Command.objects.using(alias).filter(
        kind="WAIT",
        status__in=["RECEIVED", "QUEUED", "RUNNING", "SETTLING"],
    )
    for command in commands.iterator():
        runtime = Runtime.objects.using(alias).filter(npc_id=command.npc_id).first()
        state_version = runtime.state_version if runtime else command.state_version
        command.status = "FAILED"
        command.result_code = "ACTION_RETIRED"
        command.completed_world_minute = world_minute
        command.state_version = state_version
        command.save(using=alias, update_fields=[
            "status", "result_code", "completed_world_minute", "state_version",
        ])
        receipt, _ = Receipt.objects.using(alias).get_or_create(
            request_id=command.request_id,
            defaults={
                "command_id": command.command_id,
                "npc_id": command.npc_id,
                "action_kind": "WAIT",
                "success": False,
                "result_code": "ACTION_RETIRED",
                "response_data": {},
                "world_minute": world_minute,
                "time_revision": time_revision,
                "state_version": state_version,
            },
        )
        Outbox.objects.using(alias).get_or_create(
            dedupe_key=f"outcome:{command.request_id}",
            defaults={
                "recipient": command.sender_id,
                "message_kind": "OUTCOME",
                "command_id": command.command_id,
                "receipt_id": receipt.request_id,
                "payload": {
                    "schema_version": "village.outcome.v1",
                    "ref_id": str(command.command_id),
                    "original_message_id": str(command.message_id),
                    "request_id": str(command.request_id),
                    "npc_id": str(command.npc.agent_id),
                    "action": "WAIT",
                    "success": False,
                    "result_code": "ACTION_RETIRED",
                    "message": "此指令已停用。",
                    "world_event_id": None,
                    "world_minute": world_minute,
                    "time_revision": time_revision,
                    "state_version": state_version,
                    "retryable": False,
                    "effects": {},
                    "data": {},
                },
                "priority": 10,
                "status": "PENDING",
            },
        )


class Migration(migrations.Migration):
    dependencies = [("village", "0062_bridge_worksites")]

    operations = [
        migrations.RunPython(retire_wait_command, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="npcruntimestate",
            name="current_action",
            field=models.CharField(
                blank=True,
                choices=[("", "无"), ("MOVE", "移动"), ("GO_HOME", "回家")],
                default="",
                max_length=32,
                verbose_name="当前动作",
            ),
        ),
        migrations.AlterField(
            model_name="npcruntimestate",
            name="current_goal",
            field=models.CharField(
                choices=[("NONE", "无目标"), ("MOVE", "移动"), ("GO_HOME", "回家")],
                default="NONE",
                max_length=32,
                verbose_name="当前目标",
            ),
        ),
        migrations.AlterField(
            model_name="villageactiondurationconfig",
            name="action_name",
            field=models.CharField(
                choices=[
                    ("MOVE", "移动（MOVE）"),
                    ("GO_HOME", "回家（GO_HOME）"),
                    ("EAT", "进食（EAT）"),
                    ("REST", "休息（REST）"),
                    ("SLEEP", "睡眠（SLEEP）"),
                    ("SOCIALIZE", "社交（SOCIALIZE）"),
                    ("WORK_PLACEHOLDER", "工作（WORK_PLACEHOLDER）"),
                    ("PLANT", "播种（PLANT）"),
                    ("HARVEST", "收获（HARVEST）"),
                    ("HUNT", "狩猎（HUNT）"),
                    ("PROCESS_GAME", "处理猎物（PROCESS_GAME）"),
                    ("COOK", "烹饪（COOK）"),
                    ("CREATE_ORDER", "创建订单（CREATE_ORDER）"),
                    ("CANCEL_ORDER", "取消订单（CANCEL_ORDER）"),
                    ("START_DELIVERY", "开始配送（START_DELIVERY）"),
                    ("CONTACT_CARAVAN", "联络商队（CONTACT_CARAVAN）"),
                    ("WITHDRAW_STORE_FUNDS", "商店提款（WITHDRAW_STORE_FUNDS）"),
                    ("REQUEST_DISH_REVIEW", "提交菜品品鉴（REQUEST_DISH_REVIEW）"),
                    ("CANCEL_DISH_REVIEW", "取消菜品品鉴（CANCEL_DISH_REVIEW）"),
                    ("DEPOSIT_STORE_FUNDS", "商店存款（DEPOSIT_STORE_FUNDS）"),
                    ("WITHDRAW_STORE_ITEM", "商店取货（WITHDRAW_STORE_ITEM）"),
                    ("SET_STORE_LIMITS", "设置商店限购（SET_STORE_LIMITS）"),
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
    ]
