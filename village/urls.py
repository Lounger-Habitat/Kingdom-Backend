from django.urls import path

from . import views
from . import phase3_views
from . import phase4_views
from . import phase5_views


urlpatterns = [
    # 阶段零 API
    path("api/village/config", views.village_config, name="village-config"),
    path("api/village/npcs", views.village_npcs, name="village-npcs"),
    path("api/village/npcs/<str:npc_id>/runtime-summary", views.npc_runtime_summary, name="village-npc-runtime-summary"),
    path("api/village/npcs/<str:npc_id>/available-actions", views.npc_available_actions, name="village-npc-available-actions"),
    path("api/village/npcs/<str:npc_id>/discover", views.npc_discover, name="village-npc-discover"),
    path("api/village/locations", views.village_locations, name="village-locations"),
    path(
        "api/village/bootstrap-validation",
        views.bootstrap_validation,
        name="village-bootstrap-validation",
    ),
    # 阶段一 Runtime API
    path("api/village/runtime/bootstrap", views.runtime_bootstrap, name="village-runtime-bootstrap"),
    path("api/village/runtime/checkpoint", views.runtime_checkpoint, name="village-runtime-checkpoint"),
    path("api/village/runtime/summary", views.runtime_summary_view, name="village-runtime-summary"),
    path("api/village/runtime/time", views.runtime_time, name="village-runtime-time"),
    path("api/village/runtime/advance-needs", views.runtime_advance_needs, name="village-runtime-advance-needs"),
    path("api/village/life-actions/execute", views.life_actions_execute, name="village-life-actions-execute"),
    path("api/village/actions/complete", views.actions_complete, name="village-actions-complete"),
    path("api/village/events", views.events_list, name="village-events"),
    # 阶段一 Command API
    # 阶段一 Outbox API
    path("api/village/deliveries/pending", views.deliveries_pending, name="village-deliveries-pending"),
    path("api/village/deliveries/<str:delivery_id>/ack", views.deliveries_ack, name="village-deliveries-ack"),
    path("api/village/deliveries/cursor", views.deliveries_cursor, name="village-deliveries-cursor"),
    path("api/village/commands/accept", views.commands_accept, name="village-commands-accept"),
    path("api/village/commands/<str:command_id>/start", views.commands_start, name="village-commands-start"),
    path("api/village/commands/<str:command_id>/status", views.commands_status, name="village-commands-status"),
    path("api/village/commands/<str:command_id>/cancel", views.commands_cancel, name="village-commands-cancel"),
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
