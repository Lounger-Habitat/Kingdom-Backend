from django.contrib import admin

from .models import (
    NpcActionReservation,
    NpcCommandState,
    NpcNeedsDaySummary,
    NpcProfile,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageActionReceipt,
    VillageCommunityMealDailyUsage,
    VillageDeliveryOutbox,
    VillageLocationDefinition,
    VillageSimulationConfig,
    VillageWorldEvent,
)


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


@admin.register(NpcRuntimeState)
class NpcRuntimeStateAdmin(admin.ModelAdmin):
    list_display = (
        "npc", "action_status", "current_location_code", "state_version",
        "needs_world_minute", "hunger_fp", "energy_fp", "health_fp", "updated_at",
    )
    search_fields = ("npc__agent_id", "npc__name")
    list_filter = ("action_status",)
    readonly_fields = ("state_version", "time_revision", "needs_world_minute")


@admin.register(NpcNeedsDaySummary)
class NpcNeedsDaySummaryAdmin(admin.ModelAdmin):
    list_display = ("npc", "day_index", "sleep_minutes", "successful_meals", "severe_hunger_minutes", "injured", "finalized")
    search_fields = ("npc__agent_id", "npc__name")
    list_filter = ("injured", "finalized")
    readonly_fields = ("updated_at",)


@admin.register(NpcSocialDailyCounter)
class NpcSocialDailyCounterAdmin(admin.ModelAdmin):
    list_display = ("low_npc", "high_npc", "day_index", "socialize_count", "updated_at")
    search_fields = ("low_npc__agent_id", "high_npc__agent_id")
    readonly_fields = ("updated_at",)


@admin.register(VillageCommunityMealDailyUsage)
class VillageCommunityMealDailyUsageAdmin(admin.ModelAdmin):
    list_display = ("day_index", "total_meals", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(NpcCommandState)
class NpcCommandStateAdmin(admin.ModelAdmin):
    list_display = ("command_id", "npc", "kind", "status", "created_at")
    search_fields = ("command_id", "npc__agent_id", "sender_id")
    list_filter = ("status", "kind", "queue_mode")
    readonly_fields = ("command_id", "message_id", "request_id", "payload_sha256")


@admin.register(VillageActionReceipt)
class VillageActionReceiptAdmin(admin.ModelAdmin):
    list_display = ("request_id", "action_kind", "success", "result_code", "world_minute")
    search_fields = ("request_id", "result_code")
    list_filter = ("success", "action_kind")
    readonly_fields = ("request_id",)


@admin.register(VillageWorldEvent)
class VillageWorldEventAdmin(admin.ModelAdmin):
    list_display = ("event_sequence", "event_type", "world_minute", "visibility", "created_at")
    search_fields = ("event_id", "event_type")
    list_filter = ("event_type", "visibility")
    readonly_fields = ("event_sequence", "event_id")


@admin.register(NpcActionReservation)
class NpcActionReservationAdmin(admin.ModelAdmin):
    list_display = ("reservation_id", "action_kind", "status", "acquired_world_minute")
    search_fields = ("reservation_id",)
    list_filter = ("status", "action_kind")


@admin.register(VillageDeliveryOutbox)
class VillageDeliveryOutboxAdmin(admin.ModelAdmin):
    list_display = ("delivery_id", "recipient", "message_kind", "status", "attempt_count", "created_at")
    search_fields = ("delivery_id", "recipient")
    list_filter = ("status", "message_kind")
