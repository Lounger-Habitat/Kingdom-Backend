"""Rehearse the phase-two migration on an online SQLite copy, never the live DB."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


TARGETS = [("inventory", "0028_inventoryslotdish_inventoryslotreview_and_more"),
           ("marketplace", "0005_marketlistingdish_marketlistingreview_and_more"),
           ("village", "0042_villagestoreinventoryslotdish_and_more")]
OWNERS = [("inventory", "InventorySlot"), ("inventory", "NpcGlobalInventorySlot"),
          ("village", "VillageStoreInventorySlot"), ("marketplace", "MarketListing")]


def canonical(column, value):
    if column.endswith("_at") and isinstance(value, str):
        # SQLite may retain .000000 while Django's adapter omits zero micros.
        return datetime.fromisoformat(value)
    if column in {"dish_metadata", "item_attributes", "item_snapshot"}:
        if isinstance(value, str):
            value = json.loads(value)
        value = copy.deepcopy(value or {})
        if column == "item_attributes":
            for key, default in {"rated": False, "rating_price": -1, "overall_score": 0,
                                 "ingredient": "", "cook_time": 0, "review_comment": "",
                                 "dish_metadata": {}, "dish_quality": 3}.items():
                value.setdefault(key, default)
    return value


def read_table(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)}")
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fingerprints(connection, tables):
    return {table: hashlib.sha256(repr(sorted(read_table(connection, table), key=repr)).encode()).hexdigest()
            for table in tables}


class Command(BaseCommand):
    help = "在新 SQLite 副本上演练物品拆表，逐字段核对，并确认所有其他表完全不变（不迁移原库）"

    def add_arguments(self, parser):
        parser.add_argument("--copy", required=True, help="新的演练数据库路径，必须不存在")

    def handle(self, *args, **options):
        from django.apps import apps
        source = connections["default"]
        if source.vendor != "sqlite":
            raise CommandError("此在线副本演练命令只支持 SQLite；PostgreSQL 请在独立恢复库中演练。")
        source_path = Path(source.settings_dict["NAME"]).resolve()
        destination = Path(options["copy"]).resolve()
        if destination.exists() or destination == source_path or not destination.parent.is_dir():
            raise CommandError("演练目标必须是现有目录下尚不存在的新文件。")
        # sqlite backup API includes committed WAL data and never alters the source.
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as reader:
            with sqlite3.connect(destination) as writer:
                reader.backup(writer)
        alias = "batch_migration_rehearsal"
        config = copy.deepcopy(source.settings_dict)
        config["NAME"] = str(destination)
        connections.databases[alias] = config
        connection = connections[alias]
        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(TARGETS)
            if any(backwards or (migration.app_label, migration.name) not in TARGETS for migration, backwards in plan):
                raise CommandError("原库存在其他未应用迁移；停止演练，不跨阶段迁移。")
            if len(plan) != len(TARGETS):
                raise CommandError("原库不处于完整的拆表前状态；保留副本但不继续。")
            owners = [apps.get_model(app, name) for app, name in OWNERS]
            before = {model: read_table(connection, model._meta.db_table) for model in owners}
            excluded = {model._meta.db_table for model in owners} | {"django_migrations"}
            untouched = set(connection.introspection.table_names()) - excluded
            hashes = fingerprints(connection, untouched)
            executor.migrate(TARGETS)
            for model in owners:
                rows = {row.pk: row for row in model.objects.using(alias).all()}
                expected = before[model]
                if len(rows) != len(expected):
                    raise CommandError(f"{model.__name__}: 批次数量变化")
                fields = {field.column: field for field in model._meta.concrete_fields}
                for old in expected:
                    row = rows[old[model._meta.pk.column]]
                    for column, value in old.items():
                        if column in fields:
                            field = fields[column]
                            actual = field.get_db_prep_value(getattr(row, field.attname), connection)
                        else:
                            actual = getattr(row, column)
                        if canonical(column, actual) != canonical(column, value):
                            raise CommandError(f"{model.__name__} pk={row.pk}: {column} 不一致")
                self.stdout.write(f"{model.__name__}: {len(rows)} rows, every legacy field preserved")
            if hashes != fingerprints(connection, untouched):
                raise CommandError("非目标表发生变化，演练未通过")
            connection.check_constraints()
            self.stdout.write(self.style.SUCCESS(f"PASS: {len(untouched)} other tables unchanged; all PKs, IDs, quantities and attributes preserved. Copy: {destination}"))
        finally:
            connection.close()
