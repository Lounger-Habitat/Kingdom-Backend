from game_time.models import GlobalGameTimeState


class ScheduledTaskDashboard(GlobalGameTimeState):
    """仅用于后台导航的代理模型；任务数据来自进程内注册表。"""

    class Meta:
        proxy = True
        verbose_name = "当前定时任务"
        verbose_name_plural = "当前定时任务"
