from django.contrib import admin
from .models import GameAccount


@admin.register(GameAccount)
class GameAccountAdmin(admin.ModelAdmin):
    list_display = ["id", "username", "is_guest", "is_active", "created_at"]
    list_filter = ["is_guest", "is_active"]
    search_fields = ["username"]
    readonly_fields = ["created_at"]
