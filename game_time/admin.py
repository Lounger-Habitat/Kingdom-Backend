from django.contrib import admin
from .models import GameTimeState


@admin.register(GameTimeState)
class GameTimeStateAdmin(admin.ModelAdmin):
    list_display = ["id", "max_game_days"]
    fields = ["max_game_days"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
