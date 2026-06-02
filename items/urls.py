from django.urls import path
from . import views

urlpatterns = [
    path("api/items", views.item_list, name="item-list"),
    path("api/items/upload", views.item_upload, name="item-upload"),
    path("api/items/<int:item_id>/image", views.item_upload_image, name="item-upload-image"),
    path("api/items/since", views.item_list_since, name="item-list-since"),
]
