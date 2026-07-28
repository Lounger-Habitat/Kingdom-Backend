from django import forms
from django.contrib import admin

from items.models import ItemType

from .models import AutoBuyConfig, AutoBuyRule, AutoBuyPurchase


class AutoBuyPurchaseInline(admin.TabularInline):
    model = AutoBuyPurchase
    extra = 0
    fields = ["created_at", "buyer", "item_name", "amount", "unit_price", "total_price", "fund_balance"]
    readonly_fields = ["created_at", "buyer", "item_name", "amount", "unit_price", "total_price", "fund_balance"]
    ordering = ["-created_at"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AutoBuyConfig)
class AutoBuyConfigAdmin(admin.ModelAdmin):
    list_display = ["id", "enabled", "interval_minutes", "fund", "updated_at"]
    inlines = [AutoBuyPurchaseInline]

    def has_add_permission(self, request):
        return not AutoBuyConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class AutoBuyRuleForm(forms.ModelForm):
    item_types = forms.MultipleChoiceField(
        choices=ItemType.choices,
        widget=forms.CheckboxSelectMultiple,
        label="物品类型",
    )

    class Meta:
        model = AutoBuyRule
        fields = "__all__"

    def clean_item_types(self):
        return self.cleaned_data["item_types"]


@admin.register(AutoBuyRule)
class AutoBuyRuleAdmin(admin.ModelAdmin):
    form = AutoBuyRuleForm
    list_display = ["id", "buyer", "item_types_display", "max_price_multiplier", "is_active", "created_at"]
    search_fields = ["buyer__username"]
    list_editable = ["is_active", "max_price_multiplier"]

    def item_types_display(self, obj):
        return obj.get_item_types_display()
    item_types_display.short_description = "物品类型"
