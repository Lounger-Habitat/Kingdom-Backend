from django.urls import path
from . import views, views_admin

urlpatterns = [
    # 自定义 Admin 页面
    path("admin/inventory/player-bags/", views_admin.player_list, name="inventory-admin-player-list"),
    path("admin/inventory/player-bags/<int:player_id>/", views_admin.player_bags, name="inventory-admin-player-bags"),
    path("admin/inventory/player-bags/<int:player_id>/bag/<int:bag_id>/", views_admin.bag_detail, name="inventory-admin-bag-detail"),

    # API
    path("api/inventory/bulk-push", views.inventory_bulk_push, name="inventory-bulk-push"),
    path("api/inventory/<str:character_id>/collect", views.inventory_collect, name="inventory-collect"),
    path("api/inventory/<str:character_id>/collect-upgrade", views.inventory_collect_upgrade, name="inventory-collect-upgrade"),
    path("api/inventory/<str:character_id>/collect-upgrade-info", views.inventory_collect_upgrade_info, name="inventory-collect-upgrade-info"),
    path("api/inventory/<str:character_id>", views.inventory_detail, name="inventory-detail"),
    path("api/inventory/<str:character_id>/add", views.inventory_add, name="inventory-add"),
    path("api/inventory/<str:character_id>/use", views.inventory_use, name="inventory-use"),
    path("api/inventory/<str:character_id>/rate", views.inventory_rate, name="inventory-rate"),
    path("api/inventory/<str:character_id>/judge", views.inventory_judge, name="inventory-judge"),
    path("api/inventory/<str:character_id>/judge-by-agent", views.inventory_judge_by_agent, name="inventory-judge-by-agent"),
    path("api/inventory/<str:character_id>/remove", views.inventory_remove, name="inventory-remove"),
    path("api/inventory/<str:character_id>/swap", views.inventory_swap, name="inventory-swap"),
    path("api/inventory/<str:character_id>/transfer", views.inventory_transfer, name="inventory-transfer"),
    path("api/inventory/<str:character_id>/money", views.inventory_money, name="inventory-money"),
    path("api/inventory/<str:character_id>/compact", views.inventory_compact, name="inventory-compact"),

    # Env Server 内部动作 API（service token only）
    path("api/env/actions/character-trade", views.character_trade, name="character-trade"),
    path("api/env/actions/character-trade/receipt", views.character_trade_receipt, name="character-trade-receipt"),

    # NPC 全局库存 API
    path("api/npc-inventory/restock", views.npc_global_inventory_restock, name="npc-global-inventory-restock"),
    path("api/npc-inventory/<str:character_id>", views.npc_global_inventory_detail, name="npc-global-inventory-detail"),
    path("api/npc-inventory/<str:character_id>/update", views.npc_global_inventory_update, name="npc-global-inventory-update"),
]
