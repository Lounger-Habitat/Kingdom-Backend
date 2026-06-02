#!/usr/bin/env python
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    # 从 server_config.json 读取默认 host/port
    config_path = Path(__file__).resolve().parent / "server_config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            server_config = json.load(f)
    else:
        server_config = {}

    # 当直接运行 manage.py 且未指定子命令时，自动使用 runserver
    if len(sys.argv) == 1:
        sys.argv.append("runserver")

    # 当子命令是 runserver 且未指定 host:port 时，从配置文件读取
    if len(sys.argv) >= 2 and sys.argv[1] == "runserver" and len(sys.argv) == 2:
        host = server_config.get("host", "127.0.0.1")
        port = str(server_config.get("port", 8000))
        sys.argv.append(f"{host}:{port}")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
