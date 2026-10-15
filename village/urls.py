from django.urls import path

from . import views
from . import phase3_views
from . import phase4_views
from . import phase5_views
from . import phase7_views
from . import player_views
from . import wildlife_views
from . import forestry_views
from . import daily_record_views
from . import action_lifecycle


urlpatterns = [
    path("api/village/npcs/<str:npc_id>/operation", action_lifecycle.runtime_operation),
    path("api/village/npcs/<str:npc_id>/resources", action_lifecycle.runtime_resources),
    path("api/village/runtime/daily-record", daily_record_views.write_record, name="village-daily-record-write"),
    path("api/village/public/npcs/<str:npc_id>/daily-records/<str:kind>", daily_record_views.public_records, name="village-daily-records-public"),
    path("api/village/forestry/action/", forestry_views.forestry_action, name="village-forestry-action"),
    path("api/village/forestry/bootstrap", forestry_views.forestry_bootstrap, name="village-forestry-bootstrap"),
    path("api/village/wildlife/action/", wildlife_views.wildlife_action, name="village-wildlife-action"),
    path("api/village/wildlife/bootstrap", wildlife_views.wildlife_bootstrap, name="village-wildlife-bootstrap"),
    path("api/village/player/labor/tick/", player_views.player_labor_tick, name="village-player-labor-tick"),
    path("api/village/player/action/", player_views.player_action, name="village-player-action"),
    path("api/village/player/action", player_views.player_action),
    path("api/village/public/journal", phase7_views.journal, name="village-public-journal"),
    path("api/village/public/overview", phase7_views.overview, name="village-public-overview"),
    path("api/village/public/stores/<str:store_code>", phase7_views.store, name="village-public-store"),
    path("api/village/public/caravans/<str:route>", phase7_views.caravan, name="village-public-caravan"),
    path("api/village/public/caravan-demands", phase7_views.caravan_demands, name="village-public-caravan-demands"),
    path("api/village/public/market", phase7_views.market, name="village-public-market"),
    path("api/village/public/npcs/<str:npc_id>/observation", phase7_views.npc_observation, name="village-public-npc-observation"),
    path("api/village/operations/snapshot", phase7_views.operations, name="village-operations-snapshot"),
    # 阶段零 API
    path("api/village/config", views.village_config, name="village-config"),
    path("api/village/npcs", views.village_npcs, name="village-npcs"),
    path("api/village/npcs/<str:npc_id>/runtime-summary", views.npc_runtime_summary, name="village-npc-runtime-summary"),
    path("api/village/npcs/<str:npc_id>/available-actions", views.npc_available_actions, name="village-npc-available-actions"),
    path("api/village/npcs/<str:npc_id>/discover", views.npc_discover, name="village-npc-discover"),
    path("api/village/npcs/<str:npc_id>/query/<str:query_name>", views.npc_agent_query, name="village-npc-agent-query"),
    path("api/village/locations", views.village_locations, name="village-locations"),
    path(
        "api/village/bootstrap-validation",
        views.bootstrap_validation,
        name="village-bootstrap-validation",
    ),
    # 阶段一 Runtime API
    path("api/village/runtime/bootstrap", views.runtime_bootstrap, name="village-runtime-bootstrap"),
    path("api/village/runtime/checkpoint", views.runtime_checkpoint, name="village-runtime-checkpoint"),
    path("api/village/runtime/subjective-goal", views.subjective_goal_update, name="village-subjective-goal-update"),
    path("api/village/runtime/instruction-outcome", views.instruction_outcome_record, name="village-instruction-outcome"),
    path("api/village/runtime/summary", views.runtime_summary_view, name="village-runtime-summary"),
    path("api/village/runtime/time", views.runtime_time, name="village-runtime-time"),
    path("api/village/runtime/tick", views.runtime_tick, name="village-runtime-tick"),
    path("api/village/runtime/advance-needs", views.runtime_advance_needs, name="village-runtime-advance-needs"),
    path("api/village/life-actions/execute", views.life_actions_execute, name="village-life-actions-execute"),
    path("api/village/dialogues/start", views.dialogue_start, name="village-dialogue-start"),
    path("api/village/dialogues/<str:conversation_id>/speak", views.dialogue_speak, name="village-dialogue-speak"),
    path("api/village/dialogues/<str:conversation_id>/complete", views.dialogue_complete, name="village-dialogue-complete"),
    path("api/village/actions/complete", views.actions_complete, name="village-actions-complete"),
    path("api/village/events", views.events_list, name="village-events"),
    # 阶段一 Command API
    # 阶段一 Outbox API
    path("api/village/deliveries/pending", views.deliveries_pending, name="village-deliveries-pending"),
    path("api/village/deliveries/<str:delivery_id>/ack", views.deliveries_ack, name="village-deliveries-ack"),
    path("api/village/deliveries/cursor", views.deliveries_cursor, name="village-deliveries-cursor"),
    path("api/village/commands/accept", views.commands_accept, name="village-commands-accept"),
    path("api/village/action-tasks/<str:npc_id>/<path:action_id>", views.star_action_status, name="village-star-action-status"),
    path("api/village/commands/<str:command_id>/start", views.commands_start, name="village-commands-start"),
    path("api/village/commands/<str:command_id>/status", views.commands_status, name="village-commands-status"),
    path("api/village/commands/<str:command_id>/cancel", views.commands_cancel, name="village-commands-cancel"),
    path("api/village/commands/<str:command_id>/wake", views.interrupt_sleep, name="village-sleep-interrupt"),
    # Phase 4 social read APIs
    path("api/village/social/npcs/<str:npc_id>", phase4_views.social_npc, name="village-social-npc"),
    path("api/village/social/player/facts", phase4_views.player_facts, name="village-social-player-facts"),
    path("api/village/social/actions/complete", phase4_views.complete_social_action, name="village-social-action-complete"),
    # Phase 3 economy API
    path("api/village/economy/actions/complete", phase3_views.economy_actions_complete, name="village-economy-actions-complete"),
    path("api/village/economy/snapshot", phase3_views.economy_snapshot, name="village-economy-snapshot"),
    path("api/village/economy/day-close", phase3_views.economy_day_close, name="village-economy-day-close"),
    path(
        "api/village/economy/stores/<str:store_code>/inventory",
        phase3_views.economy_store_inventory,
        name="village-economy-store-inventory",
    ),
    path("api/village/story/snapshot", phase5_views.story_snapshot, name="village-story-snapshot"),
    path("api/village/story/vote", phase5_views.story_vote, name="village-story-vote"),
    path("api/village/story/actions/complete", phase5_views.story_action_complete, name="village-story-action-complete"),
    path("api/village/story/command", phase5_views.story_command, name="village-story-command"),
    path("api/village/story/meeting/<uuid:meeting_id>/settle", phase5_views.story_meeting_settle, name="village-story-meeting-settle"),
]
