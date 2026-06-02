from django.urls import path
from . import views

urlpatterns = [
    path("api/recipes", views.recipe_list, name="recipe-list"),
]
