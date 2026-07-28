from django.urls import path
from . import views

urlpatterns = [
    path("game/init", views.game_init, name="game-init"),
    path("game/time/day-advanced", views.day_advanced, name="day-advanced"),
    path("game/time/check-day-advance", views.check_day_advance, name="check-day-advance"),
    path("game/time/season-refresh", views.season_refresh, name="season-refresh"),
    path("game/time/sync", views.time_sync, name="time-sync"),
    # 全局游戏时间 API（Env Server专用）
    path("game/time/global", views.global_time_get, name="global-time-get"),
    path("game/time/global/update", views.global_time_update, name="global-time-update"),
    path("game/time/global/day-advanced", views.global_time_day_advanced, name="global-time-day-advanced"),
]
