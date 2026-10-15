"""配置加载：default.yaml + 用户配置（GUI 设置持久化）+ 环境变量。

优先级：环境变量 > 用户配置(user.yaml) > default.yaml
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field

from .common.errors import ConfigError
from .common.paths import project_root, user_config_dir

# 用户配置（API key 等敏感信息）持久化位置
_USER_CONFIG_NAME = "user.yaml"


class ModelConfig(BaseModel):
    """单个模型后端的配置（用户在设置页填写）。"""

    provider: str = "openai"  # 目前主要支持 openai 兼容接口
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_s: float = 60.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


class AgentConfig(BaseModel):
    max_steps: int = 20
    step_timeout_s: int = 60
    task_timeout_s: int = 900
    max_consecutive_failures: int = 3
    # ── 防重复 / 防卡死（避免「一直点停不下来」）──
    max_repeated_actions: int = 3  # 连续多少次完全相同的动作判定卡住
    max_same_action_total: int = 3  # 同一目标累计点击多少次判定卡住
    max_same_action_total_other: int = 8  # 同一按键/滚动/输入类动作累计上限
    max_total_clicks: int = 12  # 单个任务的点击总预算
    max_no_change_steps: int = 3  # 屏幕连续无变化多少次判定卡住
    max_plan_rejects: int = 2  # 重复拆分子任务多少次判定死循环并停止
    stop_on_grounding_error: bool = True
    dry_run: bool = False


class Settings:
    """全局配置单例。"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw
        self.agent = AgentConfig(**raw.get("agent", {}))
        self.models: Dict[str, ModelConfig] = self._load_models(raw)
        self.loop = raw.get("loop", {})
        self.perception = raw.get("perception", {})
        self.grounding = raw.get("grounding", {})
        self.safety = raw.get("safety", {})
        self.memory = raw.get("memory", {})
        self.paths = raw.get("paths", {})

    def _load_models(self, raw: Dict[str, Any]) -> Dict[str, ModelConfig]:
        """从 raw['models'] 构建后端配置。

        支持两种来源：
        1. config/models/*.yaml 里的 profile（通过 profiles 字段）
        2. 用户配置 user.yaml 里直接写的 model 配置（GUI 设置页）
        """
        result: Dict[str, ModelConfig] = {}
        # 用户直接配置（设置页填写）
        model_cfg = raw.get("model_config")
        if isinstance(model_cfg, dict):
            result["default"] = ModelConfig(**model_cfg)
        return result

    def get_model(self, role: str = "default") -> ModelConfig:
        """按角色取模型。当前简化为单模型（设置页配置的那一个）。"""
        cfg = self.models.get("default")
        if cfg is None:
            cfg = ModelConfig()
        return cfg


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并两个 dict，override 优先。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_default_config() -> Dict[str, Any]:
    """加载 config/default.yaml。"""
    p = project_root() / "config" / "default.yaml"
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_user_config() -> Dict[str, Any]:
    """加载用户配置（GUI 设置持久化，含 API key）。"""
    p = user_config_dir() / _USER_CONFIG_NAME
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_user_config(cfg: Dict[str, Any]) -> None:
    """持久化用户配置（用于 GUI 设置页保存）。"""
    p = user_config_dir() / _USER_CONFIG_NAME
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def get_saved_models() -> list[Dict[str, str]]:
    """获取用户保存过的所有模型配置（用于 GUI 快速切换）。

    返回列表，每项含 name/base_url/api_key/model。name 为 model 名（可带 base_url 简写）。
    """
    cfg = load_user_config()
    models = cfg.get("saved_models", [])
    result: list[Dict[str, str]] = []
    for m in models:
        if isinstance(m, dict) and m.get("model"):
            result.append(
                {
                    "name": m.get("name") or m["model"],
                    "base_url": m.get("base_url", ""),
                    "api_key": m.get("api_key", ""),
                    "model": m["model"],
                }
            )
    return result


def add_saved_model(name: str, base_url: str, api_key: str, model: str) -> None:
    """保存一个新的模型配置到历史（去重：同名+同base_url 视为已存在则更新）。"""
    cfg = load_user_config()
    models = cfg.get("saved_models", [])
    # 去重
    models = [
        m for m in models
        if not (m.get("model") == model and m.get("base_url") == base_url)
    ]
    models.append(
        {"name": name, "base_url": base_url, "api_key": api_key, "model": model}
    )
    cfg["saved_models"] = models
    save_user_config(cfg)


def switch_model(model: str, base_url: str) -> bool:
    """把某个已保存的模型设为当前使用，返回是否成功。"""
    for m in get_saved_models():
        if m["model"] == model and m["base_url"] == base_url:
            cfg = load_user_config()
            cfg["model_config"] = {
                "base_url": m["base_url"],
                "api_key": m["api_key"],
                "model": m["model"],
            }
            save_user_config(cfg)
            reload_settings()
            return True
    return False


def _apply_env(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把环境变量覆盖到配置上（密钥优先从环境变量读）。"""
    out = copy.deepcopy(raw)
    mc = out.setdefault("model_config", {})

    env_map = {
        "OPENAI_API_KEY": "api_key",
        "OPENAI_BASE_URL": "base_url",
        "OPENAI_MODEL": "model",
    }
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            mc[key] = val

    api_key = os.environ.get("API_KEY")
    if api_key:
        mc["api_key"] = api_key
    base_url = os.environ.get("BASE_URL")
    if base_url:
        mc["base_url"] = base_url
    model = os.environ.get("MODEL_NAME")
    if model:
        mc["model"] = model

    return out


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """获取全局配置单例（懒加载）。"""
    global _settings
    if _settings is None:
        reload_settings()
    return _settings  # type: ignore


def reload_settings() -> Settings:
    """重新加载配置（设置页保存后调用）。"""
    global _settings
    raw = _deep_merge(load_default_config(), load_user_config())
    raw = _apply_env(raw)
    _settings = Settings(raw)
    return _settings
