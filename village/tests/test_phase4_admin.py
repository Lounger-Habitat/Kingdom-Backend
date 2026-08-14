from django.test import SimpleTestCase

from village.admin import (
    NpcCommitmentAdmin,
    NpcRelationshipAdmin,
    NpcRelationshipChangeAdmin,
    NpcStoryFactAccessAdmin,
    PlayerStoryFactAccessAdmin,
    VillageDeliveryOutboxAdmin,
    VillageWorldEventAdmin,
)
from village.models import (
    NpcCommitment,
    NpcRelationship,
    NpcRelationshipChange,
    NpcStoryFactAccess,
    PlayerStoryFactAccess,
    VillageDeliveryOutbox,
    VillageWorldEvent,
)


class PhaseFourAdminAuditTest(SimpleTestCase):
    def test_phase_four_admins_are_read_only(self):
        cases = (
            (VillageWorldEventAdmin, VillageWorldEvent),
            (VillageDeliveryOutboxAdmin, VillageDeliveryOutbox),
            (NpcRelationshipAdmin, NpcRelationship),
            (NpcRelationshipChangeAdmin, NpcRelationshipChange),
            (NpcCommitmentAdmin, NpcCommitment),
            (NpcStoryFactAccessAdmin, NpcStoryFactAccess),
            (PlayerStoryFactAccessAdmin, PlayerStoryFactAccess),
        )
        for admin_class, model in cases:
            admin = admin_class(model, None)
            self.assertFalse(admin.has_add_permission(None))
            self.assertFalse(admin.has_change_permission(None, None))
            self.assertFalse(admin.has_delete_permission(None, None))
            self.assertEqual(
                set(admin.readonly_fields),
                {field.name for field in model._meta.fields},
            )
