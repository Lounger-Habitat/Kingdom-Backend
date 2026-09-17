from copy import deepcopy
from unittest import TestCase

from village.story_state_projection import project_story_state


class StoryStateProjectionTests(TestCase):
    def detail(self):
        return {
            "story_id": "story", "arc_code": "story", "title": "断掉的商路",
            "status": "ACTIVE", "phase": "AFTERMATH", "plan": None,
            "bridge_condition": "BROKEN", "story_version": 0,
            "progress": {"repair_wood": 0, "ration_daily_limits": {}, "ration_limits_until_world_minute": None},
            "known_facts": ["BRIDGE_FOUNDATION_WARNING"], "available_actions": [],
            "available_actions_explanation": "日结评估满足任一条件后推进；种植/狩猎/备货/就餐。",
            "emergency_route_rule": "Nora个人支付开通费。", "retired_plan": False,
            "mark_required_for_new_decision": True, "approved_work_may_continue_while_mark_away": True,
            "meetings": [], "public_decisions": [], "has_more_public_decisions": False,
            "public_decisions_scope": "LATEST_10",
        }

    def test_five_roles_receive_only_own_rules_and_bill_keeps_public_facts(self):
        source = self.detail()
        original = deepcopy(source)
        for npc_id in ("10011", "10012", "10013", "10014", "10015"):
            with self.subTest(npc_id=npc_id):
                data = project_story_state(source, npc_id, ["PLANT", "EAT"], 100)
                self.assertEqual("emergency_route_rule" in data, npc_id == "10015")
                self.assertEqual(data["progress"], {"repair_wood": 0})
                self.assertEqual(data["known_facts"], original["known_facts"])
                self.assertIn("种植、就餐", data["available_actions_explanation"])
                self.assertNotIn("狩猎", data["available_actions_explanation"])
                for key in ("arc_code", "retired_plan", "mark_required_for_new_decision", "meetings", "public_decisions_scope"):
                    self.assertNotIn(key, data)
        self.assertEqual(source, original)

    def test_voter_keeps_proposals_and_own_vote_without_chair_controls(self):
        source = self.detail()
        source["meetings"] = [{"meeting_id": "meeting", "proposals": {"BRIDGE_REPAIR": {"budget_gold": 500}},
            "own_vote": {"plan_code": "BRIDGE_REPAIR"}, "voting_open": False,
            "voting_unavailable_reason": "STORY_MEETING_NOT_STARTED", "can_settle": False,
            "settlement_result_code": "MARK_NOT_PRESENT"}]
        bill = project_story_state(source, "10011", ["STORY_VOTE"], 100)
        self.assertIn("proposals", bill["meetings"][0])
        self.assertIn("own_vote", bill["meetings"][0])
        self.assertFalse(bill["meetings"][0]["voting_open"])
        self.assertNotIn("can_settle", bill["meetings"][0])
        mark = project_story_state(source, "10013", ["STORY_VOTE"], 100)
        self.assertIn("can_settle", mark["meetings"][0])

    def test_decisions_preserve_personal_limits_and_executor_details(self):
        source = self.detail()
        decision = {"decision_id": "decision", "plan_code": "RATION_AND_WAIT",
            "published_world_minute": 50, "revoked_world_minute": None, "remaining_budget": 200,
            "terms": {"executor_npc_id": "10015", "budget_gold": 500, "expires_world_minute": 300,
                "ration_daily_limits": {"1005": 3}, "meal_service": {"executor_npc_id": "10014",
                    "store_code": "RESTAURANT_STORE", "per_npc_daily_limit": 1,
                    "beneficiaries": ["10012"], "subsidies": {"meal": 5}}}}
        unpublished = deepcopy(decision)
        unpublished["published_world_minute"] = None
        source["public_decisions"] = [decision, unpublished]
        bill = project_story_state(source, "10011", [], 100)["public_decisions"]
        self.assertEqual(len(bill), 1)
        self.assertEqual(bill[0]["terms"]["ration_daily_limits"], {"1005": 3})
        self.assertNotIn("executor_npc_id", bill[0]["terms"])
        self.assertNotIn("remaining_budget", bill[0])
        self.assertNotIn("subsidies", bill[0]["terms"]["meal_service"])
        self.assertFalse(bill[0]["terms"]["meal_service"]["eligible"])
        self.assertNotIn("beneficiaries", bill[0]["terms"]["meal_service"])
        lucy = project_story_state(source, "10014", [], 100)["public_decisions"][0]
        self.assertIn("subsidies", lucy["terms"]["meal_service"])
        self.assertEqual(lucy["remaining_budget"], 200)
        nora = project_story_state(source, "10015", [], 100)["public_decisions"][0]
        self.assertEqual(nora["terms"]["executor_npc_id"], "10015")
        self.assertEqual(len(project_story_state(source, "10013", [], 100)["public_decisions"]), 2)
        self.assertNotIn("public_decisions", project_story_state(source, "10011", [], 400))
