"""Editable equipment species, five-level curves and weapon move coefficients."""
import math
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


def validate_materials(value):
    if not isinstance(value, list):
        raise ValidationError("材料必须是列表，例如 [{\"item_id\":1002,\"quantity\":2}]。")
    seen = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"item_id", "quantity"}
                or any(type(row[k]) is not int or row[k] <= 0 for k in row)
                or row["item_id"] in seen):
            raise ValidationError("每项材料需要唯一的正整数 item_id 和正整数 quantity。")
        seen.add(row["item_id"])


class EquipmentDefinition(models.Model):
    CATEGORIES = [("sword", "剑"), ("spear", "长矛"), ("bow", "弓"), ("axe", "斧"),
                  ("gathering", "采集包"), ("farming", "农具"), ("cooking", "炊具"), ("processing", "加工猎具")]
    item = models.OneToOneField("items.Item", verbose_name="物品品种", on_delete=models.CASCADE, related_name="equipment_definition")
    category = models.CharField("类别", max_length=20, choices=CATEGORIES)
    advanced = models.BooleanField("进阶品种", default=False)
    enabled = models.BooleanField("启用", default=True)
    required_skill = models.CharField("对应技能", max_length=20, choices=[("gathering", "采集"), ("farming", "种植"),
        ("forestry", "伐木"), ("hunting", "狩猎"), ("cooking", "烹饪"), ("", "NPC生产工具")], blank=True)

    class Meta:
        verbose_name = "装备品种"
        verbose_name_plural = "装备品种与五级属性"
        ordering = ["category", "advanced", "item_id"]

    def __str__(self):
        return self.item.item_name


class EquipmentLevel(models.Model):
    equipment = models.ForeignKey(EquipmentDefinition, verbose_name="装备品种", on_delete=models.CASCADE, related_name="levels")
    level = models.PositiveSmallIntegerField("装备等级", validators=[MinValueValidator(1), MaxValueValidator(5)])
    required_skill_level = models.PositiveSmallIntegerField("技能门槛", default=1, validators=[MinValueValidator(1), MaxValueValidator(10)])
    max_durability = models.PositiveIntegerField("最大耐久", validators=[MinValueValidator(1)])
    attack_min = models.PositiveIntegerField("基础攻击下限", default=0)
    attack_max = models.PositiveIntegerField("基础攻击上限", default=0)
    gathering_time_reduction = models.FloatField("采摘减时比例", default=0, validators=[MinValueValidator(0), MaxValueValidator(.95)])
    quality_bonus_chance = models.FloatField("提升一星概率", default=0, validators=[MinValueValidator(0), MaxValueValidator(1)])
    harvest_bonus_chance = models.FloatField("额外一份收获概率", default=0, validators=[MinValueValidator(0), MaxValueValidator(1)])
    chop_power = models.FloatField("每击伐木进度", default=1, validators=[MinValueValidator(.1), MaxValueValidator(100)])
    cooking_batches = models.PositiveSmallIntegerField("单次烹饪批数", default=1, validators=[MinValueValidator(1), MaxValueValidator(5)])
    processing_time_reduction = models.FloatField("加工减时比例", default=0, validators=[MinValueValidator(0), MaxValueValidator(.95)])
    upgrade_gold = models.PositiveIntegerField("升到本级金币", default=0)
    upgrade_materials = models.JSONField("升到本级材料", default=list, blank=True, validators=[validate_materials])
    repair_gold = models.PositiveIntegerField("维修金币", default=5)
    repair_materials = models.JSONField("维修材料", default=list, blank=True, validators=[validate_materials])

    class Meta:
        verbose_name = "装备等级属性"
        verbose_name_plural = "装备等级属性"
        ordering = ["equipment_id", "level"]
        constraints = [models.UniqueConstraint(fields=["equipment", "level"], name="equipment_unique_level"),
            models.CheckConstraint(condition=models.Q(level__gte=1, level__lte=5), name="equipment_level_1_5"),
            models.CheckConstraint(condition=models.Q(attack_max__gte=models.F("attack_min")), name="equipment_attack_range"),
            models.CheckConstraint(condition=models.Q(max_durability__gt=0), name="equipment_positive_durability")]

    def clean(self):
        super().clean()
        for field in self._meta.fields:
            value = getattr(self, field.name)
            if isinstance(field, models.FloatField) and isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValidationError({field.name: "必须填写有限数值。"})
        if self.attack_min is not None and self.attack_max is not None and self.attack_min > self.attack_max:
            raise ValidationError({"attack_max": "攻击上限不能低于下限。"})
        if self.equipment_id and self.equipment.category in {"sword", "spear", "bow", "axe"} and self.attack_min is not None and self.attack_min < 1:
            raise ValidationError({"attack_min": "武器基础攻击必须大于0。"})
        from items.models import Item
        for field in ("upgrade_materials", "repair_materials"):
            value = getattr(self, field)
            if value is None:
                value = []
                setattr(self, field, value)
            validate_materials(value)
            ids = {row["item_id"] for row in value}
            if ids - set(Item.objects.filter(pk__in=ids, is_deleted=False).values_list("pk", flat=True)):
                raise ValidationError({field: "材料包含不存在或已停用的物品。"})

    def __str__(self):
        return f"{self.equipment} Lv.{self.level}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.level == 1:
            from items.models import Item
            from django.utils import timezone
            Item.objects.filter(pk=self.equipment.item_id).update(max_use_count=self.max_durability, updated_at=timezone.now())


class WeaponMove(models.Model):
    code = models.CharField("招式代码", max_length=30, unique=True)
    name = models.CharField("招式名称", max_length=60)
    category = models.CharField("武器类别", max_length=20, choices=EquipmentDefinition.CATEGORIES[:4])
    multiplier = models.FloatField("伤害倍率／满弦倍率", validators=[MinValueValidator(.01), MaxValueValidator(100)])
    minimum_multiplier = models.FloatField("未蓄力倍率（弓）", default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])

    class Meta:
        verbose_name = "武器招式倍率"
        verbose_name_plural = "武器招式倍率"
        ordering = ["category", "code"]

    def clean(self):
        if any(isinstance(value, (int, float)) and not math.isfinite(value) for value in (self.multiplier, self.minimum_multiplier)):
            raise ValidationError("倍率必须为有限数值。")
        if self.minimum_multiplier is not None and self.multiplier is not None and self.minimum_multiplier > self.multiplier:
            raise ValidationError({"minimum_multiplier": "未蓄力倍率不能超过满弦倍率。"})

    def __str__(self):
        return self.name
