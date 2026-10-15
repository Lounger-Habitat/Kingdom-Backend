from django.contrib import admin
from .models import VillageDailyEvent, VillageDailyEventTemplate


@admin.register(VillageDailyEventTemplate)
class VillageDailyEventTemplateAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "rule_type", "npc_scope", "enabled", "invitation_minutes", "cooldown_minutes")
    list_filter = ("enabled", "npc_scope", "rule_type")
    search_fields = ("code", "title")
    list_editable = ("npc_scope", "enabled", "invitation_minutes", "cooldown_minutes")


@admin.register(VillageDailyEvent)
class VillageDailyEventAdmin(admin.ModelAdmin):
    list_display = ("title", "template", "status", "npc_id", "opened_world_minute", "invite_until_world_minute", "closed_world_minute")
    list_filter = ("status", "template")
    search_fields = ("title", "source_id", "npc_id", "affair_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
