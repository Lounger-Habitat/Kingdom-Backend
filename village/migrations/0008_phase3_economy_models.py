"""Phase 3 economy models: ledger, account, farm plots, orders, deliveries,
price snapshots, restaurant state."""

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0024_phase3_inventory_batches"),
        ("village", "0007_seed_phase2_food"),
    ]

    operations = [
        migrations.CreateModel(
            name="EconomicLedgerEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_id", models.UUIDField(unique=True, verbose_name="请求ID")),
                ("entry_type", models.CharField(max_length=32, verbose_name="类型")),
                ("changes", models.JSONField(default=dict, verbose_name="变更")),
                ("external_delta", models.IntegerField(default=0, verbose_name="外部差额")),
                ("world_minute", models.BigIntegerField(verbose_name="世界分钟")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "actor_npc",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ledger_entries",
                        to="inventory.npcdefinition",
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ledger_entries",
                        to="village.villageworldevent",
                    ),
                ),
            ],
            options={
                "verbose_name": "经济账目",
                "verbose_name_plural": "经济账目",
            },
        ),
        migrations.CreateModel(
            name="VillageAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_code", models.CharField(max_length=32, unique=True, verbose_name="账户代码")),
                ("balance", models.IntegerField(default=0, verbose_name="余额")),
                ("asset_version", models.BigIntegerField(default=0, verbose_name="资产版本")),
                ("last_caravan_day", models.IntegerField(blank=True, null=True, verbose_name="上次商队日")),
                ("last_closed_day_index", models.IntegerField(default=-1, verbose_name="上次关闭日索引")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "村庄账户",
                "verbose_name_plural": "村庄账户",
            },
        ),
        migrations.AddConstraint(
            model_name="villageaccount",
            constraint=models.CheckConstraint(
                name="village_account_balance_non_negative",
                condition=models.Q(balance__gte=0),
            ),
        ),
        migrations.CreateModel(
            name="VillageFarmPlot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("plot_code", models.CharField(max_length=32, unique=True, verbose_name="地块代码")),
                ("status", models.CharField(default="EMPTY", max_length=16, verbose_name="状态")),
                ("batch_id", models.UUIDField(blank=True, null=True, unique=True, verbose_name="批次ID")),
                ("crop_code", models.CharField(blank=True, default="", max_length=32, verbose_name="作物代码")),
                ("planted_world_minute", models.BigIntegerField(blank=True, null=True, verbose_name="播种世界分钟")),
                ("mature_world_minute", models.BigIntegerField(blank=True, null=True, verbose_name="成熟世界分钟")),
                ("yield_seed", models.IntegerField(blank=True, null=True, verbose_name="预期产量种子")),
                ("source_request_id", models.UUIDField(blank=True, null=True, verbose_name="来源请求ID")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "农田地块",
                "verbose_name_plural": "农田地块",
            },
        ),
        migrations.CreateModel(
            name="NpcTradeOrder",
            fields=[
                ("order_id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False, verbose_name="订单ID")),
                ("request_id", models.UUIDField(unique=True, verbose_name="请求ID")),
                ("side", models.CharField(max_length=4, verbose_name="方向")),
                ("item_id", models.IntegerField(verbose_name="物品ID")),
                ("amount", models.IntegerField(verbose_name="数量")),
                ("unit_price", models.IntegerField(verbose_name="单价")),
                ("delivery_point_code", models.CharField(max_length=64, verbose_name="交付点")),
                ("expires_world_minute", models.BigIntegerField(verbose_name="过期世界分钟")),
                ("status", models.CharField(default="OPEN", max_length=16, verbose_name="状态")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "matched_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="village.npctradeorder",
                    ),
                ),
                (
                    "npc",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="trade_orders",
                        to="inventory.npcdefinition",
                    ),
                ),
            ],
            options={
                "verbose_name": "NPC 交易订单",
                "verbose_name_plural": "NPC 交易订单",
            },
        ),
        migrations.AddConstraint(
            model_name="npctradeorder",
            constraint=models.CheckConstraint(
                name="trade_order_amount_positive",
                condition=models.Q(amount__gt=0),
            ),
        ),
        migrations.AddConstraint(
            model_name="npctradeorder",
            constraint=models.CheckConstraint(
                name="trade_order_unit_price_positive",
                condition=models.Q(unit_price__gt=0),
            ),
        ),
        migrations.CreateModel(
            name="NpcTradeDelivery",
            fields=[
                ("delivery_id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False, verbose_name="交付ID")),
                ("seller_slot_instance_id", models.UUIDField(verbose_name="卖方槽位批次ID")),
                ("reserved_amount", models.IntegerField(verbose_name="预留数量")),
                ("reserved_money", models.IntegerField(verbose_name="预留金额")),
                ("delivery_point_code", models.CharField(max_length=64, verbose_name="交付点")),
                ("expires_world_minute", models.BigIntegerField(verbose_name="过期世界分钟")),
                ("status", models.CharField(default="ACTIVE", max_length=16, verbose_name="状态")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "buyer_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="buyer_deliveries",
                        to="village.npctradeorder",
                    ),
                ),
                (
                    "seller_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="seller_deliveries",
                        to="village.npctradeorder",
                    ),
                ),
            ],
            options={
                "verbose_name": "NPC 交易交付",
                "verbose_name_plural": "NPC 交易交付",
            },
        ),
        migrations.AddConstraint(
            model_name="npctradedelivery",
            constraint=models.CheckConstraint(
                name="delivery_reserved_amount_non_negative",
                condition=models.Q(reserved_amount__gte=0),
            ),
        ),
        migrations.AddConstraint(
            model_name="npctradedelivery",
            constraint=models.CheckConstraint(
                name="delivery_reserved_money_non_negative",
                condition=models.Q(reserved_money__gte=0),
            ),
        ),
        migrations.CreateModel(
            name="DailyPriceSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day_index", models.IntegerField(unique=True, verbose_name="日索引")),
                ("prices", models.JSONField(default=dict, verbose_name="价格")),
                ("status", models.CharField(default="DRAFT", max_length=16, verbose_name="状态")),
                ("published_by_npc_id", models.IntegerField(blank=True, null=True, verbose_name="发布NPC ID")),
                ("published_world_minute", models.BigIntegerField(blank=True, null=True, verbose_name="发布世界分钟")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
            ],
            options={
                "verbose_name": "每日价格快照",
                "verbose_name_plural": "每日价格快照",
            },
        ),
        migrations.AddConstraint(
            model_name="dailypricesnapshot",
            constraint=models.CheckConstraint(
                name="daily_price_day_index_non_negative",
                condition=models.Q(day_index__gte=0),
            ),
        ),
        migrations.CreateModel(
            name="VillageRestaurantState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day_index", models.IntegerField(unique=True, verbose_name="日索引")),
                ("status", models.CharField(default="OPEN", max_length=16, verbose_name="状态")),
                ("traveler_sales_count", models.IntegerField(default=0, verbose_name="旅客销售数")),
                ("traveler_revenue", models.IntegerField(default=0, verbose_name="旅客收入")),
                ("asset_version", models.BigIntegerField(default=0, verbose_name="资产版本")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "餐厅状态",
                "verbose_name_plural": "餐厅状态",
            },
        ),
        migrations.AddConstraint(
            model_name="villagerestaurantstate",
            constraint=models.CheckConstraint(
                name="restaurant_traveler_sales_non_negative",
                condition=models.Q(traveler_sales_count__gte=0),
            ),
        ),
        migrations.AddConstraint(
            model_name="villagerestaurantstate",
            constraint=models.CheckConstraint(
                name="restaurant_traveler_revenue_non_negative",
                condition=models.Q(traveler_revenue__gte=0),
            ),
        ),
    ]
