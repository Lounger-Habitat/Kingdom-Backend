from django.core.management.base import BaseCommand
from leaderboard.models import RewardConfig, BoardType, SettlementType


class Command(BaseCommand):
    help = "初始化赛季结算奖励规则配置"

    def handle(self, *args, **options):
        configs = [
            # 富豪榜 - 赛季结算
            {"board_type": BoardType.WEALTH, "settlement_type": SettlementType.SEASON,
             "rank_min": 1, "rank_max": 1, "reward_type": "actionPoint", "reward_data": {"amount": 50}},
            {"board_type": BoardType.WEALTH, "settlement_type": SettlementType.SEASON,
             "rank_min": 1, "rank_max": 1, "reward_type": "title", "reward_data": {"titleId": "season_wealth_1", "titleName": "赛季首富"}},
            {"board_type": BoardType.WEALTH, "settlement_type": SettlementType.SEASON,
             "rank_min": 2, "rank_max": 3, "reward_type": "actionPoint", "reward_data": {"amount": 30}},
            # 产量榜 - 赛季结算
            {"board_type": BoardType.OUTPUT, "settlement_type": SettlementType.SEASON,
             "rank_min": 1, "rank_max": 1, "reward_type": "actionPoint", "reward_data": {"amount": 50}},
            {"board_type": BoardType.OUTPUT, "settlement_type": SettlementType.SEASON,
             "rank_min": 1, "rank_max": 1, "reward_type": "title", "reward_data": {"titleId": "season_output_1", "titleName": "赛季产量王"}},
            {"board_type": BoardType.OUTPUT, "settlement_type": SettlementType.SEASON,
             "rank_min": 2, "rank_max": 3, "reward_type": "actionPoint", "reward_data": {"amount": 30}},
            # 极鲜榜 - 赛季结算
            {"board_type": BoardType.FRESHNESS, "settlement_type": SettlementType.SEASON,
             "rank_min": 1, "rank_max": 1, "reward_type": "actionPoint", "reward_data": {"amount": 50}},
            {"board_type": BoardType.FRESHNESS, "settlement_type": SettlementType.SEASON,
             "rank_min": 1, "rank_max": 1, "reward_type": "title", "reward_data": {"titleId": "season_fresh_1", "titleName": "赛季极鲜王"}},
            {"board_type": BoardType.FRESHNESS, "settlement_type": SettlementType.SEASON,
             "rank_min": 2, "rank_max": 3, "reward_type": "actionPoint", "reward_data": {"amount": 30}},
        ]

        created = 0
        for cfg in configs:
            obj, is_new = RewardConfig.objects.get_or_create(
                board_type=cfg["board_type"],
                settlement_type=cfg["settlement_type"],
                rank_min=cfg["rank_min"],
                rank_max=cfg["rank_max"],
                reward_type=cfg["reward_type"],
                defaults={"reward_data": cfg["reward_data"]},
            )
            if is_new:
                created += 1

        total = RewardConfig.objects.filter(settlement_type=SettlementType.SEASON).count()
        self.stdout.write(self.style.SUCCESS(
            f"赛季结算奖励规则初始化完成：新增 {created} 条，赛季结算共 {total} 条"
        ))
