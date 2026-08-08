from django.urls import path

from . import views


urlpatterns = [
    # 阶段零 API
    path("api/village/config", views.village_config, name="village-config"),
    path("api/village/npcs", views.village_npcs, name="village-npcs"),
    path("api/village/locations", views.village_locations, name="village-locations"),
    path(
        "api/village/bootstrap-validation",
        views.bootstrap_validation,
        name="village-bootstrap-validation",
    ),
    # 阶段一 Runtime API
    path("api/village/runtime/bootstrap", views.runtime_bootstrap, name="village-runtime-bootstrap"),
    path("api/village/runtime/checkpoint", views.runtime_checkpoint, name="village-runtime-checkpoint"),
    path("api/village/runtime/time", views.runtime_time, name="village-runtime-time"),
    path("api/village/actions/complete", views.actions_complete, name="village-actions-complete"),
    path("api/village/events", views.events_list, name="village-events"),
    # 阶段一 Command API
    path("api/village/commands/accept", views.commands_accept, name="village-commands-accept"),
    path("api/village/commands/<str:command_id>/start", views.commands_start, name="village-commands-start"),
    # 阶段一 Outbox API
    path("api/village/deliveries/pending", views.deliveries_pending, name="village-deliveries-pending"),
    path("api/village/deliveries/<str:delivery_id>/ack", views.deliveries_ack, name="village-deliveries-ack"),
]
