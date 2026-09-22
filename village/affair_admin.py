"""Staff configuration for resource affairs, using the same world creation service."""
import uuid

from django import forms
from django.contrib import admin, messages
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html_join

from game_time.models import GlobalGameTimeState
from items.models import Item
from .affairs import AffairError, create_resource_affair, project_affair
from .models import VillageAffair, VillageAffairContribution, VillageLocationDefinition


class LocationChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.display_name}（{obj.location_code}）"


class SingleLocationChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.display_name}（{obj.location_code}）"


CREATION_TOKEN_SALT = "village.affair.admin.creation"


def new_creation_token():
    return signing.dumps(uuid.uuid4().hex, salt=CREATION_TOKEN_SALT)


class VillageAffairCreateForm(forms.Form):
    # A server-issued identity stays stable through validation errors and retries.
    creation_token = forms.CharField(widget=forms.HiddenInput, initial=new_creation_token,
        error_messages={"required": "创建凭据已失效，请刷新页面后重试。"})
    title = forms.CharField(max_length=100, label="事务名称")
    default_location_code = SingleLocationChoiceField(label="事务默认地点", required=False, empty_label="不限地点",
        queryset=VillageLocationDefinition.objects.filter(is_active=True).order_by("location_code"), to_field_name="location_code",
        help_text="阶段未单独配置地点时使用此地点；不选表示默认不限地点。")

    def clean_creation_token(self):
        try:
            value = signing.loads(self.cleaned_data["creation_token"], salt=CREATION_TOKEN_SALT)
            return "AFFAIR_" + uuid.UUID(value).hex.upper()
        except (signing.BadSignature, ValueError, TypeError, AttributeError):
            raise forms.ValidationError("创建凭据已失效，请刷新页面后重试。")


class AffairStageForm(forms.Form):
    key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    order = forms.IntegerField(min_value=1, max_value=20, widget=forms.HiddenInput, initial=1)
    title = forms.CharField(max_length=100, label="阶段名称", initial="完成条件")
    location_code = SingleLocationChoiceField(label="阶段地点", required=False, empty_label="沿用事务默认地点",
        queryset=VillageLocationDefinition.objects.filter(is_active=True).order_by("location_code"), to_field_name="location_code",
        help_text="本阶段的所有物品、金币交付与劳动使用此地点；不选则沿用事务默认地点。")


class AffairConditionForm(forms.Form):
    stage_key = forms.UUIDField(widget=forms.HiddenInput)
    type = forms.ChoiceField(label="条件类型", choices=(("item", "交付物品"), ("gold", "交付金币"), ("labor", "完成劳动")))
    label = forms.CharField(label="条件名称", max_length=100, required=False,
                            help_text="选填，例如“任意合格餐食”；不填时按条件自动显示。")
    target = forms.IntegerField(label="目标数量", min_value=1, max_value=2147483647)
    item_ids = forms.ModelMultipleChoiceField(label="接受的物品", required=False,
        queryset=Item.objects.filter(is_deleted=False).order_by("item_id"), widget=forms.SelectMultiple(attrs={"size": 6}),
        help_text="多选表示其中任意一种均可计入同一目标；若每种物品都需要，请分别添加条件。")
    min_quality = forms.IntegerField(label="最低品质", min_value=0, max_value=6, required=False, initial=0)
    requires_freshness = forms.BooleanField(label="要求有保鲜记录且未过期", required=False)
    min_remaining_minutes = forms.IntegerField(label="最低剩余保鲜分钟", min_value=0, max_value=2147483647, required=False, initial=0)
    unit_minutes = forms.IntegerField(label="每份劳动的游戏分钟", min_value=1, max_value=1440, required=False, initial=60)

    def clean(self):
        data = super().clean()
        if data.get("type") == "item" and not data.get("item_ids"):
            self.add_error("item_ids", "至少选择一种接受的物品。")
        if data.get("type") == "labor" and not data.get("unit_minutes"):
            self.add_error("unit_minutes", "请填写每份劳动的有效时长。")
        return data


class AffairStageFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        rows = [form.cleaned_data for form in self.forms if not form.errors and not form.cleaned_data.get("DELETE")]
        if not rows:
            raise forms.ValidationError("至少配置一个阶段。")
        if len({row.get("key") for row in rows}) != len(rows) or len({row.get("order") for row in rows}) != len(rows):
            raise forms.ValidationError("阶段标识或顺序不能重复。")


AffairStages = forms.formset_factory(AffairStageForm, formset=AffairStageFormSet,
    extra=0, can_delete=True, max_num=20, validate_max=True, absolute_max=20)
AffairConditions = forms.formset_factory(AffairConditionForm,
    extra=0, can_delete=True, max_num=100, validate_max=True, absolute_max=100)


def configured_stages(stage_formset, condition_formset):
    """Build only after both formsets validate; identities freeze at publication."""
    rows = sorted((form.cleaned_data for form in stage_formset if not form.cleaned_data.get("DELETE")), key=lambda row: row["order"])
    stages = {row["key"]: {"id": "stage_" + row["key"].hex, "title": row["title"],
                          "location_code": row["location_code"].location_code if row.get("location_code") else "", "conditions": []} for row in rows}
    for form in condition_formset:
        row = form.cleaned_data
        if row.get("DELETE"):
            continue
        if row["stage_key"] not in stages:
            raise forms.ValidationError("条件所对应的阶段不存在或已移除，请检查阶段配置。")
        condition = {"id": "condition_" + uuid.uuid4().hex, "type": row["type"], "target": row["target"]}
        if row.get("label"):
            condition["label"] = row["label"]
        if row["type"] == "item":
            condition.update(item_ids=[item.pk for item in row["item_ids"]], min_quality=row.get("min_quality") or 0,
                requires_freshness=bool(row["requires_freshness"] or row.get("min_remaining_minutes")),
                min_remaining_minutes=row.get("min_remaining_minutes") or 0)
        elif row["type"] == "labor":
            condition["unit_minutes"] = row["unit_minutes"]
        stages[row["stage_key"]]["conditions"].append(condition)
    if any(not stage["conditions"] for stage in stages.values()):
        raise forms.ValidationError("每个阶段至少需要一个完成条件。")
    return {"stages": list(stages.values())}


class ReadOnlyAffairAdmin(admin.ModelAdmin):
    actions = None

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VillageAffair)
class VillageAffairAdmin(ReadOnlyAffairAdmin):
    list_display = ("identifier", "affair_title", "stage", "progress_summary", "work_locations")
    search_fields = ("code", "title")
    list_filter = ("status",)
    list_per_page = 25
    ordering = ("-opened_world_minute", "code")
    readonly_fields = ("identifier", "affair_title", "stage", "requirements_summary", "progress_summary",
                       "work_locations", "opened_at", "completed_at", "contribution_records")
    fields = readonly_fields

    def add_view(self, request, form_url="", extra_context=None):
        if not self.has_add_permission(request):
            raise PermissionDenied
        data = request.POST if request.method == "POST" else None
        form = VillageAffairCreateForm(data)
        initial_key = uuid.uuid4()
        stage_formset = AffairStages(data, prefix="stages", initial=[{"key": initial_key, "order": 1, "title": "完成条件"}] if data is None else None)
        condition_formset = AffairConditions(data, prefix="conditions", initial=[{"stage_key": initial_key, "type": "item"}] if data is None else None)
        if request.method == "POST":
            valid = [form.is_valid(), stage_formset.is_valid(), condition_formset.is_valid()]
            if all(valid):
                try:
                    requirements = configured_stages(stage_formset, condition_formset)
                    with transaction.atomic():
                        clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
                        if clock is None:
                            raise AffairError("WORLD_NOT_INITIALIZED", "世界时钟尚未初始化，请先初始化村庄后再创建事务。")
                        code = form.cleaned_data["creation_token"]
                        if VillageAffair.objects.filter(pk=code).exists():
                            form.add_error(None, "该表单已发布事务，请返回事务列表查看；重复提交不会创建新事务。")
                        else:
                            location = form.cleaned_data.get("default_location_code")
                            affair = create_resource_affair(code, form.cleaned_data["title"], requirements,
                                [location.location_code] if location else [], world_minute=clock.world_minute)
                            self.log_addition(request, affair, "在后台创建并发布分阶段事务。")
                    if not form.errors:
                        self.message_user(request, "事务已创建并发布，游戏刷新事务列表后即可参与。", messages.SUCCESS)
                        destination = "admin:village_villageaffair_changelist" if self.has_view_permission(request) else "admin:index"
                        return HttpResponseRedirect(reverse(destination))
                except AffairError as error:
                    form.add_error(None, error.message)
                except forms.ValidationError as error:
                    form.add_error(None, error)
        sections, assigned = [], set()
        for stage_form in stage_formset:
            key = str(stage_form["key"].value())
            conditions = [child for child in condition_formset if str(child["stage_key"].value()) == key]
            assigned.update(child.prefix for child in conditions)
            sections.append({"form": stage_form, "conditions": conditions})
        sections.sort(key=lambda section: int(section["form"]["order"].value())
                      if str(section["form"]["order"].value()).isdigit() else 21)
        context = {
            **self.admin_site.each_context(request), "title": "新增村庄事务", "opts": self.model._meta,
            "app_label": self.model._meta.app_label, "form": form, "stage_formset": stage_formset,
            "condition_formset": condition_formset, "stage_sections": sections,
            "orphan_conditions": [child for child in condition_formset if child.prefix not in assigned],
            "media": self.media + form.media + stage_formset.media + condition_formset.media, "add": True,
            "has_view_permission": self.has_view_permission(request), "has_add_permission": True,
            "has_change_permission": False, "has_delete_permission": False,
            **(extra_context or {}),
        }
        return TemplateResponse(request, "admin/village/villageaffair/add_form.html", context)

    @admin.display(description="事务标识", ordering="code")
    def identifier(self, obj):
        return obj.code

    @admin.display(description="事务名称", ordering="title")
    def affair_title(self, obj):
        return obj.title

    @admin.display(description="当前阶段", ordering="status")
    def stage(self, obj):
        data = project_affair(obj)
        return "已完成" if obj.status == "COMPLETED" else data.get("current_stage_title", "进行中")

    def resource_lines(self, obj, show_progress):
        entries = []
        for stage in project_affair(obj).get("stages", []):
            state = {"ACTIVE": "当前", "LOCKED": "未开放", "COMPLETED": "已完成"}.get(stage["status"], stage["status"])
            locations = "、".join(stage.get("locations", [])) or "不限地点"
            entries.append((f"{stage['title']}（{state}）", locations))
            for condition in stage["conditions"]:
                label = condition.get("label") or {"item": "交付物品", "gold": "交付金币", "labor": "完成劳动"}[condition["type"]]
                amount = f"{condition['submitted']} / {condition['target']}" if show_progress else str(condition["target"])
                entries.append((label, amount))
        return format_html_join("", "<div>{} {}</div>", entries)

    @admin.display(description="固定需求")
    def requirements_summary(self, obj):
        return self.resource_lines(obj, False)

    @admin.display(description="累计进度")
    def progress_summary(self, obj):
        return self.resource_lines(obj, True)

    @admin.display(description="阶段地点")
    def work_locations(self, obj):
        conditions = [condition for stage in project_affair(obj).get("stages", []) for condition in stage["conditions"]]
        codes = sorted({code for condition in conditions for code in condition.get("locations", [])})
        names = dict(VillageLocationDefinition.objects.filter(location_code__in=codes).values_list("location_code", "display_name"))
        result = [names.get(code, code) for code in codes]
        if any(not condition.get("locations") for condition in conditions):
            result.append("部分条件不限地点" if codes else "不限地点")
        return "、".join(result)

    @admin.display(description="创建时的世界分钟")
    def opened_at(self, obj):
        return obj.opened_world_minute

    @admin.display(description="完成时的世界分钟")
    def completed_at(self, obj):
        return obj.completed_world_minute

    @admin.display(description="贡献明细")
    def contribution_records(self, obj):
        from django.utils.html import format_html
        from urllib.parse import urlencode
        url = reverse("admin:village_villageaffaircontribution_changelist") + "?" + urlencode({"affair__code__exact": obj.code})
        return format_html('<a href="{}">查看这件事务的贡献记录</a>', url)


@admin.register(VillageAffairContribution)
class VillageAffairContributionAdmin(ReadOnlyAffairAdmin):
    list_display = ("affair", "condition_id", "contributor", "contribution_kind", "contribution_item", "contribution_amount", "contributed_at")
    search_fields = ("affair__code", "affair__title", "actor_id")
    list_filter = ("kind", "actor_type", "affair")
    list_select_related = ("affair",)
    ordering = ("-world_minute", "-pk")
    readonly_fields = tuple(field.name for field in VillageAffairContribution._meta.fields)

    def has_add_permission(self, request):
        return False

    @admin.display(description="贡献者")
    def contributor(self, obj):
        return f"{'玩家' if obj.actor_type == 'PLAYER' else 'NPC'} {obj.actor_id}"

    @admin.display(description="贡献类型")
    def contribution_kind(self, obj):
        return {"item": "物品", "material": "材料", "meal": "成品餐", "gold": "工程金币", "labor": "劳动"}.get(obj.kind, obj.kind)

    @admin.display(description="物品编号")
    def contribution_item(self, obj):
        return obj.item_id or "—"

    @admin.display(description="数量")
    def contribution_amount(self, obj):
        return obj.quantity

    @admin.display(description="贡献时的世界分钟")
    def contributed_at(self, obj):
        return obj.world_minute
