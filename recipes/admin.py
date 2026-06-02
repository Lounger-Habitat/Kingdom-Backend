from django import forms
from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from items.models import Item
from .models import Recipe, RecipeIngredient


class RecipeIngredientForm(forms.ModelForm):
    item_selector = forms.ChoiceField(label="选择食材", required=False)

    class Meta:
        model = RecipeIngredient
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        items = Item.objects.all().order_by("item_id")
        choices = [("", "-- 请选择食材 --")]
        choices += [(str(i.item_id), f"{i.item_id} - {i.item_name}") for i in items]
        self.fields["item_selector"].choices = choices
        if self.instance and self.instance.pk and self.instance.item_id:
            self.fields["item_selector"].initial = str(self.instance.item_id)
        self.fields["item_id"].widget = forms.HiddenInput()


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    form = RecipeIngredientForm
    extra = 1

    class Media:
        css = {
            "all": (
                "https://cdnjs.cloudflare.com/ajax/libs/select2/4.0.13/css/select2.min.css",
                "recipes/admin/css/select2-dark.css",
            )
        }
        js = (
            "https://cdnjs.cloudflare.com/ajax/libs/select2/4.0.13/js/select2.min.js",
            "recipes/admin/js/ingredient_selector.js",
        )


class RecipeResource(resources.ModelResource):
    class Meta:
        model = Recipe
        import_id_fields = ["item_id"]


@admin.register(Recipe)
class RecipeAdmin(ImportExportModelAdmin):
    resource_class = RecipeResource
    list_display = ["item_id", "recipe_name", "cuisine", "taste_profile", "difficulty", "default_cook_time", "default_dish_quality", "version", "updated_at"]
    search_fields = ["recipe_name", "item_id"]
    list_filter = ["cuisine", "difficulty"]
    inlines = [RecipeIngredientInline]
