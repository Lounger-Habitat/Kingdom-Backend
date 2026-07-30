from django.contrib import admin
from django.shortcuts import redirect
from .models import (
    InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot,
    CollectionPoint, CollectionPointLevel, CollectionPointLevelItem,
    CollectionPointUpgradeItemCost, PlayerCollectionPoint, PlayerCollectionOverride,
    AiConfig,
    NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot,
)


class InventoryBagProxy(InventoryBag):
    """代理模型，仅用于在 Admin 侧边栏显示'背包管理'入口。"""
    class Meta:
        proxy = True
        verbose_name = "背包管理"
        verbose_name_plural = "背包管理"


@admin.register(InventoryBagProxy)
class InventoryBagManagerAdmin(admin.ModelAdmin):
    """点击侧边栏'背包管理'后跳转到自定义页面。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect("inventory-admin-player-list")


class InventorySlotInline(admin.TabularInline):
    model = InventorySlot
    extra = 0
    fields = ["slot_index", "item_id", "item_amount", "rated", "rating_price", "overall_score", "ingredient", "item_bag_name", "cook_time", "dish_quality", "hide_in_shop"]
    ordering = ["slot_index"]


@admin.register(InventoryBag)
class InventoryBagAdmin(admin.ModelAdmin):
    list_display = ["character_id", "player", "money", "slot_count"]
    search_fields = ["character_id", "player__username"]
    list_filter = ["character_id", "player"]
    ordering = ["character_id", "player"]
    inlines = [InventorySlotInline]

    def slot_count(self, obj):
        return obj.slots.count()

    slot_count.short_description = "格子数"


# InventorySlot 通过 InventoryBag 的 inline 管理，不单独显示


class BagTemplateSlotInline(admin.TabularInline):
    model = BagTemplateSlot
    extra = 0
    fields = ["slot_index", "item", "item_amount", "rated", "rating_price", "overall_score", "ingredient", "cook_time", "dish_quality", "hide_in_shop"]
    ordering = ["slot_index"]
    autocomplete_fields = ["item"]


@admin.register(NpcDefinition)
class NpcDefinitionAdmin(admin.ModelAdmin):
    list_display = ["agent_id", "name"]
    search_fields = ["agent_id", "name"]


@admin.register(BagTemplate)
class BagTemplateAdmin(admin.ModelAdmin):
    list_display = ["display_name", "money", "slot_count"]
    search_fields = ["npc__agent_id", "npc__name", "character_id"]
    list_filter = ["npc"]
    inlines = [BagTemplateSlotInline]

    class Media:
        js = ("inventory/js/auto_slot_index.js",)

    def display_name(self, obj):
        if obj.npc:
            return f"{obj.npc.name} ({obj.npc.agent_id})"
        return obj.character_id
    display_name.short_description = "模板归属"

    def slot_count(self, obj):
        return obj.slots.count()

    slot_count.short_description = "物品数"


# BagTemplateSlot 通过 BagTemplate 的 inline 管理，不单独显示


# ──────────────────────────────────────────────────────────────
#  采集点等级系统 Admin
# ──────────────────────────────────────────────────────────────

class CollectionPointLevelInline(admin.TabularInline):
    model = CollectionPointLevel
    extra = 0
    fields = ["level", "description", "upgrade_cost_money"]
    show_change_link = True


@admin.register(CollectionPoint)
class CollectionPointAdmin(admin.ModelAdmin):
    list_display = ["name", "collect_type", "item_type", "action_cost", "collect_count", "default_level"]
    list_filter = ["item_type"]
    search_fields = ["name", "collect_type"]
    inlines = [CollectionPointLevelInline]


class CollectionPointLevelItemInline(admin.TabularInline):
    model = CollectionPointLevelItem
    extra = 0
    fields = ["item", "collect_weight", "collect_min_amount", "collect_max_amount"]
    autocomplete_fields = ["item"]


class CollectionPointUpgradeItemCostInline(admin.TabularInline):
    model = CollectionPointUpgradeItemCost
    extra = 0
    fields = ["item", "amount"]
    autocomplete_fields = ["item"]


@admin.register(CollectionPointLevel)
class CollectionPointLevelAdmin(admin.ModelAdmin):
    list_display = ["collection_point", "level", "description", "item_count", "upgrade_cost_money"]
    list_filter = ["collection_point"]
    inlines = [CollectionPointLevelItemInline, CollectionPointUpgradeItemCostInline]

    def item_count(self, obj):
        return obj.items.count()

    item_count.short_description = "物品数"


class PlayerCollectionOverrideInline(admin.TabularInline):
    model = PlayerCollectionOverride
    extra = 0
    fields = ["item", "override_weight", "override_min_amount", "override_max_amount", "is_excluded"]
    autocomplete_fields = ["item"]


@admin.register(PlayerCollectionPoint)
class PlayerCollectionPointAdmin(admin.ModelAdmin):
    list_display = ["player", "collection_point", "current_level"]
    list_filter = ["collection_point", "current_level"]
    search_fields = ["player__username"]
    inlines = [PlayerCollectionOverrideInline]


@admin.register(PlayerCollectionOverride)
class PlayerCollectionOverrideAdmin(admin.ModelAdmin):
    list_display = ["player_name", "collection_point_name", "item", "override_weight", "override_min_amount", "override_max_amount", "is_excluded"]
    list_filter = ["player_collection_point__collection_point", "is_excluded"]
    search_fields = ["player_collection_point__player__username", "item__item_name"]
    autocomplete_fields = ["item"]

    def player_name(self, obj):
        return obj.player_collection_point.player.username
    player_name.short_description = "玩家"

    def collection_point_name(self, obj):
        return obj.player_collection_point.collection_point.name
    collection_point_name.short_description = "采集点"


@admin.register(AiConfig)
class AiConfigAdmin(admin.ModelAdmin):
    list_display = ["model_name", "updated_at"]

    def has_add_permission(self, request):
        # 只允许添加一条配置
        return not AiConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# ──────────────────────────────────────────────────────────────
#  NPC 全局库存 Admin
# ──────────────────────────────────────────────────────────────

class NpcGlobalInventorySlotInline(admin.TabularInline):
    model = NpcGlobalInventorySlot
    extra = 0
    fields = ["slot_index", "item_id", "item_amount", "rated", "rating_price", "overall_score", "ingredient", "item_bag_name", "cook_time", "dish_quality", "hide_in_shop"]
    ordering = ["slot_index"]


@admin.register(NpcGlobalInventory)
class NpcGlobalInventoryAdmin(admin.ModelAdmin):
    list_display = ["npc_name", "npc_agent_id", "money", "slot_count", "updated_at"]
    search_fields = ["npc__agent_id", "npc__name"]
    inlines = [NpcGlobalInventorySlotInline]

    def npc_name(self, obj):
        return obj.npc.name
    npc_name.short_description = "NPC"

    def npc_agent_id(self, obj):
        return obj.npc.agent_id
    npc_agent_id.short_description = "Agent ID"

    def slot_count(self, obj):
        return obj.slots.count()

    slot_count.short_description = "物品数"


# NpcGlobalInventorySlot 通过 NpcGlobalInventory 的 inline 管理，不单独显示
