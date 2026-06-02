from django.db import models


class Recipe(models.Model):
    item_id = models.IntegerField()
    recipe_name = models.CharField(max_length=100)

    # 菜品属性（固定）
    cuisine = models.CharField(max_length=50, default="", blank=True, help_text="菜系：川菜、粤菜、浙菜、鲁菜")
    dish_category = models.CharField(max_length=50, default="", blank=True, help_text="分类：主菜/汤品/凉菜/点心/主食")
    serving_style = models.CharField(max_length=50, default="", blank=True, help_text="上菜形式：大盘/小碟/拼盘/位上")
    taste_profile = models.CharField(max_length=50, default="", blank=True, help_text="口味标签：甜/咸/辣/酸/鲜")
    difficulty = models.IntegerField(default=2, help_text="难度：1简单 2中等 3困难")

    # 菜品属性（默认值）
    default_cook_time = models.IntegerField(default=0, help_text="默认烹饪时长（秒）")
    default_dish_quality = models.IntegerField(default=3, help_text="默认品质：1略逊 2一般 3正常 4优秀 5极品 6绝品")

    version = models.IntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item_id"]

    def __str__(self):
        return f"{self.item_id} - {self.recipe_name}"


class RecipeIngredient(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="ingredients")
    item_id = models.IntegerField()
    item_name = models.CharField(max_length=100)
    count = models.IntegerField(default=1)

    def __str__(self):
        return f"{self.item_name} x{self.count}"
