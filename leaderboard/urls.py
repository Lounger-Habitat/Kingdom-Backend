from django.urls import path
from . import views

urlpatterns = [
    # 排行榜历史浏览页面
    path("leaderboard/history/", views.leaderboard_history_page, name="leaderboard-history-page"),
    path("api/leaderboard/history/days", views.history_days, name="leaderboard-history-days"),
    path("api/leaderboard/history/<str:board_type>", views.history_detail, name="leaderboard-history-detail"),
    # 排行榜 API
    path("api/leaderboard/summary", views.leaderboard_summary, name="leaderboard-summary"),
    path("api/leaderboard/<str:board_type>/me", views.leaderboard_my_rank, name="leaderboard-my-rank"),
    path("api/leaderboard/<str:board_type>", views.leaderboard_detail, name="leaderboard-detail"),
    # 奖励 API
    path("api/reward/pending", views.reward_pending, name="reward-pending"),
    path("api/reward/claim", views.reward_claim, name="reward-claim"),
    # 数据上报 API
    path("api/report/heartbeat", views.report_heartbeat, name="report-heartbeat"),
    path("api/report/event", views.report_event, name="report-event"),
    path("api/report/batch", views.report_batch, name="report-batch"),
]
