from django.contrib import admin
from django.shortcuts import redirect
from .models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot


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


@admin.register(BagTemplate)
class BagTemplateAdmin(admin.ModelAdmin):
    list_display = ["character_id", "money", "slot_count"]
    search_fields = ["character_id"]
    inlines = [BagTemplateSlotInline]

    class Media:
        js = ("inventory/js/auto_slot_index.js",)

    def slot_count(self, obj):
        return obj.slots.count()

    slot_count.short_description = "物品数"


# BagTemplateSlot 通过 BagTemplate 的 inline 管理，不单独显示
