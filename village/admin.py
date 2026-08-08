from django.contrib import admin

from .models import NpcProfile, VillageLocationDefinition, VillageSimulationConfig


@admin.register(NpcProfile)
class NpcProfileAdmin(admin.ModelAdmin):
    list_display = ("npc", "display_name", "occupation", "is_active")
    search_fields = ("npc__agent_id", "npc__name", "display_name", "occupation")
    list_filter = ("occupation", "is_active")


@admin.register(VillageLocationDefinition)
class VillageLocationDefinitionAdmin(admin.ModelAdmin):
    list_display = ("location_code", "display_name", "location_type", "scene_name", "is_active")
    search_fields = ("location_code", "display_name", "center_anchor_code")
    list_filter = ("scene_name", "location_type", "is_active")


@admin.register(VillageSimulationConfig)
class VillageSimulationConfigAdmin(admin.ModelAdmin):
    list_display = ("config_key", "village_simulation_enabled", "updated_at")

    def has_add_permission(self, request):
        return not VillageSimulationConfig.objects.exists()
