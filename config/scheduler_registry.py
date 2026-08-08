"""当前 Backend 进程内 APScheduler 实例的只读注册表。"""
from threading import RLock


_lock = RLock()
_schedulers = {}


def register_scheduler(source, scheduler):
    with _lock:
        _schedulers[source] = scheduler


def get_registered_jobs():
    """返回当前进程已启动的任务，用于 Django Admin 查看。"""
    schedule_labels = {
        "leaderboard_periodic_settlement": "每分钟第 5 秒（整点时结算周期）",
        "auto_buy": "每 5 分钟",
        "rotate_season": "每天 00:05",
        "settle_old_season": "每天 04:00",
    }
    task_names = {
        "leaderboard_periodic_settlement": "排行榜周期结算",
        "auto_buy": "自动购买执行",
        "rotate_season": "赛季轮转",
        "settle_old_season": "赛季结算",
    }
    descriptions = {
        "leaderboard_periodic_settlement": "检查并结算到期的北京时间排行榜周期",
        "auto_buy": "执行自动购买规则",
        "rotate_season": "创建或轮转赛季",
        "settle_old_season": "结算已结束赛季并迁移玩家",
    }
    with _lock:
        schedulers = list(_schedulers.items())

    jobs = []
    for source, scheduler in schedulers:
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": task_names.get(job.id, job.id),
                "source": source,
                "schedule": schedule_labels.get(job.id, str(job.trigger)),
                "description": descriptions.get(job.id, "后台定时任务"),
                "next_run_time": job.next_run_time,
            })
    return sorted(jobs, key=lambda job: (job["next_run_time"] is None, job["next_run_time"] or 0, job["id"]))
