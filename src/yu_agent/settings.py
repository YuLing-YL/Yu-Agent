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
    max_steps: int = 30
    step_timeout_s: int = 60
    task_timeout_s: int = 900
    max_consecutive_failures: int = 3
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
