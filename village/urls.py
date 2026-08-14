from django.urls import path

from . import views
from . import phase3_views


urlpatterns = [
    # 阶段零 API
    path("api/village/config", views.village_config, name="village-config"),
    path("api/village/npcs", views.village_npcs, name="village-npcs"),
    path("api/village/npcs/<str:npc_id>/runtime-summary", views.npc_runtime_summary, name="village-npc-runtime-summary"),
    path("api/village/npcs/<str:npc_id>/available-actions", views.npc_available_actions, name="village-npc-available-actions"),
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
    path("api/village/actions/complete", views.actions_complete, name="village-actions-complete"),
    path("api/village/events", views.events_list, name="village-events"),
    # 阶段一 Command API
    # 阶段一 Outbox API
    # Phase 3 economy API
    path("api/village/economy/actions/complete", phase3_views.economy_actions_complete, name="village-economy-actions-complete"),
    path("api/village/economy/snapshot", phase3_views.economy_snapshot, name="village-economy-snapshot"),
    path("api/village/economy/day-close", phase3_views.economy_day_close, name="village-economy-day-close"),
    path(
        "api/village/economy/stores/<str:store_code>/inventory",
        phase3_views.economy_store_inventory,
        name="village-economy-store-inventory",
    ),
]
