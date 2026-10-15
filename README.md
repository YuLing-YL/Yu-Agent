# Yu-Agent

基于 VLM 的 GUI Agent（Python）。核心是一条闭环：

```
observe(截屏) → recall(取记忆) → think(规划/拆分任务) → ground(定位坐标)
   → act(执行) → verify(校验) → reflect(反思) → distill(任务结束结晶记忆)
```

> 当前状态：**核心闭环已实现，可运行**。提供图形化界面（PySide6）+ 命令行两种入口。
> 支持 OpenAI 兼容接口（OpenAI / Gemini / 通义千问 / DeepSeek / Kimi / 本地 vLLM / Ollama），
> 用户可在「设置」页自行填写 base_url / api_key / 模型名。

## 核心特性

- **图形化界面**：需求输入框 + 模型快速切换下拉框 + 实时屏幕预览 + 执行日志 + 设置页（API 配置）
- **悬浮控制条**：任务运行时置顶悬浮（跨桌面/全屏可见），切到别的应用也能一键停止
- **智能意图识别**：AI 先判断输入是否需要操作屏幕，并生成回复文字；纯闲聊/打招呼只回复不调 agent
- **任务拆分**：遇到复杂任务，模型会先拆成多个子任务再逐个执行（子任务完成自动推进）
- **自动停止**：多层兜底——连续重复动作 / 同一目标累计点击 / 点击总预算 / 屏幕长时间无变化 / 步数上限，
  任一触发即停下，避免无限重复点击；点「停止」后当前动作也会被丢弃，不会再多点一下
- **分层记忆**：短时工作记忆 + 长期记忆（情节/语义/程序/用户画像），SQLite 持久化
- **技能系统**：预写可复用的 Python 函数，模型决策时可选择调用
- **多模型记忆**：记住用户添加过的模型，下拉框快速切换
- **SOM 标记定位**：在截图上给可交互元素画编号框（基于 macOS 辅助功能 API），
  模型输出编号而非估算像素坐标，点击精确落在元素中心；模型若直接给坐标，
  会自动**吸附**到附近的标记元素（修正 VLM 常见的几十像素偏差）。
  覆盖**屏幕上所有可见应用 + 完整程序坞**，被前面窗口遮住的元素与
  非前台应用的菜单栏重影会被自动剔除；编号表按应用分组，模型不会点错窗口
- **可观测 / 可排障**：日志落盘 `data/logs/yu_agent.log`；每次运行在 `runs/<时间戳>/`
  保存「模型看到的带编号截图 + 每一步的原始输出/坐标」；`yu-agent perceive`
  可在不调模型的情况下直接列出识别到的可点元素
- **鲁棒性**：提示词注入防护、广告噪声过滤、动作校验与重试、危险动作拦截、异常容错

## 快速开始

```bash
# 1. 安装依赖（含 GUI）
pip install -e ".[macos,gui]"

# 2. 启动图形界面
python -m yu_agent gui
# 或
./scripts/run.sh

# 3. 命令行执行任务（需先配置 API）
python -m yu_agent run "打开浏览器搜索天气"

# 4. 离线测试闭环（mock 后端，无需 API 和屏幕）
python -m yu_agent test "打开浏览器搜索天气"

# 5. 环境自检
python -m yu_agent doctor

# 6. 截屏测试
python -m yu_agent screenshot -o screenshot.png
```

首次使用需在「系统设置 → 隐私与安全性」授予 **屏幕录制** 与 **辅助功能** 权限。

模型 API 配置两种方式：
1. **图形界面**：点「设置」按钮，填写 base_url / api_key / 模型名（保存到 `data/config/user.yaml`）
2. **环境变量**：`.env` 里填 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`

---

## 目录结构

```
Yu-Agent/
├── pyproject.toml            # 打包 + 依赖 + pytest/ruff/mypy 配置（核心依赖仅 pydantic/pyyaml/pillow）
├── Makefile                  # 任务入口：install / dev / test / lint / doctor / demo
├── .env.example              # 密钥模板（真实 .env 不入库）
├── .gitignore                # 已忽略 runs/、.env、__pycache__、.DS_Store 等
│
├── config/                   # 配置（非代码）
│   ├── default.yaml          #   全局默认：agent/loop/perception/grounding/safety/models/paths
│   ├── experiments/          #   实验覆盖，深度合并到 default（用于 A/B 对比 prompt、模型、SoM 开关）
│   └── models/               #   模型 profile：provider/model/base_url/成本相关参数
│
├── src/yu_agent/             # ★ 唯一可安装包（src-layout，import yu_agent）
│   ├── __init__.py
│   ├── __main__.py           #   python -m yu_agent 入口
│   ├── cli.py                #   子命令：doctor / run / replay / screenshot
│   ├── settings.py           #   配置加载：default.yaml + experiment 覆盖 + 环境变量
│   │
│   ├── common/               # 无业务含义的通用工具（最底层，不依赖任何其他模块）
│   │   ├── logging.py        #   日志初始化
│   │   ├── paths.py          #   项目路径、runs/<timestamp> 目录分配
│   │   ├── image.py          #   图像读写、缩放、base64、差分、感知哈希
│   │   ├── tracing.py        #   逐步写 steps.jsonl（截图 + 动作 + 结果 + token + 耗时）
│   │   └── errors.py         #   异常体系（ConfigError/ModelError/EnvError/GroundingError/SafetyViolation）
│   │
│   ├── models/               # VLM 后端（只负责「调模型」，不关心 GUI）
│   │   ├── base.py           #   VLMBackend Protocol + VLMRequest/VLMResponse/TokenUsage
│   │   ├── registry.py       #   provider 名 → 后端实现
│   │   ├── router.py         #   角色(planner/grounder/reflector) → 后端，支持大小模型分工
│   │   ├── cache.py          #   响应缓存（开发期避免重复计费，回放测试靠它）
│   │   ├── mock.py           #   脚本化假后端（离线测试必备）
│   │   ├── openai_vlm.py     #   OpenAI 及兼容端点
│   │   ├── anthropic_vlm.py  #   Claude
│   │   ├── qwen_vl.py        #   通义千问 VL（DashScope 兼容模式）
│   │   └── local_vllm.py     #   本地 vLLM / Ollama
│   │
│   ├── perception/           # 「看」：产出 Observation
│   │   ├── observation.py    #   Observation 数据结构（图像 + 元素树 + 窗口 + 屏幕几何）
│   │   ├── screenshot.py     #   截屏实现（macOS screencapture / mss / PIL ImageGrab）
│   │   ├── ui_tree.py        #   UIElement 树、a11y/DOM/OCR 统一表示、检索、渲染成 prompt 文本
│   │   ├── a11y.py           #   辅助功能树读取（macOS AXUIElement）
│   │   ├── ocr.py            #   OCR 兜底（无 a11y 时从像素取文字）
│   │   └── som.py            #   Set-of-Mark：在截图上画编号框
│   │
│   ├── env/                  # 「在哪操作」：平台驱动，实现 GUIEnv
│   │   ├── base.py           #   GUIEnv Protocol（start/observe/screen_geometry/primitives/close）
│   │   ├── macos.py          #   macOS 主实现（本项目首要目标平台）
│   │   ├── windows.py        #   Windows（未验证）
│   │   ├── linux.py          #   Linux（未验证）
│   │   ├── browser_cdp.py    #   浏览器：走 CDP/DOM，比点像素可靠得多
│   │   └── mock.py           #   假环境：合成截图 + 记录动作（离线回放测试用）
│   │
│   ├── actions/              # 「做什么」：动作契约与执行
│   │   ├── schema.py         #   ★ 动作空间 pydantic 契约（click/type/key/scroll/drag/wait/
│   │   │                     #     open_app/done/ask_user...）——整个项目的中心接口
│   │   ├── executor.py       #   动作 → 原语分发，含重试、超时、安全闸门
│   │   └── primitives/       #   原子操作（平台无关的意图层）
│   │       ├── base.py       #     PrimitiveDriver Protocol（click_at/type_text/key_press/scroll/drag）
│   │       └── pyautogui_driver.py  # 跨平台实现
│   │
│   ├── grounding/            # 「落到哪一点」：模型输出 → 屏幕坐标（最容易失败的一环）
│   │   ├── parser.py         #   从模型文本里抽出 JSON 动作并校验
│   │   ├── coords.py         #   归一化↔像素换算、Retina/DPI 缩放
│   │   ├── matcher.py        #   目标描述 → a11y 元素 / OCR 文本匹配
│   │   ├── resolver.py       #   综合定位：显式坐标 > 元素 id > 文本匹配 > SoM 视觉定位
│   │   └── verify.py         #   动作后校验：屏幕是否变化、期望文字是否出现
│   │
│   ├── safety/               # 安全闸门（不可逆操作前的拦截与审批）
│   │   ├── policy.py         #   规则：危险动作类型、危险 shell、删除/支付/发送类 UI 文案
│   │   └── approval.py       #   审批接口：自动放行 / 控制台询问 / 全拒（测试用）
│   │
│   ├── prompts/              # 版本化提示词（纯文本，改 prompt 不用碰 Python）
│   │   ├── loader.py         #   加载 + {{var}} 渲染
│   │   ├── system.md         #   系统角色与输出格式约定
│   │   ├── planner.md        #   规划：给定任务+历史+元素列表 → 下一个动作
│   │   ├── grounder.md       #   定位：把目标描述转成坐标
│   │   └── reflector.md      #   反思：卡住/失败时的自我批评与纠偏
│   │
│   ├── memory/               # ★ 记忆：独立包，被 planner / reflect / 离线蒸馏三方共用
│   │   ├── base.py           #   MemoryRecord + Memory 接口（write/search/forget/feedback）
│   │   ├── working.py        #   L1 任务内：轨迹窗口、scratchpad、上下文预算裁剪
│   │   ├── procedural.py     #   L2 技能模板（参数化，复用前必须重新校验）
│   │   ├── semantic.py       #   L3 环境知识（app/元素/快捷键/登录态，带失效字段）
│   │   ├── episodic.py       #   L4 过往轨迹存取与检索（few-shot 来源）
│   │   ├── profile.py        #   用户偏好与安全偏好
│   │   ├── store.py          #   后端：sqlite(FTS5) / jsonl / in-memory（测试用）
│   │   ├── retrieval.py      #   检索与重排：关键词/BM25 + app 过滤（向量可选，暂不做）
│   │   └── distillation.py   #   离线：任务轨迹 → 技能 / 教训（成功与失败分开结晶）
│   │
│   └── agent/                # ★ 循环本体
│       ├── loop.py           #   AgentLoop.run(task) -> Trajectory
│       ├── planner.py        #   组装 prompt（历史 + 元素 + 截图 + 记忆提示）并调用模型
│       ├── state.py          #   Task / StepRecord / Trajectory / RunState
│       └── reflect.py        #   卡死检测（重复动作/无变化）与纠偏
│
├── eval/                     # 评测（和 tests 是两回事：这里量的是「成功率」）
│   ├── tasks/                #   任务用例（yaml）
│   ├── runners/              #   批量跑用例、产出报告
│   ├── metrics/              #   任务成功率、单步成功率、定位准确率、成本、延迟
│   └── reports/              #   报告产物（不入库）
│
├── tests/
│   ├── unit/                 #   纯函数单测：坐标换算、动作 schema 校验、策略规则、解析器
│   ├── integration/          #   跨模块：mock env + mock 模型跑完整循环
│   └── replay/               #   录制轨迹离线重放（CI 不依赖屏幕与 API key）
│
├── scripts/                  # 开发脚本：抓一张屏、单测一次模型调用、录轨迹
├── examples/                 # 可运行示例（原 demo/ 改名）
├── notebooks/                # 探索性分析
├── assets/                   # 图标模板、OCR 模型等静态资源
├── data/                     # 任务用例与小样本数据（大文件不入库）
│   └── memory/               #   ★ 长期记忆落盘（*.sqlite 不入库；与一次性的 runs/ 严格分开）
└── runs/                     # ★ 运行产物，整目录 gitignore
    └── <timestamp>/          #   steps.jsonl + screenshots/ + video/ + agent.log
```

> 上面所有模块文件目前都是 **0 字节占位符**（只有目录名和文件名，没有任何代码），
> 目的是让结构在编辑器和 git 里可见，逐个填充时不会放错位置。

## 记忆分层（四层，不是一层）

| 层 | 文件 | 内容 | 关键约束 |
|---|---|---|---|
| L1 工作记忆 | `memory/working.py` | 当前任务的最近 N 步 (截图/动作/结果) + scratchpad 笔记 | **上下文预算**：只留最近 1–2 张图，更早的步骤降级成纯文本摘要 |
| L2 程序性记忆 | `memory/procedural.py` | 可复用技能模板，如"打开深色模式"= 4 个动作 | 任务成功后由 `distillation.py` 结晶；**复用前必须重新定位校验** |
| L3 语义记忆 | `memory/semantic.py` | 某 App 的界面结构、按钮位置、快捷键、登录态、踩过的坑 | 坐标会过期，位置必须带 `(屏幕分辨率, app 版本, 时间戳)`，且**只能当候选提示**交给 `grounding/`，不能当真实坐标用 |
| L4 情景记忆 | `memory/episodic.py` | 过去任务的完整轨迹，检索出来当 few-shot | 只存成功轨迹作正样本；失败轨迹进负样本库避免重犯 |
| 用户画像 | `memory/profile.py` | 用户偏好、禁用操作、审批白名单 | 供 `safety/` 决策参考 |

三条工程约定：

1. **写入时机**：在线循环里不让模型每步写记忆（噪声大且贵）。任务结束后由 `distillation.py` 统一蒸馏，离线可批量重跑。
2. **检索方式**：`sqlite + FTS5` 关键词检索 + `(app, 窗口标题)` 过滤。GUI 领域查询短、范围窄，通常优于向量检索，且**零新增依赖**；向量留作后续可选增强。
3. **必须能证明有用**：`eval/tasks/` 带 `use_memory` 开关，同一套用例跑 on/off 对比成功率、平均步数、成本。否则无从判断记忆是在帮忙还是在引入幻觉。

## 分层依赖规则

只允许**自上而下**引用，禁止反向 import（否则必然出现循环依赖）：

```
cli / settings
      ↓
   agent/            ← 循环本体，可以引用下面所有层
      ↓
 grounding/  safety/  memory/
      ↓
   env/  ←──── actions/  (env 用 actions 的原语；actions 只在 TYPE_CHECKING 下引用 env)
      ↓
perception/  models/
      ↓
   common/
```

硬性约定：

- `actions/schema.py` **不 import 任何其他模块** —— 它是全局中心契约。
- `env/` 不得 import `agent/`、`grounding/` 或 `memory/`。
- `actions/executor.py` 需要 `GUIEnv` 类型时用 `if TYPE_CHECKING:`，避免与 `env/` 形成环。
- `memory/` 只能依赖 `common/`（离线蒸馏 `distillation.py` 可额外依赖 `models/`），**不得反向引用 `agent/`**。
- `common/` 是叶子，里面不要出现任务/动作/记忆相关的业务概念。

## 提示词里的记忆槽位

`prompts/planner.md` 需要预留这些占位（由 `agent/planner.py` 从 `memory/` 取填）：

```
{{memory_hints}}     # L3 环境知识：这个 App 的相关经验
{{skills}}           # L2 命中的技能模板（可复用步骤）
{{similar_tasks}}    # L4 相似历史任务的轨迹摘要
{{notes}}            # L1 当前任务 scratchpad
```

## 快速开始

```bash
make dev          # 建 venv 并安装 -e ".[dev]"（当前环境已有 .venv，只装了 pydantic/pyyaml/pillow/pytest）
make macos        # 需要真实控制桌面时，再加装 pyautogui + pyobjc
cp .env.example .env    # 填 API key
make test         # 跑单测/回放（不需要屏幕与密钥）
make doctor       # 需要 src/yu_agent/cli.py 写完后才可用
```

## 关于目标平台

首要目标是 **macOS 桌面 App**，所以 `env/macos.py` 是主线实现：

- 截屏走系统自带 `screencapture`，零额外依赖；
- 首次使用需在「系统设置 → 隐私与安全性」授予 **屏幕录制** 与 **辅助功能** 权限；
- 坐标一律使用**逻辑点（point）**，`grounding/coords.py` 负责 Retina 缩放换算。

`env/browser_cdp.py` 为网页类任务预留：能走 DOM 就不要点像素。
