from django.urls import path
from . import views

urlpatterns = [
    path("api/marketplace/list", views.marketplace_list, name="marketplace-list"),
    path("api/marketplace/delisting", views.marketplace_delist, name="marketplace-delist"),
    path("api/marketplace/browse", views.marketplace_browse, name="marketplace-browse"),
    path("api/marketplace/buy", views.marketplace_buy, name="marketplace-buy"),
    path("api/marketplace/my-listings", views.marketplace_my_listings, name="marketplace-my-listings"),
    path("api/marketplace/history", views.marketplace_history, name="marketplace-history"),
    path("api/marketplace/sale-notifications", views.marketplace_sale_notifications, name="marketplace-sale-notifications"),
]
