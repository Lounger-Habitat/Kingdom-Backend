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
    path("api/inventory/<str:character_id>", views.inventory_detail, name="inventory-detail"),
    path("api/inventory/<str:character_id>/add", views.inventory_add, name="inventory-add"),
    path("api/inventory/<str:character_id>/rate", views.inventory_rate, name="inventory-rate"),
    path("api/inventory/<str:character_id>/remove", views.inventory_remove, name="inventory-remove"),
    path("api/inventory/<str:character_id>/swap", views.inventory_swap, name="inventory-swap"),
    path("api/inventory/<str:character_id>/trade", views.inventory_trade, name="inventory-trade"),
    path("api/inventory/<str:character_id>/transfer", views.inventory_transfer, name="inventory-transfer"),
    path("api/inventory/<str:character_id>/trade-with", views.inventory_trade_with, name="inventory-trade-with"),
    path("api/inventory/<str:character_id>/money", views.inventory_money, name="inventory-money"),
    path("api/inventory/<str:character_id>/compact", views.inventory_compact, name="inventory-compact"),
]
