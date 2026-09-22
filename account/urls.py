from django.urls import path
from . import views
from .appearance import player_appearance

urlpatterns = [
    path("api/player/me/appearance", player_appearance, name="player-appearance"),
    path("api/auth/register", views.auth_register, name="auth-register"),
    path("api/auth/login", views.auth_login, name="auth-login"),
    path("api/auth/guest", views.auth_guest, name="auth-guest"),
    path("api/auth/convert", views.auth_convert, name="auth-convert"),
    path("api/auth/refresh", views.auth_refresh, name="auth-refresh"),
    path("api/env/auth/verify-player", views.env_verify_player, name="env-verify-player"),
]
