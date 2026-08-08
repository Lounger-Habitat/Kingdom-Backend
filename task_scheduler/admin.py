from django.contrib import admin
from django.template.response import TemplateResponse

from config.scheduler_registry import get_registered_jobs
from .models import ScheduledTaskDashboard


@admin.register(ScheduledTaskDashboard)
class ScheduledTaskDashboardAdmin(admin.ModelAdmin):
    change_list_template = "admin/task_scheduler/scheduledtaskdashboard/change_list.html"

    def changelist_view(self, request, extra_context=None):
        context = {
            **self.admin_site.each_context(request),
            "title": "当前定时任务",
            "tasks": get_registered_jobs(),
        }
        return TemplateResponse(request, self.change_list_template, context)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
