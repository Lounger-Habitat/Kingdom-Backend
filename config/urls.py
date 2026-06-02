from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from player.views_admin import player_reset_list, player_reset_detail

urlpatterns = [
    # 自定义 Admin 页面（必须在 admin.site.urls 之前，否则被 catch_all_view 拦截）
    path("", include("inventory.urls")),
    path("admin/player/reset/", player_reset_list, name="player-admin-reset-list"),
    path("admin/player/reset/<int:player_id>/", player_reset_detail, name="player-admin-reset-detail"),
    path("admin/", admin.site.urls),
    # API Docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # API
    path("", include("account.urls")),
    path("", include("items.urls")),
    path("", include("recipes.urls")),
    path("", include("player.urls")),
    path("", include("leaderboard.urls")),
    path("", include("game_time.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
