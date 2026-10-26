# Yu-Agent 项目结构说明

本文档说明**每个文件夹存放什么**、**每个文件的作用**。内容依据当前工作区的真实文件核对整理（部分目录为结构占位，已在状态列注明）。

核心闭环：

```
observe(截屏) → recall(取记忆) → think(规划/拆分) → ground(定位坐标)
   → act(执行) → verify(校验) → reflect(反思) → distill(任务结晶记忆)
```

---

## 一、顶层目录一览

| 目录 | 存放什么 | 是否入库 | 状态 |
|---|---|---|---|
| `src/yu_agent/` | **唯一可安装的 Python 包**，全部业务代码 | 是 | 已实装（约 5900 行） |
| `config/` | 非代码配置：全局默认参数 | 是（密钥除外） | 已实装 |
| `data/` | 运行时数据：用户配置、日志、长期记忆库 | 目录入库，数据不入库 | 已实装 |
| `runs/` | 每次运行的产物（截图 + 轨迹），一次性 | **整目录忽略** | 已实装 |
| `tests/` | 测试：冒烟、停止逻辑等 | 是 | 部分实装 |
| `scripts/` | 开发与启动脚本 | 是 | 已实装 |
| `eval/` | 评测：任务用例（tasks.yaml）、判据、运行器、报告 | 用例入库，报告不入库 | 已实装 |
| `.workbuddy/` | 工具私有记忆（项目工作日志） | 忽略 | 自动维护 |

---

## 二、顶层文件

| 文件 | 作用 |
|---|---|
| `README.md` | 项目总览：定位、核心特性、快速开始、目录结构、记忆分层、依赖规则 |
| `COMMANDS.md` | **命令速查 + 排障手册**：全部 CLI 子命令、点击不准/漏元素/重复点击等问题的排查方法 |
| `PROJECT_STRUCTURE.md` | 本文件，文件夹与文件职责对照 |
| `pyproject.toml` | 打包元数据 + 依赖分组 + `yu-agent` 命令入口。核心依赖仅 `pydantic/pyyaml/pillow`；可选组 `macos/gui/browser/ocr/openai/anthropic/local/dev` |
| `Makefile` | 任务入口：`help/venv/install/dev/macos/test/lint/fmt/doctor/screenshot/demo/gui/clean` |
| `.env.example` | 密钥模板（各 VLM 后端的 API key / base_url + 运行时开关），复制为 `.env` 使用；真实 `.env` 不入库 |
| `.gitignore` | 忽略 `runs/`、`.env`、`__pycache__`、`*.sqlite`、`data/*`、`.workbuddy/` 等 |

---

## 三、`src/yu_agent/`（核心包）

### 3.1 包根

| 文件 | 作用 | 状态 |
|---|---|---|
| `__init__.py` | 导出 `YuAgent`、`build_agent_from_settings` 等顶层 API，声明版本 | 已实现 |
| `__main__.py` | `python -m yu_agent` 入口，转调 `cli.main()` | 已实现 |
| `cli.py` | 命令行子命令：`gui / run / test / perceive / screenshot / logs / doctor / replay` | 已实现（258 行） |
| `settings.py` | 配置加载：`default.yaml` + 用户配置(`user.yaml`) + 环境变量，优先级 环境变量 > 用户配置 > 默认；含模型切换 `switch_model` / `get_saved_models` | 已实现（224 行） |
| `agent_facade.py` | **顶层门面**：组装配置/模型/记忆/技能/主循环，供 GUI 与 CLI 复用；含 `build_test_agent`（mock + 内存记忆，离线测试用） | 已实现 |

### 3.2 `common/` — 通用工具（最底层，不依赖任何业务模块）

| 文件 | 作用 |
|---|---|
| `__init__.py` | 通用工具包声明 |
| `errors.py` | 统一异常体系：`YuAgentError / ConfigError / ModelError / EnvError / GroundingError / SafetyViolation / SkillNotFoundError` |
| `logging.py` | 日志初始化，同时输出控制台与文件 `data/logs/yu_agent.log`（轮转保留 3 份） |
| `paths.py` | 项目路径管理：定位项目根、`runs/<时间戳>` 目录分配、数据目录 |
| `image.py` | 图像工具：缩放、base64 编码、图像差分、感知哈希 |
| `tracing.py` | 步骤追踪：把每步（截图 + 动作 + 结果 + token + 耗时）写入 `runs/<ts>/trace.jsonl` |

### 3.3 `models/` — VLM 后端（只负责"调模型"）

| 文件 | 作用 |
|---|---|
| `__init__.py` | 导出 `VLMBackend / VLMRequest / VLMResponse / TokenUsage` |
| `base.py` | VLM 后端 Protocol 与数据结构（请求/响应/Token 用量） |
| `registry.py` | 后端注册表：provider 名 → 后端工厂 |
| `router.py` | 模型路由：角色(planner/grounder/reflector) → 后端（当前简化为单模型） |
| `cache.py` | 响应缓存：开发期避免重复计费，回放测试依赖它（哈希键） |
| `mock.py` | 脚本化假后端：按 prompt 指令返回预设动作，离线跑通闭环 |
| `openai_vlm.py` | OpenAI 兼容接口（覆盖 OpenAI / Gemini / Qwen / DeepSeek / Kimi / 本地 vLLM） |
| `anthropic_vlm.py` | Claude 后端（走 Anthropic 的 OpenAI 兼容端点） |
| `qwen_vl.py` | 通义千问 VL（DashScope 兼容模式，复用 OpenAICompatBackend） |
| `local_vllm.py` | 本地 vLLM / Ollama（OpenAI 兼容端点，无需 key） |

### 3.4 `perception/` — 「看」：产出 Observation

| 文件 | 作用 |
|---|---|
| `__init__.py` | 导出 `Observation` 等 |
| `observation.py` | `Observation` 数据结构：图像 + 元素树 + 窗口 + 屏幕几何 |
| `screenshot.py` | 跨平台截屏（macOS `screencapture`/Quartz，负责把物理像素缩回逻辑像素） |
| `a11y.py` | **辅助功能树（AXUIElement）**：枚举可交互元素的位置/尺寸/角色/标题，SOM 标记的数据来源（717 行，核心） |
| `som.py` | Set-of-Mark：在截图上给元素画编号框，模型输出编号即可精确定位 |
| `ocr.py` | OCR 兜底：无 a11y 时从像素取文字（需 pytesseract） |
| `ui_tree.py` | UI 元素树结构化表示（可选增强，当前极简） |

### 3.5 `env/` — 「在哪操作」：平台驱动

| 文件 | 作用 |
|---|---|
| `__init__.py` | 环境包声明，按平台选择实现 |
| `base.py` | `GUIEnv` Protocol：`start/observe/screen_geometry/primitives/close` |
| `macos.py` | macOS 主实现（**首要目标平台**） |
| `windows.py` | Windows 实现（未验证） |
| `linux.py` | Linux 实现（未验证） |
| `browser_cdp.py` | 浏览器环境：走 CDP/DOM，比点像素可靠 |
| `mock.py` | 假环境：合成截图 + 记录动作（离线回放测试用） |

### 3.6 `actions/` — 「做什么」：动作契约与执行

| 文件 | 作用 |
|---|---|
| `__init__.py` | 导出 `Action / ActionExecutor / describe_actions` |
| `schema.py` | ★ **动作空间 pydantic 契约**（click/type/key/scroll/drag/wait/open_app/skill/done/ask_user...），全项目中心接口，不 import 其他模块 |
| `executor.py` | 动作 → 原语分发，含重试、超时、安全闸门、停止标志确认 |
| `primitives/__init__.py` | 原子操作包声明 |
| `primitives/base.py` | `PrimitiveDriver` Protocol（click_at/type_text/key_press/scroll/drag），占位保留扩展位 |
| `primitives/pyautogui_driver.py` | pyautogui 驱动的跨平台薄封装 |

### 3.7 `grounding/` — 「落到哪一点」：坐标定位（最易失败环节）

| 文件 | 作用 |
|---|---|
| `__init__.py` | 导出 `GroundingResolver / parse_action / parse_actions` |
| `parser.py` | 从模型文本里抽出 JSON 动作并做 schema 校验 |
| `coords.py` | 归一化坐标 ↔ 像素坐标换算、Retina/DPI 缩放 |
| `matcher.py` | 目标描述 → a11y 元素 / OCR 文本匹配（difflib 相似度） |
| `resolver.py` | 综合定位：显式坐标 > 元素 id > 文本匹配 > SOM 视觉定位 |
| `verify.py` | 动作后校验：屏幕是否变化、期望文字是否出现 |

### 3.8 `safety/` — 安全闸门

| 文件 | 作用 |
|---|---|
| `__init__.py` | 导出 `approve_action` |
| `policy.py` | 安全策略：危险动作类型、危险 shell、删除/支付/发送类文案拦截 + 提示词注入防护 |
| `approval.py` | 动作审批：自动放行 / 控制台询问 / 全拒（测试用） |

### 3.9 `prompts/` — 版本化提示词（纯文本，改 prompt 不碰代码）

| 文件 | 作用 |
|---|---|
| `loader.py` | 加载模板并做 `{{var}}` 变量替换 |
| `system.md` | 系统角色与输出格式约定（含"整个任务最多只允许输出一次 plan"等约束） |
| `planner.md` | 规划：给定任务 + 历史 + 元素列表 → 下一个动作（预留记忆槽位） |
| `grounder.md` | 定位：把目标描述转成坐标 |
| `reflector.md` | 反思：卡住/失败时的自我批评与纠偏 |

### 3.10 `memory/` — 分层记忆（独立包，被 planner / reflect / 离线蒸馏共用）

| 文件 | 作用 |
|---|---|
| `__init__.py` | `MemoryManager` 统一入口，汇总各层记忆 |
| `base.py` | `MemoryRecord` + `Memory` 接口（write/search/forget/feedback） |
| `working.py` | L1 工作记忆：任务内轨迹窗口、scratchpad、上下文预算裁剪（只留最近 1–2 张图） |
| `procedural.py` | L2 程序性记忆：可复用技能模板（参数化，复用前必须重新校验） |
| `semantic.py` | L3 语义记忆：环境知识（app/元素/快捷键/登录态，带失效字段） |
| `episodic.py` | L4 情景记忆：过往完整轨迹存取与检索（few-shot 来源） |
| `profile.py` | 用户画像：偏好、常用 App、安全偏好 |
| `store.py` | 记忆存储后端：SQLite(FTS5) / JSONL / 内存（测试用） |
| `retrieval.py` | 检索与重排：关键词/BM25 + app 过滤（向量暂不做） |
| `distillation.py` | 离线蒸馏：任务轨迹 → 技能/教训（成功与失败分开结晶） |

### 3.11 `agent/` — 循环本体

| 文件 | 作用 |
|---|---|
| `__init__.py` | 导出 `AgentLoop / Planner / Reflector / Responder / AgentState` |
| `loop.py` | ★ **核心主循环**：`AgentLoop.run(task) -> Trajectory`，含多层自动停止、注入防护、子任务推进（865 行） |
| `planner.py` | 组装 prompt（历史 + 元素 + 截图 + 记忆提示）并调用模型 |
| `responder.py` | 意图识别 + 回复：先判断输入是否需要操作屏幕，纯闲聊只回复不调 agent |
| `reflect.py` | 反思者：卡死检测（重复动作/无变化）与任务结束后的总结纠偏 |
| `state.py` | 运行状态：`Task / StepRecord / Trajectory / RunState / AgentState` |

### 3.12 `skills/` — 技能系统（`README` 早期未列，实际已实装）

| 文件 | 作用 |
|---|---|
| `__init__.py` | 技能包入口，导出 `build_default_registry` |
| `registry.py` | 技能注册表：技能名 + 参数说明注入 prompt，模型可返回 `{"type":"skill",...}` 调用 |
| `builtin.py` | 内置技能：`open_app / type_text / press_hotkey / click_text / take_screenshot / wait` |

### 3.13 `gui/` — 图形界面（PySide6，`README` 早期未列，实际已实装）

| 文件 | 作用 |
|---|---|
| `__init__.py` | GUI 包入口，导出 `MainWindow / run_gui` |
| `app.py` | 主窗口：需求输入框 + 模型切换下拉 + 实时截图预览 + 执行日志（474 行） |
| `floating.py` | 悬浮控制条：任务运行时置顶、跨桌面可见，含停止/唤回主窗口、可拖动（200 行） |
| `settings_dialog.py` | 设置对话框：API 配置（base_url / api_key / 模型名）+ 测试连接（170 行） |
| `theme.py` | GUI 主题样式（QSS）（265 行） |
| `worker.py` | 后台执行线程：先做意图识别，再决定是否调用 GUI agent |

---

## 四、其他目录

### `config/`

| 文件 | 作用 |
|---|---|
| `default.yaml` | 全局默认参数：`agent`（步数/超时/各类防重复阈值）、`loop`（历史窗口/SOM/校验）、`perception`（缩放/标记上限）、`grounding`（匹配阈值/坐标模式）、`safety`（审批/禁用 shell）、`models`（角色→profile）、`memory`（各层预算/失效规则/AB 开关）、`paths` |

> 说明：`README` 曾规划 `config/experiments/`（实验覆盖）与 `config/models/`（模型 profile），当前**尚未创建**，模型配置实际落在 `data/config/user.yaml`。

### `data/`（运行时数据）

| 路径 | 作用 |
|---|---|
| `data/.gitkeep` | 占位，保证目录入库 |
| `data/config/user.yaml` | **GUI 设置页保存的模型配置**：`model_config`（当前模型）+ `saved_models`（历史模型列表），已被 gitignore |
| `data/logs/yu_agent.log` | 运行日志（自动轮转） |
| `data/memory/yu_agent.sqlite` | ★ **长期记忆落盘**（SQLite/FTS5），与一次性的 `runs/` 严格分开；sqlite 不入库 |

### `runs/`（每次运行产物，整目录 gitignore）

| 路径 | 作用 |
|---|---|
| `runs/.gitkeep` | 占位 |
| `runs/<时间戳>/step_NN.png` | 模型每一步实际看到的**带编号截图**（与喂给模型的一致，便于对照排障） |
| `runs/<时间戳>/trace.jsonl` | 每步一行 JSON：元素数、模型原始输出、解析动作、点击坐标（`action_mark`/`action_raw_xy`/`action_final_xy`） |
| `runs/perceive_latest.png` | `yu-agent perceive` 最近一次的标记图 |

### `tests/`

| 文件 | 作用 |
|---|---|
| `test_smoke.py` | 端到端冒烟：核心模块可导入 + 坐标还原 + 意图识别（离线，无需 API/屏幕） |
| `test_stop_logic.py` | 停止逻辑：验证"任务完成不重复点击""点停止立即生效"（打桩避开真实截屏/鼠标） |
| `unit/.gitkeep` | 规划中的纯函数单测目录（占位） |
| `integration/.gitkeep` | 规划中的跨模块测试目录（mock env + mock 模型，占位） |
| `replay/.gitkeep` | 规划中的轨迹离线重放目录（CI 不依赖屏幕与 key，占位） |

### `scripts/`

| 文件 | 作用 |
|---|---|
| `run.sh` | 启动脚本：自动定位项目根、选择 Python 环境（`.venv` → WorkBuddy 隔离环境 → 系统）、检查依赖、启动 GUI |

### 保留 `.gitkeep` 的运行时目录

git 不记录空目录，所以这四个目录各留一个 `.gitkeep` 让结构在仓库里可见：

| 目录 | 为什么必须存在 |
|---|---|
| `runs/` | 每次运行写截图与轨迹（整目录 gitignore） |
| `data/` | 用户配置 / 日志 / 记忆库的父目录 |
| `data/memory/` | 长期记忆 sqlite 落盘位置 |
| `eval/reports/` | 评测报告输出位置（`compare.py` 用） |

> 早期规划里的 `examples/`、`notebooks/`、`assets/`、`eval/{tasks,runners,metrics}/`、
> `tests/{unit,integration,replay}/` 从未填过内容，代码也零引用，已删除。

### `.workbuddy/`

| 路径 | 作用 |
|---|---|
| `.workbuddy/memory/YYYY-MM-DD.md` | 项目每日工作日志（append-only） |
| `.workbuddy/memory/MEMORY.md` | 项目长期约定与偏好 |

---

## 五、分层依赖规则（禁止反向 import）

```
cli / settings
      ↓
   agent/            ← 循环本体，可引用下面所有层
      ↓
grounding/  safety/  memory/  skills/  gui/
      ↓
   env/  ←──── actions/   (env 用 actions 的原语；actions 只在 TYPE_CHECKING 下引用 env)
      ↓
perception/  models/
      ↓
   common/
```

硬性约定：

- `actions/schema.py` **不 import 任何其他模块**——全局中心契约。
- `env/` 不得 import `agent/`、`grounding/` 或 `memory/`。
- `actions/executor.py` 需要 `GUIEnv` 类型时用 `if TYPE_CHECKING:`，避免与 `env/` 成环。
- `memory/` 只能依赖 `common/`（离线蒸馏 `distillation.py` 可额外依赖 `models/`），不得反向引用 `agent/`。
- `common/` 是叶子，不出现任务/动作/记忆相关业务概念。

---

## 六、状态速查

| 模块 | 状态 |
|---|---|
| `src/yu_agent/`（含 gui / skills） | ✅ 已实装，核心闭环可运行 |
| `config/`、`data/`、`runs/`、`scripts/`、顶层文件 | ✅ 已实装 |
| `tests/` | ✅ 已实装（11 个测试文件，97 passed） |
| `eval/` | ✅ 已实装（tasks.yaml + checks + runner + compare），缺基线数据 |
| `config/experiments/`、`config/models/` | ⏳ 尚未创建（用 `user.yaml` 覆盖即可，暂不需要） |
