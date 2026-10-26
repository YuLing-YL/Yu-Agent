"""配置加载：default.yaml + 用户配置（GUI 设置持久化）+ 环境变量。

优先级：环境变量 > 用户配置(user.yaml) > default.yaml

设计要点（别再退回无序 dict）：
1. 每个配置段都是 pydantic 模型，且 `extra="forbid"` —— 键名拼错/写错位置会**当场报出来**，
   而不是被 `.get(..., 默认值)` 静默吞掉（历史上 default.yaml 少一个换行，
   `stop_on_grounding_error` 整行被 YAML 当注释吃掉，几个月都没人发现）。
2. 段对象保留 `.get(key, default)`：老代码 `settings.loop.get("verify_after_action", True)`
   不用改，但值已经过校验、有类型。
3. user.yaml 里可能存在历史遗留键（如已删除功能的 `agent.reset_desktop`）：
   这类**已知废弃键**会被剔除并打警告，不让它把整个启动流程炸掉。
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .common.errors import ConfigError
from .common.paths import project_root, user_config_dir

# 用户配置（API key 等敏感信息）持久化位置
_USER_CONFIG_NAME = "user.yaml"
# 用户配置权限：只允许本人读写（里面有 API key，默认 644 是同机可读）
_USER_CONFIG_MODE = 0o600


class _Section(BaseModel):
    """配置段基类：禁止未知键，同时保留 dict 风格的 .get()。"""

    model_config = ConfigDict(extra="forbid")

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ModelConfig(BaseModel):
    """单个模型后端的配置（用户在设置页填写）。"""

    # 这里允许额外键：不同 provider 会带自己的参数（top_p / extra_headers…）
    model_config = ConfigDict(extra="allow")

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


class AgentConfig(_Section):
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
    type_interval_s: float = 0.02  # 逐字输入每个字符的间隔（只影响纯 ASCII）
    stop_on_grounding_error: bool = True  # 定位失败是否立刻终止
    # ── 元素标注（SOM）──
    # marks = 截图上画编号框，模型输出编号（默认，最准）
    # coords = 不画框，模型直接给像素坐标（坐标会吸附最近的元素）
    # off = 完全不枚举元素，纯看图给坐标（最省，最容易点偏）
    som_mode: str = "marks"
    som_max_marks: int = 80  # 单帧最多标多少个（太多会互相重叠、模型看不过来）
    som_frontmost_only: bool = True  # 只标最前面的应用 + 程序坞
    som_content_check: bool = True  # 用截图像素校准元素框（空框丢弃 / 过大框收缩到内容）
    dry_run: bool = False


class LoopConfig(_Section):
    verify_after_action: bool = True
    verify_delay_s: float = 0.5
    screenshot_diff_threshold: float = 0.002
    verify_goal: bool = True
    max_goal_rejects: int = 1
    max_replans: int = 1
    ocr_anchor: bool = True
    som_ocr: bool = True
    som_ocr_max: int = 30
    som_ocr_min_conf: float = 0.30
    som_ocr_skip_ax_elements: int = 40
    som_dock_max_marks: int = 20
    som_max_apps: int = 1
    save_trace: bool = True
    runs_keep: int = 50
    require_approval_for_risky: bool = True
    model_pricing: Dict[str, float] = {}


class PerceptionConfig(_Section):
    max_image_side: int = 1568
    use_som: bool = True
    a11y_max_depth: int = 24
    a11y_max_elements: int = 400


class GroundingConfig(_Section):
    text_match_threshold: float = 0.62
    prefer_a11y_over_pixels: bool = True
    coords_mode: str = "pixel"


class SafetyConfig(_Section):
    require_approval_for_risky: bool = True
    deny_shell_by_default: bool = True
    forbidden_action_types: List[str] = ["run_shell"]
    redact_secrets_in_traces: bool = True


class MemoryConfig(_Section):
    enabled: bool = True
    store: str = "sqlite"
    sqlite_path: str = "data/memory/yu_agent.sqlite"
    # 各层细项由各自实现读默认值，这里只透传（允许额外键，不强校验子项）
    working: Dict[str, Any] = {}
    retrieval: Dict[str, Any] = {}
    semantic: Dict[str, Any] = {}
    procedural: Dict[str, Any] = {}
    distillation: Dict[str, Any] = {}


class PathsConfig(_Section):
    runs_dir: str = "runs"
    cache_dir: str = ".cache/vlm"


class PromptsConfig(_Section):
    language: str = "en"


# 用户配置里出现过、但已经删除的历史键：直接剔除，不让它挡住启动
_OBSOLETE_KEYS: Dict[str, set] = {
    "agent": {"reset_desktop", "som_max_elements", "include_a11y", "include_screenshot"},
    "loop": {"history_window", "som", "som_max_marks"},
    "perception": {"som_max_elements", "screenshot_format"},
    "memory": {"ab_test_flag"},
    "paths": {"prompts_dir"},
}


def _build_section(cls: type, raw: Dict[str, Any], name: str):
    """构建一个配置段；未知键剔除并警告，其它校验错误直接抛 ConfigError。"""
    data = dict(raw or {})
    for key in _OBSOLETE_KEYS.get(name, ()):  # 已知废弃键：静默剔除
        data.pop(key, None)
    try:
        return cls(**data)
    except ValidationError as e:
        extra = sorted(
            {str(err["loc"][0]) for err in e.errors() if err.get("type") == "extra_forbidden"}
        )
        if not extra:
            raise ConfigError(f"配置段 {name} 校验失败：{e}") from e
        for key in extra:
            data.pop(key, None)
        from .common.logging import get_logger

        get_logger("yu_agent.settings").warning(
            "配置段 %s 里出现未知键 %s，已忽略（检查 config/default.yaml 或 user.yaml 的拼写）",
            name,
            ", ".join(extra),
        )
        try:
            return cls(**data)
        except ValidationError as e2:
            raise ConfigError(f"配置段 {name} 校验失败：{e2}") from e2


class Settings:
    """全局配置。"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw
        self.agent = _build_section(AgentConfig, raw.get("agent", {}), "agent")
        self.loop = _build_section(LoopConfig, raw.get("loop", {}), "loop")
        self.perception = _build_section(PerceptionConfig, raw.get("perception", {}), "perception")
        self.grounding = _build_section(GroundingConfig, raw.get("grounding", {}), "grounding")
        self.safety = _build_section(SafetyConfig, raw.get("safety", {}), "safety")
        self.memory = _build_section(MemoryConfig, raw.get("memory", {}), "memory")
        self.paths = _build_section(PathsConfig, raw.get("paths", {}), "paths")
        self.prompts = _build_section(PromptsConfig, raw.get("prompts", {}), "prompts")
        self.models: Dict[str, ModelConfig] = self._load_models(raw)

    def _load_models(self, raw: Dict[str, Any]) -> Dict[str, ModelConfig]:
        """从 user.yaml 的 `model_config` 构建当前使用的后端。

        当前是**单模型**实现：planner / grounder / reflector 共用同一个后端。
        （早期文档里的「大小模型分工」并未实现，这里不再假装支持 role。）
        """
        result: Dict[str, ModelConfig] = {}
        model_cfg = raw.get("model_config")
        if isinstance(model_cfg, dict):
            result["default"] = ModelConfig(**model_cfg)
        return result

    def get_model(self, role: str = "default") -> ModelConfig:  # noqa: ARG002
        """取模型配置。当前单模型，role 仅作保留参数（不假装支持大小模型分工）。"""
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
    """持久化用户配置。文件里有 API key，权限收到 600。"""
    p = user_config_dir() / _USER_CONFIG_NAME
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    try:
        p.chmod(_USER_CONFIG_MODE)
    except OSError:
        pass  # Windows 上 chmod 基本无效，不影响功能


def get_saved_models() -> list[Dict[str, str]]:
    """获取用户保存过的所有模型配置（用于 GUI 快速切换）。"""
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


def set_agent_option(key: str, value: Any) -> None:
    """把单个 `agent.*` 选项写进用户配置并重新加载（GUI 开关用）。

    键名必须存在于 AgentConfig，否则直接报错——不然又会写出
    `reset_desktop` 这种没人读、还会挡住下次启动的孤儿键。
    """
    if key not in AgentConfig.model_fields:
        raise ConfigError(f"未知的 agent 配置项：{key}")
    cfg = load_user_config()
    agent = cfg.get("agent")
    if not isinstance(agent, dict):
        agent = {}
    agent[key] = value
    cfg["agent"] = agent
    save_user_config(cfg)
    reload_settings()


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
    return _settings  # type: ignore[return-value]


def reload_settings() -> Settings:
    """重新加载配置（设置页保存后调用）。"""
    global _settings
    raw = _deep_merge(load_default_config(), load_user_config())
    raw = _apply_env(raw)
    _settings = Settings(raw)
    return _settings
