from django.urls import path
from . import views

urlpatterns = [
    path("player/state", views.player_state, name="player-state"),
    path("player/cost_action", views.player_cost_action, name="player-cost-action"),
    path("api/player/recipes", views.player_recipes, name="player-recipes"),
    path("api/player/recipes/<str:recipe_name>/setting", views.player_recipe_setting, name="player-recipe-setting"),
    path("api/player/titles", views.player_titles, name="player-titles"),
]
