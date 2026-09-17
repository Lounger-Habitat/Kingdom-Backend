"""Admin compatibility fields persisted through the same batch save boundary."""
from django import forms

from .components import COMPONENT_FIELDS


class BatchComponentForm(forms.ModelForm):
    remaining_uses = forms.IntegerField(required=False, label="剩余使用次数")
    ingredient = forms.CharField(required=False, max_length=200, label="菜品原料")
    cook_time = forms.IntegerField(required=False, initial=0, label="烹饪时长")
    dish_metadata = forms.JSONField(required=False, label="菜品元数据")
    rated = forms.BooleanField(required=False, label="已点评")
    rating_price = forms.IntegerField(required=False, initial=-1, label="点评定价")
    overall_score = forms.FloatField(required=False, initial=0, label="点评分数")
    review_comment = forms.CharField(required=False, widget=forms.Textarea, label="点评评语")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for name in COMPONENT_FIELDS:
                if name in self.fields:
                    self.initial[name] = getattr(self.instance, name)

    def save(self, commit=True):
        instance = super().save(commit=False)
        for name, (_, _, default) in COMPONENT_FIELDS.items():
            if name in self.cleaned_data:
                value = self.cleaned_data[name]
                setattr(instance, name, default if value is None else value)
        if commit:
            instance.save()
            self.save_m2m()
        return instance
