"""Load and validate the Agent-visible village command catalog."""

from copy import deepcopy
import json
from pathlib import Path


CATALOG_PATH = Path(__file__).with_name("command_catalog.json")
_REQUIRED_COMMAND_FIELDS = {"name", "phase", "label", "description", "parameters"}


def load_command_catalog(path=CATALOG_PATH):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取指令配置文件：{path}") from exc

    actions = document.get("actions") if isinstance(document, dict) else None
    if not isinstance(actions, list):
        raise RuntimeError("指令配置必须包含 actions 数组")

    catalog = {}
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or not _REQUIRED_COMMAND_FIELDS.issubset(action):
            raise RuntimeError(f"actions[{index}] 缺少必要字段")
        name = action["name"]
        if not isinstance(name, str) or not name or name != name.upper():
            raise RuntimeError(f"actions[{index}].name 必须是非空大写字符串")
        if name in catalog:
            raise RuntimeError(f"指令名称重复：{name}")
        if not all(isinstance(action[field], str) and action[field] for field in ("phase", "label", "description")):
            raise RuntimeError(f"{name} 的阶段、名称和说明不能为空")

        parameters = action["parameters"]
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise RuntimeError(f"{name}.parameters 必须是 object Schema")
        properties = parameters.get("properties")
        required = parameters.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise RuntimeError(f"{name}.parameters 必须包含 properties 和 required")
        if len(required) != len(set(required)) or any(field not in properties for field in required):
            raise RuntimeError(f"{name}.required 包含重复或未定义字段")

        catalog[name] = {key: deepcopy(value) for key, value in action.items() if key != "name"}
    return catalog


COMMAND_CATALOG = load_command_catalog()


def agent_tool(action):
    command = COMMAND_CATALOG[action]
    return {
        "name": action,
        "description": command["description"],
        "parameters": deepcopy(command["parameters"]),
    }


def command_admin_label(action):
    command = COMMAND_CATALOG[action]
    return f'{command["phase"]} · {command["label"]}'


def command_admin_sort_key(action):
    command = COMMAND_CATALOG[action]
    phase_order = {"阶段一": 1, "阶段二": 2, "阶段三": 3, "阶段四": 4, "阶段五": 5}
    return phase_order.get(command["phase"], 99), command["label"]
