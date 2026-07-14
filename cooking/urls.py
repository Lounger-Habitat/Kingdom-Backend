from django.urls import path
from . import views

urlpatterns = [
    path("api/cooking/pity", views.pity_check, name="cooking-pity-check"),
    path("api/cooking/cook", views.cook, name="cooking-cook"),
]
