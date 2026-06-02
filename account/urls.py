from django.urls import path
from . import views

urlpatterns = [
    path("api/auth/register", views.auth_register, name="auth-register"),
    path("api/auth/login", views.auth_login, name="auth-login"),
    path("api/auth/guest", views.auth_guest, name="auth-guest"),
    path("api/auth/convert", views.auth_convert, name="auth-convert"),
    path("api/auth/refresh", views.auth_refresh, name="auth-refresh"),
]
