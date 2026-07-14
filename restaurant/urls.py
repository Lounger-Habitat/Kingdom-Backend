from django.urls import path
from . import views

app_name = "restaurant"

urlpatterns = [
    path("api/restaurant/state", views.restaurant_state, name="restaurant-state"),
    path("api/restaurant/menu/exhibit", views.menu_exhibit, name="menu-exhibit"),
    path("api/restaurant/menu/withdraw", views.menu_withdraw, name="menu-withdraw"),
    path("api/restaurant/daily-business", views.daily_business, name="daily-business"),
    path("api/restaurant/reviews", views.review_history, name="review-history"),
    path("api/restaurant/business-history", views.business_history, name="business-history"),
    path("api/restaurant/upgrade", views.upgrade_restaurant, name="upgrade-restaurant"),
    path("api/restaurant/upgrade-info", views.upgrade_info, name="upgrade-info"),
]
