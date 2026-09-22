from copy import deepcopy
from unittest import TestCase

from village.affair_agent_projection import project_npc_affairs
from village.story_state_projection import project_story_state


class AffairAgentProjectionTests(TestCase):
    def test_two_projects_offer_actions_for_their_own_stage(self):
        affairs = [
            {"affair_id": "BRIDGE_REPAIR", "kind": "RESOURCE_PROJECT", "status": "PREPARING",
             "materials": [{"remaining": 10}], "gold": {"remaining": 0}, "meals": {"remaining": 2}, "labor": {"remaining": 20}},
            {"affair_id": "WELL_REPAIR", "kind": "RESOURCE_PROJECT", "status": "READY",
             "materials": [], "gold": {"remaining": 0}, "meals": {"remaining": 0}, "labor": {"remaining": 3}},
            {"affair_id": "MEETING_1", "kind": "MEETING", "status": "OPEN"},
        ]
        original = deepcopy(affairs)
        projected = project_npc_affairs(affairs, ["STORY_ACT"])
        self.assertEqual(projected[0]["available_actions"], ["DONATE_REPAIR_RESOURCES", "PROVIDE_REPAIR_MEAL"])
        self.assertEqual(projected[1]["available_actions"], ["REPAIR_LABOR"])
        self.assertEqual(projected[2]["available_actions"], [])
        self.assertEqual(affairs, original)

    def test_completed_or_unavailable_actor_cannot_start_work(self):
        affairs = [{"affair_id": "BRIDGE_REPAIR", "kind": "RESOURCE_PROJECT", "status": "READY", "labor": {"remaining": 1}}]
        self.assertEqual(project_npc_affairs(affairs, ["STORY_ACT"], unavailable=True)[0]["available_actions"], [])
        self.assertEqual(project_npc_affairs(affairs, ["EAT"])[0]["available_actions"], [])
        affairs[0]["status"] = "COMPLETED"
        self.assertEqual(project_npc_affairs(affairs, ["STORY_ACT"])[0]["available_actions"], [])

    def test_affairs_survive_legacy_completed_story_projection(self):
        affair = {"affair_id": "BRIDGE_REPAIR", "kind": "RESOURCE_PROJECT", "status": "READY", "labor": {"remaining": 20}}
        source = {"phase": "AFTERMATH", "status": "ACTIVE", "affairs": [affair],
                  "available_actions_explanation": "事务独立推进，不等待日结评估满足任一条件。"}
        result = project_story_state(source, "new_npc", ["STORY_ACT"], 100, "LONG_SHORTAGE")
        self.assertEqual(result["affairs"], [affair])
        self.assertEqual(result["available_actions_explanation"], source["available_actions_explanation"])
        result["affairs"][0]["status"] = "COMPLETED"
        self.assertEqual(source["affairs"][0]["status"], "READY")
