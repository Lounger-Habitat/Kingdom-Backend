from django.contrib.auth import get_user_model
from django.test import TestCase

class CommissionAuditAdminTests(TestCase):
    def test_audit_models_are_visible_but_not_editable(self):
        user = get_user_model().objects.create_superuser("commission-auditor", password="test-password")
        self.client.force_login(user)
        for model in ("villagepersonalcommission", "villagepersonalcommissionescrow", "villagepersonalcommissionreceipt"):
            response = self.client.get(f"/admin/village/{model}/")
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, f'/admin/village/{model}/add/')
            self.assertEqual(self.client.get(f"/admin/village/{model}/add/").status_code, 403)
