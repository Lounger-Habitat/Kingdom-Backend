from django.contrib import admin

from .models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcActionReservation,
    NpcCommandState,
    NpcNeedsDaySummary,
    NpcProfile,
    NpcCommitment,
    NpcRelationship,
    NpcRelationshipChange,
    NpcRuntimeState,
    NpcStoryFactAccess,
    NpcSocialDailyCounter,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageActionDurationConfig,
    VillageActionReceipt,
    VillageCommunityMealDailyUsage,
    VillageDeliveryOutbox,
    VillageFarmPlot,
    VillageLocationDefinition,
    VillageRestaurantState,
    VillageSimulationConfig,
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageTradeOffer,
    VillageWorldEvent,
    PlayerStoryFactAccess,
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


@admin.register(VillageActionDurationConfig)
class VillageActionDurationConfigAdmin(admin.ModelAdmin):
    list_display = (
        "action_code", "action_label", "phase", "explanation",
        "duration_minutes", "navigation_timeout_seconds", "updated_at",
    )
    list_editable = ("duration_minutes", "navigation_timeout_seconds")
    readonly_fields = ("action_name", "updated_at")
    ordering = ("action_name",)

    @admin.display(description="动作代码", ordering="action_name")
    def action_code(self, obj):
        return obj.action_name

    @admin.display(description="中文动作")
    def action_label(self, obj):
        return VillageActionDurationConfig.metadata(obj.action_name)[1]

    @admin.display(description="阶段")
    def phase(self, obj):
        return VillageActionDurationConfig.metadata(obj.action_name)[0]

    @admin.display(description="中文说明")
    def explanation(self, obj):
        return VillageActionDurationConfig.metadata(obj.action_name)[2]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    list_display = (
        "event_sequence", "event_type", "visibility", "actor_npc", "location_code",
        "world_minute", "participant_npc_ids", "witness_npc_ids", "created_at",
    )
    search_fields = ("event_id", "event_type", "actor_npc__agent_id", "location_code")
    list_filter = ("event_type", "visibility")
    readonly_fields = tuple(field.name for field in VillageWorldEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NpcActionReservation)
class NpcActionReservationAdmin(admin.ModelAdmin):
    list_display = ("reservation_id", "action_kind", "status", "acquired_world_minute")
    search_fields = ("reservation_id",)
    list_filter = ("status", "action_kind")


@admin.register(VillageDeliveryOutbox)
class VillageDeliveryOutboxAdmin(admin.ModelAdmin):
    list_display = (
        "delivery_id", "message_kind", "recipient", "status", "attempt_count",
        "last_sent_at", "acked_at", "last_error_code", "created_at",
    )
    search_fields = ("delivery_id", "recipient", "last_error_code")
    list_filter = ("status", "message_kind")
    readonly_fields = tuple(field.name for field in VillageDeliveryOutbox._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ── Phase 3 admin registrations ────────────────────────────────────


@admin.register(EconomicLedgerEntry)
class EconomicLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("request_id", "entry_type", "actor_npc", "external_delta", "world_minute", "created_at")
    search_fields = ("request_id",)
    list_filter = ("entry_type",)
    readonly_fields = ("request_id", "created_at")


@admin.register(VillageAccount)
class VillageAccountAdmin(admin.ModelAdmin):
    list_display = ("account_code", "balance", "asset_version", "last_caravan_day", "last_closed_day_index", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(VillageFarmPlot)
class VillageFarmPlotAdmin(admin.ModelAdmin):
    list_display = ("plot_code", "status", "crop_code", "planted_world_minute", "mature_world_minute", "updated_at")
    list_filter = ("status",)
    readonly_fields = ("updated_at",)


@admin.register(NpcTradeOrder)
class NpcTradeOrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "side", "npc", "item_id", "amount", "unit_price", "status", "created_at")
    search_fields = ("order_id", "request_id")
    list_filter = ("side", "status")
    readonly_fields = ("order_id", "created_at")


@admin.register(NpcTradeDelivery)
class NpcTradeDeliveryAdmin(admin.ModelAdmin):
    list_display = ("delivery_id", "buyer_order", "seller_order", "reserved_amount", "reserved_money", "status", "created_at")
    search_fields = ("delivery_id",)
    list_filter = ("status",)
    readonly_fields = ("delivery_id", "created_at")


@admin.register(DailyPriceSnapshot)
class DailyPriceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("day_index", "status", "published_by_npc_id", "published_world_minute", "created_at")
    list_filter = ("status",)
    readonly_fields = ("created_at",)


@admin.register(VillageRestaurantState)
class VillageRestaurantStateAdmin(admin.ModelAdmin):
    list_display = ("day_index", "status", "traveler_sales_count", "traveler_revenue", "updated_at")
    list_filter = ("status",)
    readonly_fields = ("updated_at",)


@admin.register(VillageStore)
class VillageStoreAdmin(admin.ModelAdmin):
    list_display = ("store_code", "display_name", "location_code", "operator_npc", "status", "transaction_fee_bps", "minimum_fee", "inventory_version", "updated_at")
    search_fields = ("store_code", "display_name", "operator_npc__agent_id")
    list_filter = ("status",)
    readonly_fields = ("store_code", "updated_at")


@admin.register(VillageStoreAccount)
class VillageStoreAccountAdmin(admin.ModelAdmin):
    list_display = ("store", "balance", "account_version", "updated_at")
    search_fields = ("store__store_code",)
    readonly_fields = ("store", "updated_at")


@admin.register(VillageStoreInventorySlot)
class VillageStoreInventorySlotAdmin(admin.ModelAdmin):
    list_display = ("instance_id", "store", "listing", "item_id", "item_amount", "reserved_amount", "expires_world_minute")
    search_fields = ("instance_id", "store__store_code")
    list_filter = ("store",)
    readonly_fields = ("instance_id",)


@admin.register(VillageStoreListing)
class VillageStoreListingAdmin(admin.ModelAdmin):
    list_display = ("listing_id", "store", "item_id", "price_mode", "unit_price", "status", "listed_by_npc", "listed_world_minute", "updated_at")
    search_fields = ("listing_id", "store__store_code", "listed_by_npc__agent_id")
    list_filter = ("status", "price_mode", "store")
    readonly_fields = ("listing_id", "updated_at")


@admin.register(VillageTradeOffer)
class VillageTradeOfferAdmin(admin.ModelAdmin):
    list_display = ("offer_id", "buyer_npc", "seller_npc", "item_id", "amount", "gold_amount", "status", "location_code", "expires_world_minute", "created_at")
    search_fields = ("offer_id", "buyer_npc__agent_id", "seller_npc__agent_id")
    list_filter = ("status",)
    readonly_fields = ("offer_id", "created_at")


@admin.register(NpcRelationship)
class NpcRelationshipAdmin(admin.ModelAdmin):
    list_display = ("from_npc", "to_npc", "affinity", "trust", "obligation", "conflict", "version", "updated_at")
    search_fields = ("from_npc__agent_id", "to_npc__agent_id")
    readonly_fields = tuple(field.name for field in NpcRelationship._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NpcRelationshipChange)
class NpcRelationshipChangeAdmin(admin.ModelAdmin):
    list_display = (
        "change_id", "relationship", "category", "world_day", "requested_delta",
        "applied_delta", "before_values", "after_values", "source_event", "created_at",
    )
    search_fields = ("change_id", "relationship__from_npc__agent_id", "relationship__to_npc__agent_id")
    list_filter = ("category", "world_day")
    readonly_fields = ("change_id", "relationship", "source_event", "category", "world_day", "requested_delta", "applied_delta", "before_values", "after_values", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NpcCommitment)
class NpcCommitmentAdmin(admin.ModelAdmin):
    list_display = (
        "commitment_id", "commitment_type", "promisor_npc", "beneficiary_npc",
        "status", "target", "created_world_minute", "due_world_minute",
        "settled_world_minute", "created_event", "settled_event",
    )
    search_fields = ("commitment_id", "promisor_npc__agent_id", "beneficiary_npc__agent_id")
    list_filter = ("commitment_type", "status")
    readonly_fields = tuple(field.name for field in NpcCommitment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NpcStoryFactAccess)
class NpcStoryFactAccessAdmin(admin.ModelAdmin):
    list_display = ("npc", "fact_code", "unlocked_world_minute", "is_public", "unlocked_by_event")
    search_fields = ("npc__agent_id", "fact_code")
    list_filter = ("is_public",)
    readonly_fields = tuple(field.name for field in NpcStoryFactAccess._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlayerStoryFactAccess)
class PlayerStoryFactAccessAdmin(admin.ModelAdmin):
    list_display = ("player_id", "fact_code", "unlocked_world_minute", "unlocked_by_event")
    search_fields = ("player_id", "fact_code")
    readonly_fields = tuple(field.name for field in PlayerStoryFactAccess._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
