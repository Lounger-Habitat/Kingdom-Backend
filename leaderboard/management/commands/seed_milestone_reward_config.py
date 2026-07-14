from django.core.management.base import BaseCommand
from leaderboard.models import RewardConfig, BoardType, SettlementType


class Command(BaseCommand):
    help = "初始化里程碑（总榜）奖励规则配置"

    def handle(self, *args, **options):
        configs = [
            # 总富豪榜 - 里程碑结算
            {"board_type": BoardType.TOTAL_WEALTH, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "money", "reward_data": {"amount": 500}},
            {"board_type": BoardType.TOTAL_WEALTH, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "title", "reward_data": {"titleId": "milestone_wealth_1", "titleName": "财富里程碑"}},
            {"board_type": BoardType.TOTAL_WEALTH, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "item", "reward_data": {"itemId": 3033, "amount": 1}},
            {"board_type": BoardType.TOTAL_WEALTH, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 2, "rank_max": 3, "reward_type": "money", "reward_data": {"amount": 300}},
            # 总产量榜 - 里程碑结算
            {"board_type": BoardType.TOTAL_OUTPUT, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "money", "reward_data": {"amount": 500}},
            {"board_type": BoardType.TOTAL_OUTPUT, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "title", "reward_data": {"titleId": "milestone_output_1", "titleName": "产量里程碑"}},
            {"board_type": BoardType.TOTAL_OUTPUT, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "item", "reward_data": {"itemId": 3034, "amount": 1}},
            {"board_type": BoardType.TOTAL_OUTPUT, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 2, "rank_max": 3, "reward_type": "money", "reward_data": {"amount": 300}},
            # 总极鲜榜 - 里程碑结算
            {"board_type": BoardType.TOTAL_FRESHNESS, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "money", "reward_data": {"amount": 500}},
            {"board_type": BoardType.TOTAL_FRESHNESS, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "title", "reward_data": {"titleId": "milestone_fresh_1", "titleName": "极鲜里程碑"}},
            {"board_type": BoardType.TOTAL_FRESHNESS, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 1, "rank_max": 1, "reward_type": "item", "reward_data": {"itemId": 3032, "amount": 1}},
            {"board_type": BoardType.TOTAL_FRESHNESS, "settlement_type": SettlementType.MILESTONE,
             "rank_min": 2, "rank_max": 3, "reward_type": "money", "reward_data": {"amount": 300}},
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

        total = RewardConfig.objects.filter(settlement_type=SettlementType.MILESTONE).count()
        self.stdout.write(self.style.SUCCESS(
            f"里程碑奖励规则初始化完成：新增 {created} 条，里程碑结算共 {total} 条"
        ))
