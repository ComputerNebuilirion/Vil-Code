"""Vil 全局配置（~/.vil/config.json）

仅全局层。项目层（.vil/）和 init 命令以后再做。

用法：
    from VilAgent import load_config, set_value, get_value
    set_value("llm.endpoint", "http://localhost:11434/v1")
    cfg = load_config()   # 与 DEFAULTS 合并
"""
import json
from pathlib import Path

# 语义化版本号：MAJOR.MINOR.PATCH
# MAJOR：breaking 变更；MINOR：新 feature（向后兼容）；PATCH：bug fix
__version__ = "1.0.0"

CONFIG_DIR = Path.home() / ".vil"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict = {
    "llm": {"endpoint": "", "model": "", "api_key": None, "temperature": 0.2},
    "stream": True,
    "default_mode": "ask",
    "max_steps": 50,
    # 单轮 run 的硬上限：到达 max_steps 后若仍在推进，自动续跑到此值才停
    # （<= max_steps 时视作不启用自动续跑，等价于原来的单段上限）
    "max_steps_total": 200,
    # 上下文 token 预算：历史超此值时触发旧消息摘要（loop._maybe_summarize）。
    # 置为 null 则关闭 token 预算模式（退回按条数滑窗，不自动摘要）。
    "context_budget": 8000,
    # 输出语言：cn（中文）/ en（英文）；见 VilAgent/i18n.py
    "lang": "cn",
    # 权限层级（见 safety/permissions.py）：
    #   strict   所有非只读操作都要用户确认
    #   balanced 只有命令/代码执行 + 删除类工具需要确认（默认）
    #   auto     静态检查 risk=low 的 run_command 直接放行，medium/high 才确认
    "permission_level": "balanced",
    # 需要用户确认 run_command 时，是否调一次模型总结该命令的目的（提示给人看）
    "permission_summary": True,
}


def _ensure() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text("{}", encoding="utf-8")
    return CONFIG_FILE


def _read_raw() -> dict:
    _ensure()
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """读取全局配置，与 DEFAULTS 深合并（用户值覆盖默认）"""
    return _merge(DEFAULTS, _read_raw())


def save_config(cfg: dict) -> None:
    """整体写入（覆盖）"""
    _ensure()
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_value(key: str):
    """dotted key 读取，如 get_value('llm.endpoint')"""
    cur = load_config()
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_value(key: str, value) -> None:
    """dotted key 设值（只写用户填的字段，不把 DEFAULTS 污染进文件）"""
    cfg = _read_raw()
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
    save_config(cfg)
