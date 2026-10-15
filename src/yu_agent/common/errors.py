"""Yu-Agent 统一异常体系。"""


class YuAgentError(Exception):
    """所有 Yu-Agent 异常的基类。"""


class ConfigError(YuAgentError):
    """配置加载 / 校验失败。"""


class ModelError(YuAgentError):
    """模型调用失败（网络、鉴权、超时、返回异常）。"""


class ModelAuthError(ModelError):
    """API Key 无效或鉴权失败。"""


class EnvError(YuAgentError):
    """环境（屏幕捕获 / 权限 / 平台）错误。"""


class GroundingError(YuAgentError):
    """定位失败：无法把模型输出映射到屏幕坐标。"""


class ActionError(YuAgentError):
    """动作执行失败。"""


class SafetyViolation(YuAgentError):
    """触发了安全策略，动作被拦截。"""


class SkillNotFoundError(YuAgentError):
    """技能未注册。"""


class MemoryError(YuAgentError):
    """记忆读写异常。"""
