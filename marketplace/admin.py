from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django import forms
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.safestring import mark_safe
from datetime import timedelta
from account.models import GameAccount
from inventory.models import InventoryBag
from items.models import Item
from .models import MarketListing, MarketTransaction, MarketListingSource, MarketListingStatus
from . import services


@staff_member_required
def admin_buy_view(request, listing_id):
    listing = get_object_or_404(MarketListing, pk=listing_id)

    if listing.status != MarketListingStatus.ACTIVE:
        messages.error(request, "该挂单不在出售中状态")
        return redirect("admin:marketplace_marketlisting_changelist")

    # Get accounts with their money info
    accounts = []
    for acc in GameAccount.objects.order_by("username"):
        try:
            bag = InventoryBag.objects.get(player=acc, character_id=acc.username)
            acc.money = bag.money
        except InventoryBag.DoesNotExist:
            acc.money = 0
        accounts.append(acc)

    if request.method == "POST":
        buyer_id = request.POST.get("buyer_id")
        amount = request.POST.get("amount")

        if not buyer_id:
            messages.error(request, "请选择买家账号")
        else:
            try:
                buyer = GameAccount.objects.get(pk=buyer_id)
                amount = int(amount)
                result, err = services.admin_buy_listing(buyer, listing.pk, amount)
                if err:
                    messages.error(request, f"购买失败：{err}")
                else:
                    messages.success(request, f"购买成功！{buyer.username} 获得 {amount} 个 {listing.item_name_snapshot}，花费 {result['transaction'].total_price} 金币")
                    return redirect("admin:marketplace_marketlisting_changelist")
            except (GameAccount.DoesNotExist, ValueError) as e:
                messages.error(request, f"参数错误：{e}")

    context = {
        **admin.site.each_context(request),
        "listing": listing,
        "accounts": accounts,
        "selected_buyer": request.POST.get("buyer_id", ""),
        "title": f"代购挂单 #{listing.id}",
    }
    return render(request, "admin/marketplace/marketlisting/buy.html", context)


class ItemSelectWidget(forms.Widget):
    """Select2 widget with AJAX autocomplete and pinyin search."""

    class Media:
        css = {"all": ("admin/css/vendor/select2/select2.min.css",)}
        js = ("admin/js/vendor/select2/select2.full.min.js",)

    def render(self, name, value, attrs=None, renderer=None):
        if value:
            try:
                item = Item.objects.get(item_id=value)
                initial_text = f"[{item.item_id}] {item.item_name}"
            except Item.DoesNotExist:
                initial_text = str(value)
        else:
            initial_text = ""

        html = f"""
        <style>
        [data-theme="dark"] .select2-container--default .select2-selection--single,
        [data-theme="dark"] .select2-container--default .select2-selection--single .select2-selection__rendered,
        [data-theme="dark"] .select2-dropdown,
        [data-theme="dark"] .select2-search__field,
        [data-theme="dark"] .select2-container--default .select2-results__option {{
            background-color: #303030 !important;
            color: #e0e0e0 !important;
            border-color: #545454 !important;
        }}
        [data-theme="dark"] .select2-container--default .select2-results__option--highlighted,
        [data-theme="dark"] .select2-container--default .select2-results__option[aria-selected="true"] {{
            background-color: #404040 !important;
            color: #e0e0e0 !important;
        }}
        [data-theme="dark"] .select2-container--default .select2-selection--single .select2-selection__placeholder {{
            color: #9e9e9e !important;
        }}
        [data-theme="dark"] .select2-container--default .select2-selection--single .select2-selection__arrow b {{
            border-color: #e0e0e0 transparent transparent transparent !important;
        }}
        [data-theme="dark"] .select2-container--default.select2-container--open .select2-selection--single .select2-selection__arrow b {{
            border-color: transparent transparent #e0e0e0 transparent !important;
        }}
        @media (prefers-color-scheme: dark) {{
            [data-theme="auto"] .select2-container--default .select2-selection--single,
            [data-theme="auto"] .select2-container--default .select2-selection--single .select2-selection__rendered,
            [data-theme="auto"] .select2-dropdown,
            [data-theme="auto"] .select2-search__field,
            [data-theme="auto"] .select2-container--default .select2-results__option {{
                background-color: #303030 !important;
                color: #e0e0e0 !important;
                border-color: #545454 !important;
            }}
            [data-theme="auto"] .select2-container--default .select2-results__option--highlighted,
            [data-theme="auto"] .select2-container--default .select2-results__option[aria-selected="true"] {{
                background-color: #404040 !important;
                color: #e0e0e0 !important;
            }}
            [data-theme="auto"] .select2-container--default .select2-selection--single .select2-selection__placeholder {{
                color: #9e9e9e !important;
            }}
            [data-theme="auto"] .select2-container--default .select2-selection--single .select2-selection__arrow b {{
                border-color: #e0e0e0 transparent transparent transparent !important;
            }}
            [data-theme="auto"] .select2-container--default.select2-container--open .select2-selection--single .select2-selection__arrow b {{
                border-color: transparent transparent #e0e0e0 transparent !important;
            }}
        }}
        </style>
        <input type="hidden" name="{name}" id="id_{name}" value="{value or ''}">
        <select id="id_{name}_select" style="width: 400px;" class="item-select2">
            <option value=""></option>
            <option value="{value or ''}" selected>{initial_text}</option>
        </select>
        <script>
        (function() {{
            function initItemSelect2() {{
                var $ = django.jQuery;
                if (!$ || !$.fn.select2) {{
                    setTimeout(initItemSelect2, 100);
                    return;
                }}
                var $select = $('#id_{name}_select');
                var $hidden = $('#id_{name}');

                // Pre-fetch initial page of items
                $.ajax({{
                    url: '/admin/item-autocomplete/',
                    data: {{ term: '', page: 1 }},
                    dataType: 'json'
                }}).then(function(data) {{
                    data.results.forEach(function(item) {{
                        var option = new Option(item.text, item.id, false, false);
                        $select.append(option);
                    }});
                    $select.select2({{
                        ajax: {{
                            url: '/admin/item-autocomplete/',
                            dataType: 'json',
                            delay: 200,
                            data: function(params) {{
                                return {{ term: params.term || '', page: params.page || 1 }};
                            }},
                            processResults: function(data, params) {{
                                return {{
                                    results: data.results,
                                    pagination: {{ more: data.pagination.more }}
                                }};
                            }},
                            cache: true
                        }},
                        minimumInputLength: 0,
                        placeholder: '输入物品名称或拼音首字母搜索...',
                        allowClear: true,
                        language: {{
                            inputTooShort: function() {{ return '输入关键词开始搜索'; }},
                            noResults: function() {{ return '未找到匹配物品'; }},
                            searching: function() {{ return '搜索中...'; }}
                        }}
                    }});
                    $select.on('select2:select', function(e) {{
                        $hidden.val(e.params.data.id);
                    }});
                    $select.on('select2:clear', function(e) {{
                        $hidden.val('');
                    }});
                }});
            }}
            initItemSelect2();
        }})();
        </script>
        """
        return mark_safe(html)

    def value_from_datadict(self, data, files, name):
        return data.get(name)


class MarketListingAdminForm(forms.ModelForm):
    item_id = forms.CharField(
        label="物品",
        widget=ItemSelectWidget(),
        help_text="输入物品名称或拼音首字母搜索",
    )

    class Meta:
        model = MarketListing
        fields = ["source_type", "seller", "item_id", "total_amount", "price"]

    def clean(self):
        cleaned = super().clean()
        source_type = cleaned.get("source_type")

        if source_type == MarketListingSource.PLAYER:
            if not cleaned.get("seller"):
                raise forms.ValidationError("玩家挂单必须填写卖家")
        elif source_type == MarketListingSource.SYSTEM:
            cleaned["seller"] = None

        item_id = cleaned.get("item_id")
        if item_id:
            try:
                cleaned["item_id"] = int(item_id)
            except (ValueError, TypeError):
                raise forms.ValidationError("无效的物品ID")

        if not cleaned.get("total_amount") or cleaned["total_amount"] <= 0:
            raise forms.ValidationError("数量必须大于0")
        if not cleaned.get("price") or cleaned["price"] <= 0:
            raise forms.ValidationError("价格必须大于0")

        return cleaned


@admin.register(MarketListing)
class MarketListingAdmin(admin.ModelAdmin):
    form = MarketListingAdminForm
    list_display = [
        "id", "source_type", "seller_display", "item_id", "item_name_snapshot",
        "price", "total_amount", "remaining_amount", "status", "expires_at", "created_at",
        "buy_action",
    ]
    list_filter = ["source_type", "status", "created_at", "expires_at"]
    search_fields = ["seller__username", "item_name_snapshot", "item_id"]
    readonly_fields = ["item_snapshot", "created_at", "updated_at"]
    actions = ["action_delist_selected"]

    def seller_display(self, obj):
        if obj.source_type == MarketListingSource.SYSTEM:
            return "系统"
        return obj.seller.username if obj.seller else "-"
    seller_display.short_description = "卖家"

    def buy_action(self, obj):
        if obj.status == MarketListingStatus.ACTIVE:
            return mark_safe(
                f'<a href="/admin/marketplace/marketlisting/{obj.pk}/buy/" '
                f'style="background: #417690; color: #fff; padding: 3px 10px; '
                f'border-radius: 3px; text-decoration: none; font-size: 12px;">代购</a>'
            )
        return "-"
    buy_action.short_description = "操作"

    def save_model(self, request, obj, form, change):
        if not change:
            try:
                item = Item.objects.get(item_id=obj.item_id)
                obj.item_name_snapshot = item.item_name
                obj.item_type_snapshot = item.item_type
                obj.icon_url_snapshot = item.icon_url or ""
                snap = services.build_default_item_snapshot(item, obj.total_amount)
                obj.item_snapshot = snap
            except Item.DoesNotExist:
                pass

            obj.remaining_amount = obj.total_amount
            obj.status = MarketListingStatus.ACTIVE
            obj.created_by_admin = request.user

            if obj.source_type == MarketListingSource.SYSTEM:
                obj.seller = None
                obj.expires_at = None
            else:
                obj.expires_at = timezone.now() + timedelta(days=7)

        super().save_model(request, obj, form, change)

    @admin.action(description="下架选中的挂单")
    def action_delist_selected(self, request, queryset):
        count = 0
        for listing in queryset.filter(status=MarketListingStatus.ACTIVE):
            if listing.source_type == MarketListingSource.SYSTEM:
                services.delist_system_listing(listing.pk)
            else:
                if listing.seller:
                    services.delist_listing(listing.seller, listing.pk)
            count += 1
        self.message_user(request, f"已下架 {count} 个挂单")


@admin.register(MarketTransaction)
class MarketTransactionAdmin(admin.ModelAdmin):
    list_display = [
        "id", "listing", "source_type", "seller_display", "buyer",
        "item_id", "amount", "unit_price", "total_price",
        "commission", "seller_earnings", "system_income", "created_at",
    ]
    list_filter = ["source_type", "created_at"]
    search_fields = ["seller__username", "buyer__username", "item_name_snapshot", "item_id"]
    readonly_fields = [
        "listing", "source_type", "seller", "buyer", "item_id",
        "amount", "unit_price", "total_price", "commission",
        "seller_earnings", "system_income", "created_at",
    ]

    def seller_display(self, obj):
        if obj.source_type == MarketListingSource.SYSTEM:
            return "系统"
        return obj.seller.username if obj.seller else "-"
    seller_display.short_description = "卖家"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
