from django.urls import path
from . import views

urlpatterns = [
    path("game/time/day-advanced", views.day_advanced, name="day-advanced"),
    path("game/time/check-day-advance", views.check_day_advance, name="check-day-advance"),
]
