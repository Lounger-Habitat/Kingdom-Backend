from django.contrib import admin
from django.utils.html import format_html
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import Item


class ItemResource(resources.ModelResource):
    class Meta:
        model = Item
        import_id_fields = ["item_id"]


@admin.register(Item)
class ItemAdmin(ImportExportModelAdmin):
    resource_class = ItemResource
    list_display = ["item_id", "icon_preview", "item_name", "item_type", "item_price", "collectable", "collect_weight", "collect_min_amount", "collect_max_amount", "is_deleted", "updated_at"]
    list_filter = ["item_type", "is_deleted", "can_picked_up", "can_dropped", "can_carried"]
    search_fields = ["item_name", "item_id", "item_description"]
    readonly_fields = ["icon_preview"]
    actions = ["soft_delete", "restore", "batch_update_price"]

    def icon_preview(self, obj):
        if obj.icon_image:
            return format_html('<img src="{}" style="max-height:40px;"/>', obj.icon_image.url)
        if obj.icon_url:
            return format_html('<img src="{}" style="max-height:40px;"/>', obj.icon_url)
        return "-"

    icon_preview.short_description = "图标"

    def soft_delete(self, request, queryset):
        queryset.update(is_deleted=True)

    soft_delete.short_description = "标记删除"

    def restore(self, request, queryset):
        queryset.update(is_deleted=False)

    restore.short_description = "恢复"

    def batch_update_price(self, request, queryset):
        # Placeholder for batch price update - can be customized
        pass

    batch_update_price.short_description = "批量调价"
