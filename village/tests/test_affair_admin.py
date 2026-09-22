"""Admin configuration preserves stage ordering and server authority."""
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing
from django.test import TestCase
from django.urls import reverse

from game_time.models import GlobalGameTimeState
from items.models import Item
from village.affairs import create_resource_affair, project_affairs
from village.models import VillageAffair, VillageAffairContribution, VillageLocationDefinition


class VillageAffairAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = get_user_model().objects.create_superuser(username="affair-admin", password="test-only")
        cls.staff_user = get_user_model().objects.create_user(username="affair-staff", password="test-only", is_staff=True)
        cls.clock = GlobalGameTimeState.get_instance()
        cls.clock.apply_world_minute(1500)
        cls.clock.save()
        for code, active in (("ADMIN_WORKSITE", True), ("ADMIN_SECOND_SITE", True), ("ADMIN_DISABLED_SITE", False)):
            VillageLocationDefinition.objects.update_or_create(location_code=code, defaults={
                "display_name": "测试办理点", "location_type": "PUBLIC", "center_anchor_code": code, "is_active": active})
        for item_id, name, deleted in ((91001, "测试木材", False), (91002, "测试石材", False), (91003, "已删除材料", True)):
            Item.objects.update_or_create(pk=item_id, defaults={"item_name": name, "is_deleted": deleted})

    def setUp(self):
        self.client.force_login(self.admin_user)
        self.add_url = reverse("admin:village_villageaffair_add")
        self.list_url = reverse("admin:village_villageaffair_changelist")

    def payload(self, stages=None, **overrides):
        response = self.client.get(self.add_url)
        self.assertEqual(response.status_code, 200)
        if stages is None:
            stages = [{"title": "收集物资", "conditions": [{"type": "item", "target": 20, "item_ids": [91001]}]}]
        values = {"creation_token": response.context["form"]["creation_token"].value(), "title": "修复水井",
                  "default_location_code": "", "_save": "保存"}
        for prefix, count, maximum in (("stages", len(stages), 20),
                ("conditions", sum(len(s.get("conditions", [])) for s in stages), 100)):
            values.update({f"{prefix}-TOTAL_FORMS": str(count), f"{prefix}-INITIAL_FORMS": "0",
                           f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": str(maximum)})
        condition_index = 0
        for index, stage in enumerate(stages):
            key = str(stage.get("key") or (response.context["stage_formset"][0]["key"].value() if index == 0 else uuid.uuid4()))
            row = {"key": key, "order": stage.get("order", index + 1), "title": stage.get("title", "完成条件"),
                   "location_code": stage.get("location_code", "")}
            for field, value in row.items():
                values[f"stages-{index}-{field}"] = str(value)
            if stage.get("DELETE"):
                values[f"stages-{index}-DELETE"] = "on"
            for condition in stage.get("conditions", []):
                row = {"stage_key": key, "type": "item", "target": 1, "label": "", "min_quality": "",
                       "min_remaining_minutes": "", "unit_minutes": "60", **condition}
                for field, value in row.items():
                    if isinstance(value, list):
                        values[f"conditions-{condition_index}-{field}"] = [str(item) for item in value]
                    elif isinstance(value, bool):
                        if value:
                            values[f"conditions-{condition_index}-{field}"] = "on"
                    else:
                        values[f"conditions-{condition_index}-{field}"] = str(value)
                condition_index += 1
        values.update(overrides)
        return values

    def existing_affair(self):
        row = create_resource_affair("EXISTING_WELL", "已有水井事务", {"stages": [{
            "id": "existing", "title": "筹集资金", "location_code": "", "conditions": [
                {"id": "old_gold", "type": "gold", "target": 50}]}]}, ["ADMIN_WORKSITE"], self.clock.world_minute)
        row.progress = {"conditions": {"old_gold": 10}}
        row.version = 3
        row.save()
        return row

    def public_affair(self, row):
        return next(affair for affair in project_affairs(self.clock.world_minute) if affair["affair_id"] == row.code)

    def assert_rejected(self, data):
        response = self.client.post(self.add_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(VillageAffair.objects.exists())
        return response

    def test_page_exposes_stage_editor_and_only_dynamic_conditions(self):
        row = self.existing_affair()
        self.assertContains(self.client.get(self.list_url), row.title)
        response = self.client.get(self.add_url)
        for label in ("事务名称", "事务默认地点", "阶段名称", "阶段地点", "交付物品", "交付金币", "完成劳动"):
            self.assertContains(response, label)
        for name in ("creation_token", "title", "default_location_code", "stages-TOTAL_FORMS", "stages-0-key",
                     "stages-0-order", "stages-0-title", "stages-0-location_code", "conditions-TOTAL_FORMS",
                     "conditions-0-stage_key", "conditions-0-type", "conditions-0-target", "conditions-0-item_ids"):
            self.assertContains(response, f'name="{name}"')
        for name in ("progress", "status", "code", "meals", "gold", "labor", "conditions-0-locations", "conditions-0-location_policy"):
            self.assertNotContains(response, f'name="{name}"')
        self.assertTrue(response.context["form"]["creation_token"].field.widget.is_hidden)
        self.assertNotContains(response, "ADMIN_DISABLED_SITE")
        self.assertNotContains(response, "已删除材料")
        self.assertEqual(str(response.context["stage_formset"][0]["key"].value()),
                         str(response.context["condition_formset"][0]["stage_key"].value()))

    def test_item_only_stage_accepts_alternatives_without_meals_gold_or_labor(self):
        data = self.payload([{"conditions": [{"type": "item", "target": 30, "item_ids": [91001, 91002]}]}])
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        row = VillageAffair.objects.get()
        self.assertRegex(row.code, r"^AFFAIR_[A-F0-9]{32}$")
        self.assertEqual((row.status, row.opened_world_minute, row.progress["conditions"], row.locations),
                         ("ACTIVE", self.clock.world_minute, {}, []))
        public = self.public_affair(row)
        self.assertEqual(len(public["stages"]), 1)
        conditions = public["stages"][0]["conditions"]
        self.assertEqual(len(conditions), 1)
        need = conditions[0]
        self.assertEqual(need["type"], "item")
        self.assertEqual(set(need["item_ids"]), {91001, 91002})
        self.assertEqual((need["target"], need["locations"], need["min_quality"], need["requires_freshness"]), (30, [], 0, False))
        self.assertTrue(need["available"])

    def test_stage_sorting_and_default_location_inheritance_or_override(self):
        stages = [
            {"title": "现场劳动", "order": 3, "location_code": "ADMIN_SECOND_SITE",
             "conditions": [{"type": "labor", "target": 4, "unit_minutes": 15}]},
            {"title": "先收材料", "order": 1, "conditions": [{"type": "item", "target": 20, "item_ids": [91001]}]},
            {"title": "再筹金币", "order": 2, "conditions": [{"type": "gold", "target": 50}]},
        ]
        self.assertEqual(self.client.post(self.add_url, self.payload(stages, default_location_code="ADMIN_WORKSITE")).status_code, 302)
        row = VillageAffair.objects.get()
        self.assertEqual(row.locations, ["ADMIN_WORKSITE"])
        public = self.public_affair(row)
        self.assertEqual([s["title"] for s in public["stages"]], ["先收材料", "再筹金币", "现场劳动"])
        self.assertEqual([s["status"] for s in public["stages"]], ["ACTIVE", "LOCKED", "LOCKED"])
        for stage in public["stages"][:2]:
            self.assertEqual((stage["location_code"], stage["locations"], stage["conditions"][0]["locations"]),
                             ("", ["ADMIN_WORKSITE"], ["ADMIN_WORKSITE"]))
        work = public["stages"][2]
        self.assertEqual((work["location_code"], work["locations"], work["conditions"][0]["locations"]),
                         ("ADMIN_SECOND_SITE", ["ADMIN_SECOND_SITE"], ["ADMIN_SECOND_SITE"]))
        self.assertEqual(work["conditions"][0]["unit_minutes"], 15)
        self.assertFalse(work["conditions"][0]["available"])
        self.assertEqual([c["type"] for c in public["conditions"]], ["item"])

    def test_gold_only_labor_only_or_same_stage_mixed_conditions_open_immediately(self):
        created = []
        for conditions in ([{"type": "gold", "target": 50}], [{"type": "labor", "target": 3, "unit_minutes": 10}],
                           [{"type": "gold", "target": 50}, {"type": "labor", "target": 3, "unit_minutes": 10}]):
            with self.subTest(conditions=conditions):
                self.assertEqual(self.client.post(self.add_url, self.payload([{"conditions": conditions}])).status_code, 302)
                row = VillageAffair.objects.exclude(code__in=created).get()
                created.append(row.code)
                public = self.public_affair(row)
                self.assertEqual([c["type"] for c in public["conditions"]], [c["type"] for c in conditions])
                self.assertTrue(all(c["available"] for c in public["conditions"]))

    def test_optional_item_quality_and_freshness_are_stored(self):
        data = self.payload([{"conditions": [{"type": "item", "target": 5, "item_ids": [91001],
                                             "min_quality": 3, "min_remaining_minutes": 120}]}])
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        need = VillageAffair.objects.get().requirements["stages"][0]["conditions"][0]
        self.assertEqual((need["min_quality"], need["min_remaining_minutes"], need["requires_freshness"]), (3, 120, True))

    def test_repeat_submission_preserves_progress_and_does_not_duplicate(self):
        data = self.payload()
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        row = VillageAffair.objects.get()
        condition_id = row.requirements["stages"][0]["conditions"][0]["id"]
        row.progress = {"conditions": {condition_id: 3}}
        row.save(update_fields=["progress"])
        response = self.client.post(self.add_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        row.refresh_from_db()
        self.assertEqual(row.progress["conditions"][condition_id], 3)
        self.assertEqual(VillageAffair.objects.count(), 1)

    def test_missing_stage_empty_stage_or_orphan_condition_is_rejected(self):
        for stages in ([], [{"conditions": []}], [{"conditions": [{"type": "gold", "target": 2}]}, {"conditions": []}]):
            with self.subTest(stages=stages):
                self.assert_rejected(self.payload(stages))
        self.assert_rejected(self.payload(**{"conditions-0-stage_key": str(uuid.uuid4())}))

    def test_duplicate_stage_key_or_order_and_missing_management_forms_are_rejected(self):
        stages = [{"conditions": [{"type": "gold", "target": 2}]}, {"conditions": [{"type": "labor", "target": 2}]}]
        for field in ("order", "key"):
            data = self.payload(stages)
            data[f"stages-1-{field}"] = data[f"stages-0-{field}"]
            self.assert_rejected(data)
        for prefix in ("stages", "conditions"):
            data = self.payload()
            del data[f"{prefix}-TOTAL_FORMS"]
            response = self.assert_rejected(data)
            name = "stage_formset" if prefix == "stages" else "condition_formset"
            self.assertTrue(response.context[name].non_form_errors())

    def test_invalid_type_target_item_quality_freshness_or_duration_is_rejected(self):
        for overrides in ({"conditions-0-type": "meeting"}, {"conditions-0-target": "0"},
                          {"conditions-0-target": "-1"}, {"conditions-0-target": "1.5"},
                          {"conditions-0-item_ids": []}, {"conditions-0-min_quality": "7"},
                          {"conditions-0-min_remaining_minutes": "-1"},
                          {"conditions-0-type": "labor", "conditions-0-unit_minutes": "0"},
                          {"conditions-0-type": "labor", "conditions-0-unit_minutes": ""}):
            with self.subTest(overrides=overrides):
                self.assert_rejected(self.payload(**overrides))

    def test_unknown_inactive_locations_and_deleted_or_unknown_items_are_rejected(self):
        for overrides in ({"default_location_code": "UNKNOWN_SITE"}, {"default_location_code": "ADMIN_DISABLED_SITE"},
                          {"stages-0-location_code": "UNKNOWN_SITE"}, {"stages-0-location_code": "ADMIN_DISABLED_SITE"},
                          {"conditions-0-item_ids": ["91003"]}, {"conditions-0-item_ids": ["99999999"]}):
            with self.subTest(overrides=overrides):
                self.assert_rejected(self.payload(**overrides))

    def test_deleted_condition_is_omitted(self):
        data = self.payload([{"conditions": [{"type": "item", "target": 3, "item_ids": [91001]},
                                            {"type": "gold", "target": 50, "DELETE": True}]}])
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        self.assertEqual([c["type"] for c in VillageAffair.objects.get().requirements["stages"][0]["conditions"]], ["item"])

    def test_deleted_stage_and_its_conditions_are_omitted(self):
        data = self.payload([{"title": "保留", "conditions": [{"type": "gold", "target": 50}]},
                             {"title": "删除", "DELETE": True, "conditions": [{"type": "gold", "target": 20, "DELETE": True}]}])
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        self.assertEqual([s["title"] for s in VillageAffair.objects.get().requirements["stages"]], ["保留"])

    def test_deletion_cannot_create_empty_affair_or_orphan_conditions(self):
        self.assert_rejected(self.payload(**{"conditions-0-DELETE": "on"}))
        self.assert_rejected(self.payload(**{"stages-0-DELETE": "on", "conditions-0-DELETE": "on"}))
        self.assert_rejected(self.payload([{"conditions": [{"type": "gold", "target": 5}]},
                                           {"DELETE": True, "conditions": [{"type": "gold", "target": 5}]}]))

    def test_missing_or_forged_creation_tokens_are_rejected(self):
        for token in (None, "", str(uuid.uuid4()), signing.dumps("not-a-uuid", salt="village.affair.admin.creation"),
                      signing.dumps(str(uuid.uuid4()), salt="wrong-salt")):
            with self.subTest(token=token):
                data = self.payload(creation_token=token)
                if token is None:
                    del data["creation_token"]
                self.assertTrue(self.assert_rejected(data).context["form"].errors.get("creation_token"))

    def test_independent_forms_generate_distinct_automatic_identifiers(self):
        first, second = self.payload(), self.payload()
        self.assertNotEqual(first["creation_token"], second["creation_token"])
        for data in (first, second):
            self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        codes = list(VillageAffair.objects.values_list("code", flat=True))
        self.assertEqual(len(codes), 2)
        self.assertNotEqual(*codes)
        for code in codes:
            self.assertRegex(code, r"^AFFAIR_[A-F0-9]{32}$")

    def test_invalid_submission_retains_creation_token_and_stage_links(self):
        data = self.payload(**{"conditions-0-target": "-1"})
        response = self.assert_rejected(data)
        self.assertEqual(response.context["form"]["creation_token"].value(), data["creation_token"])
        self.assertEqual(str(response.context["stage_formset"][0]["key"].value()), data["stages-0-key"])
        self.assertEqual(str(response.context["condition_formset"][0]["stage_key"].value()), data["stages-0-key"])
        data["conditions-0-target"] = "3"
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        self.assertEqual(VillageAffair.objects.count(), 1)

    def test_anonymous_or_unprivileged_staff_cannot_create(self):
        data = self.payload()
        self.client.logout()
        response = self.client.post(self.add_url, data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response.url)
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.post(self.add_url, data).status_code, 403)
        self.assertFalse(VillageAffair.objects.exists())

    def test_staff_with_explicit_add_permission_can_create(self):
        self.staff_user.user_permissions.add(Permission.objects.get(content_type__app_label="village", codename="add_villageaffair"))
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.post(self.add_url, self.payload()).status_code, 302)
        self.assertEqual(VillageAffair.objects.count(), 1)

    def test_forged_code_progress_status_kind_and_time_are_ignored(self):
        existing = self.existing_affair()
        data = self.payload(code=existing.code, progress='{"conditions":{"fake":999999}}', status="COMPLETED", kind="MEETING",
                            version="99999", opened_world_minute="0", completed_world_minute="1", requirements='{"stages":[]}')
        self.assertEqual(self.client.post(self.add_url, data).status_code, 302)
        existing.refresh_from_db()
        self.assertEqual((existing.title, existing.progress, existing.status, existing.version),
                         ("已有水井事务", {"conditions": {"old_gold": 10}}, "ACTIVE", 3))
        row = VillageAffair.objects.exclude(pk=existing.pk).get()
        self.assertRegex(row.code, r"^AFFAIR_[A-F0-9]{32}$")
        self.assertEqual((row.kind, row.status, row.version, row.progress["conditions"]), ("RESOURCE_PROJECT", "ACTIVE", 0, {}))
        self.assertEqual((row.progress["gold"], row.progress["labor"]), (0, 0))
        self.assertEqual(row.requirements["stages"][0]["conditions"][0]["target"], 20)
        self.assertEqual(row.opened_world_minute, self.clock.world_minute)
        self.assertIsNone(row.completed_world_minute)

    def test_published_affair_is_read_only_even_for_superuser(self):
        row = self.existing_affair()
        url = reverse("admin:village_villageaffair_change", args=[row.pk])
        response = self.client.get(url)
        self.assertContains(response, row.title)
        self.assertNotContains(response, 'name="_save"')
        self.assertNotContains(response, 'name="progress"')
        self.assertEqual(self.client.post(url, self.payload(title="被篡改", status="COMPLETED")).status_code, 403)
        row.refresh_from_db()
        self.assertEqual((row.title, row.progress, row.status, row.version),
                         ("已有水井事务", {"conditions": {"old_gold": 10}}, "ACTIVE", 3))

    def test_affairs_cannot_be_deleted_individually_or_in_bulk(self):
        row = self.existing_affair()
        self.assertEqual(self.client.post(reverse("admin:village_villageaffair_delete", args=[row.pk]), {"post": "yes"}).status_code, 403)
        self.client.post(self.list_url, {"action": "delete_selected", "_selected_action": [row.pk], "post": "yes", "index": "0"})
        self.assertTrue(VillageAffair.objects.filter(pk=row.pk).exists())

    def test_contribution_audit_is_read_only(self):
        row = self.existing_affair()
        contribution = VillageAffairContribution.objects.create(affair=row, request_id=uuid.uuid4(), actor_type="PLAYER",
            actor_id="42", kind="gold", condition_id="old_gold", quantity=10, world_minute=self.clock.world_minute)
        self.assertContains(self.client.get(reverse("admin:village_villageaffaircontribution_changelist")), row.code)
        change_url = reverse("admin:village_villageaffaircontribution_change", args=[contribution.pk])
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')
        self.assertEqual(self.client.post(change_url, {"quantity": 1000}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:village_villageaffaircontribution_add"), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:village_villageaffaircontribution_delete", args=[contribution.pk]), {"post": "yes"}).status_code, 403)
        contribution.refresh_from_db()
        self.assertEqual(contribution.quantity, 10)

    def test_missing_world_clock_does_not_invent_time(self):
        GlobalGameTimeState.objects.all().delete()
        response = self.assert_rejected(self.payload())
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertFalse(GlobalGameTimeState.objects.exists())

