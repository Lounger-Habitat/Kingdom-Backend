"""Read-only audit of NPC-authored commissions and their escrow."""
from django.contrib import admin

from .models import (VillagePersonalCommission, VillagePersonalCommissionEscrow,
                     VillagePersonalCommissionReceipt)


class CommissionAuditAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(VillagePersonalCommission)
class PersonalCommissionAdmin(CommissionAuditAdmin):
    list_display = ("title", "publisher", "status", "actor_type", "actor_id", "reward_gold",
                    "reserved_reward_gold", "escrow_gold", "expires_world_minute")
    list_filter = ("status", "actor_type", "publisher")
    search_fields = ("commission_id", "title", "publisher__name", "publisher__agent_id", "actor_id")
    ordering = ("-opened_world_minute", "commission_id")


@admin.register(VillagePersonalCommissionEscrow)
class PersonalCommissionEscrowAdmin(CommissionAuditAdmin):
    list_display = ("commission", "condition_id", "item_id", "quantity", "status", "source_instance_id")
    list_filter = ("status",)
    search_fields = ("commission__commission_id", "commission__title", "condition_id", "source_instance_id")


@admin.register(VillagePersonalCommissionReceipt)
class PersonalCommissionReceiptAdmin(CommissionAuditAdmin):
    list_display = ("commission", "actor_type", "actor_id", "action", "request_id", "world_minute")
    list_filter = ("action", "actor_type")
    search_fields = ("commission__commission_id", "request_id", "actor_id")
    ordering = ("-world_minute", "-id")
