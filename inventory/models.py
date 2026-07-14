import uuid
from django.db import models
from account.models import GameAccount
from items.models import Item, ItemType


class AiConfig(models.Model):
    """AI 点评服务配置（单例模型）。"""
    AI_MODEL_CHOICES = [
        ("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ("deepseek-v3.2", "DeepSeek V3.2"),
        ("deepseek-v3.2-thinking", "DeepSeek V3.2 Thinking"),
        ("kimi-k2.5", "Kimi K2.5"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ("claude-opus-4-6", "Claude Opus 4.6"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
        ("gpt-5.4", "GPT 5.4"),
        ("gpt-5.3-codex", "GPT 5.3 Codex"),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku (Anthropic 直连)"),
        ("us.anthropic.claude-sonnet-4-6", "Claude Sonnet (Anthropic 直连)"),
        ("us.anthropic.claude-opus-4-6-v1", "Claude Opus (Anthropic 直连)"),
        ("gemini-3.1-flash-lite-preview", "Gemini 3.1 Flash Lite"),
        ("mimo-v2.5-pro", "MiMo V2.5 Pro"),
        ("mimo-v2.5", "MiMo V2.5"),
    ]

    model_name = models.CharField(
        max_length=100,
        choices=AI_MODEL_CHOICES,
        default="mimo-v2.5-pro",
        verbose_name="AI 模型",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "AI 配置"
        verbose_name_plural = "AI 配置"

    def __str__(self):
        return f"AI 配置: {self.get_model_name_display()}"

    @classmethod
    def get_model(cls):
        """获取当前配置的模型名称。"""
        config = cls.objects.first()
        if config:
            return config.model_name
        return "mimo-v2.5-pro"


class InventoryBag(models.Model):
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE, null=True, blank=True)
    character_id = models.CharField(max_length=100)
    money = models.IntegerField(default=0)

    class Meta:
        unique_together = [("player", "character_id")]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_money = self.money
        self._money_change_source = ""

    def save(self, *args, **kwargs):
        delta = self.money - self._orig_money
        money_changed = delta != 0
        source = self._money_change_source
        super().save(*args, **kwargs)
        self._orig_money = self.money
        # 金币变动时记录 ReportEvent 并刷新财富榜 LeaderboardEntry
        if money_changed and self.player_id:
            try:
                from player.models import PlayerState
                ps = PlayerState.objects.filter(player_id=str(self.player_id)).first()
                game_day = ps.total_game_days if ps else None
                season_id = ps.current_season_id if ps and ps.current_season_id else "default"

                from leaderboard.models import ReportEvent
                ReportEvent.objects.create(
                    player_id=str(self.player_id),
                    event_type="money_changed",
                    payload={"balance": self.money, "delta": delta, "source": source},
                    timestamp=int(__import__("time").time()),
                    game_day=game_day,
                    season_id=season_id,
                )

                from leaderboard.services import refresh_leaderboard
                from leaderboard.models import BoardType
                refresh_leaderboard(BoardType.WEALTH, season_id, game_day)
                refresh_leaderboard(BoardType.TOTAL_WEALTH, season_id)
            except Exception:
                pass

    def __str__(self):
        return f"{self.character_id} (player={self.player_id}, money={self.money})"


class InventorySlot(models.Model):
    bag = models.ForeignKey(InventoryBag, on_delete=models.CASCADE, related_name="slots")
    slot_index = models.IntegerField()
    instance_id = models.UUIDField(default=uuid.uuid4, editable=False)
    item_id = models.IntegerField(default=0)
    item_amount = models.IntegerField(default=0)
    rated = models.BooleanField(default=False)
    rating_price = models.IntegerField(default=-1)
    overall_score = models.FloatField(default=0, help_text="评价分数 0-100")
    ingredient = models.CharField(max_length=200, default="", blank=True)
    item_bag_name = models.CharField(max_length=100, default="", blank=True)
    cook_time = models.IntegerField(default=0, help_text="烹饪时长（秒）")
    dish_quality = models.IntegerField(default=3, help_text="品质：1略逊 2一般 3正常 4优秀 5极品 6绝品")
    hide_in_shop = models.BooleanField(default=False, help_text="是否在商店面板中隐藏此物品")
    remaining_uses = models.IntegerField(null=True, blank=True, help_text="剩余使用次数，null=不适用，-1=无限次")
    review_comment = models.TextField(default="", blank=True, help_text="AI点评评语")

    class Meta:
        ordering = ["slot_index"]
        unique_together = [("bag", "slot_index")]

    def __str__(self):
        return f"{self.bag.character_id}[{self.slot_index}] item={self.item_id} x{self.item_amount}"


class BagTemplate(models.Model):
    """背包模板，用于新玩家初始化。"""
    character_id = models.CharField(max_length=100, unique=True, help_text="对应角色ID，如 player、tjplb")
    money = models.IntegerField(default=0, help_text="初始金币")

    def __str__(self):
        return f"模板: {self.character_id} (money={self.money})"


class BagTemplateSlot(models.Model):
    """背包模板中的物品格位。"""
    template = models.ForeignKey(BagTemplate, on_delete=models.CASCADE, related_name="slots")
    slot_index = models.IntegerField(default=-1)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, help_text="选择物品", null=True, blank=True)
    item_amount = models.IntegerField(default=1)
    rated = models.BooleanField(default=False)
    rating_price = models.IntegerField(default=-1)
    overall_score = models.FloatField(default=0, help_text="评价分数 0-100")
    ingredient = models.CharField(max_length=200, default="", blank=True)
    cook_time = models.IntegerField(default=0, help_text="烹饪时长（秒）")
    dish_quality = models.IntegerField(default=3, help_text="品质：1略逊 2一般 3正常 4优秀 5极品 6绝品")
    hide_in_shop = models.BooleanField(default=False, help_text="是否在商店面板中隐藏此物品")
    review_comment = models.TextField(default="", blank=True, help_text="AI点评评语")

    class Meta:
        ordering = ["slot_index"]
        unique_together = [("template", "slot_index")]

    def save(self, *args, **kwargs):
        if self.slot_index < 0 and self.template_id:
            existing = set(
                BagTemplateSlot.objects.filter(template=self.template)
                .exclude(pk=self.pk)
                .values_list("slot_index", flat=True)
            )
            idx = 0
            while idx in existing:
                idx += 1
            self.slot_index = idx
        super().save(*args, **kwargs)

    def __str__(self):
        name = self.item.item_name if self.item else "?"
        return f"{self.template.character_id}[{self.slot_index}] {name} x{self.item_amount}"


# ──────────────────────────────────────────────────────────────
#  采集点等级系统
# ──────────────────────────────────────────────────────────────

class CollectionPoint(models.Model):
    """采集点定义，替代硬编码的 COLLECT_TYPE_MAP。"""
    name = models.CharField("名称", max_length=50, unique=True)
    collect_type = models.CharField("采集类型", max_length=50, unique=True,
                                    help_text="API标识，如 采集肉类、采集食材")
    item_type = models.CharField("物品类型", max_length=20, choices=ItemType.choices)
    action_cost = models.IntegerField("行动力消耗", default=15)
    collect_count = models.IntegerField("每次采集种类数", default=3)
    default_level = models.IntegerField("默认等级", default=1)

    class Meta:
        verbose_name = "采集点"
        verbose_name_plural = "采集点"
        ordering = ["collect_type"]

    def __str__(self):
        return self.name


class CollectionPointLevel(models.Model):
    """采集点等级，每等级对应一个物品池。"""
    collection_point = models.ForeignKey(CollectionPoint, on_delete=models.CASCADE,
                                         related_name="levels", verbose_name="采集点")
    level = models.IntegerField("等级")
    description = models.CharField("备注", max_length=200, blank=True, default="")
    upgrade_cost_money = models.IntegerField("升级金币消耗", default=0,
                                              help_text="从该等级升级到下一级消耗的金币")

    class Meta:
        verbose_name = "采集点等级"
        verbose_name_plural = "采集点等级"
        unique_together = [("collection_point", "level")]
        ordering = ["collection_point", "level"]

    def __str__(self):
        return f"{self.collection_point.name} Lv.{self.level}"


class CollectionPointLevelItem(models.Model):
    """某等级下的可采集物品及其权重/数量。"""
    level = models.ForeignKey(CollectionPointLevel, on_delete=models.CASCADE,
                              related_name="items", verbose_name="等级")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, verbose_name="物品")
    collect_weight = models.IntegerField("采集权重", default=100)
    collect_min_amount = models.IntegerField("最小数量", default=1)
    collect_max_amount = models.IntegerField("最大数量", default=2)

    class Meta:
        verbose_name = "等级物品"
        verbose_name_plural = "等级物品"
        unique_together = [("level", "item")]

    def __str__(self):
        return f"{self.level} - {self.item.item_name}"


class CollectionPointUpgradeItemCost(models.Model):
    """从某等级升级到下一级所需的物品消耗。"""
    level = models.ForeignKey(CollectionPointLevel, on_delete=models.CASCADE,
                              related_name="upgrade_item_costs", verbose_name="等级")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, verbose_name="所需物品")
    amount = models.IntegerField("数量", default=1)

    class Meta:
        verbose_name = "升级物品消耗"
        verbose_name_plural = "升级物品消耗"
        unique_together = [("level", "item")]

    def __str__(self):
        return f"{self.level} 升级需要 {self.item.item_name} x{self.amount}"


class PlayerCollectionPoint(models.Model):
    """玩家各采集点的当前等级。"""
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE,
                               related_name="collection_points", verbose_name="玩家")
    collection_point = models.ForeignKey(CollectionPoint, on_delete=models.CASCADE,
                                         verbose_name="采集点")
    current_level = models.IntegerField("当前等级", default=1)

    class Meta:
        verbose_name = "玩家采集点"
        verbose_name_plural = "玩家采集点"
        unique_together = [("player", "collection_point")]

    def __str__(self):
        return f"{self.player.username} - {self.collection_point.name} Lv.{self.current_level}"


class PlayerCollectionOverride(models.Model):
    """玩家采集物品池覆盖，可排除或调整特定物品。"""
    player_collection_point = models.ForeignKey(PlayerCollectionPoint, on_delete=models.CASCADE,
                                                related_name="overrides", verbose_name="玩家采集点")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, verbose_name="物品")
    override_weight = models.IntegerField("覆盖权重", null=True, blank=True,
                                          help_text="null=使用等级默认值")
    override_min_amount = models.IntegerField("覆盖最小数量", null=True, blank=True,
                                              help_text="null=使用等级默认值")
    override_max_amount = models.IntegerField("覆盖最大数量", null=True, blank=True,
                                              help_text="null=使用等级默认值")
    is_excluded = models.BooleanField("排除", default=False,
                                      help_text="True=从该玩家采集池中排除此物品")

    class Meta:
        verbose_name = "玩家采集覆盖"
        verbose_name_plural = "玩家采集覆盖"
        unique_together = [("player_collection_point", "item")]

    def __str__(self):
        status = "排除" if self.is_excluded else "覆盖"
        return f"{self.player_collection_point} - {self.item.item_name} ({status})"
