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

- **调试面板（GUI）**：左栏直接显示**模型实际收到的那张标注图**，右栏并排给出编号表、
  模型原始输出、执行日志；支持**单步执行**（跑一步、看清楚、再放行）、随时停止、
  一键打开本次 runs 目录。样式交给系统原生控件，不做好看的皮
- **悬浮控制条**：任务运行时置顶悬浮（跨桌面/全屏可见），切到别的应用也能一键停止
- **智能意图识别**：AI 先判断输入是否需要操作屏幕，并生成回复文字；纯闲聊/打招呼只回复不调 agent
- **任务拆分**：遇到复杂任务，模型会先拆成多个子任务再逐个执行（子任务完成自动推进）。
  拆分**只在第一步发生一次**，之后模型必须执行动作——重复拆分会被忽略并计入异常计数
- **只标当前窗口**：默认只标记「最前面的应用 + 程序坞」，背景窗口的按钮不进编号表
  ——纯粹少标一些元素，**不会隐藏、移动或关闭你的任何窗口**；GUI 可关，CLI 用 `--all-apps`
- **自动停止**：多层兜底——连续重复动作 / 同一目标累计点击 / 点击总预算 / 屏幕长时间无变化 /
  反复拆分任务 / 步数上限，任一触发即停下，避免无限重复点击；
  点「停止」后当前动作也会被丢弃，不会再多点一下。
  判定「已完成」还是「没做成」看屏幕有没有反应——重复操作但屏幕毫无变化会报 ❌，不谎报成功
- **文字输入**：click 聚焦 → `type` 输入 → `key`+`enter` 提交的三步流程；
  中文（含 emoji、中英混排）走剪贴板粘贴，支持 `cmd+l` / `ctrl+c` 等按键别名。
  点击输入框后若模型迟迟不输入，会定向提示它改用 `type`
- **提示词语言可切换**：默认英文（token 更省、指令遵循更稳），一份配置切回中文；
  运行期的动态提示也跟随语言
- **完整动作集**：左键/右键/双击/中键/三击、逐字输入、按键组合、滚动、移动、绝对坐标拖拽、
  等待、技能调用
- **分层记忆**：短时工作记忆 + 长期记忆（情节/语义/程序/用户画像），SQLite 持久化
- **技能系统**：预写可复用的 Python 函数，模型决策时可选择调用。
  `open_app` 支持中英文应用名（`文本编辑→TextEdit`）并回报真实前台结果；
  `show_dock` 会把鼠标移到屏幕边缘唤醒自动隐藏的程序坞，
  所以 **Dock 隐藏时也能打开应用**（两条路都写进了提示词）
- **多模型记忆**：记住用户添加过的模型，下拉框快速切换
- **SOM 标记定位**：在截图上给可交互元素画编号框（基于 macOS 辅助功能 API），
  模型输出编号而非估算像素坐标，点击精确落在元素中心；模型若直接给坐标，
  会自动**吸附**到附近的标记元素（修正 VLM 常见的几十像素偏差）。
  三种模式可切换：`marks` 编号标注（默认）/ `coords` 纯坐标 / `off` 不标注
- **编号跨帧稳定**：同一个元素在整个任务里始终是同一个编号（元素增减、列表滚动也不漂移），
  模型记住的编号不会指到别的元素上
- **编号语义校验**：模型说「点 yuling」却给了「iCloud云盘」的编号时，
  按 reason 里的名字自动纠正到正确编号，纠正记录会进日志和界面
- **标注降噪**：元素命名走通用回退链（属性 → 子元素文字），列表行/单元格/网页表格都能
  拿到真实名字；「行→单元格→文字」套娃合并、输入框套娃只留外层、菜单栏项默认跳过、
  无标签图标限流、点不中的细小元素剔除、禁用元素会标注。
  编号标签按字号自适应并自动避让，密集列表里也不会跨行压住邻居
- **列表项 vs 输入框**：Finder/资源管理器/Electron 列表里的文件名在 AX 里是「文本字段」，
  但不会被当成输入框（否则会逼模型往文件列表里打字）
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
│   ├── tasks.yaml            #   任务用例定义（加用例只改这里）
│   ├── checks.py             #   判据：硬判据（前台应用/窗口标题/文件存在）+ 软判据
│   ├── runner.py             #   批量跑用例 → 判定 → 汇总
│   ├── compare.py            #   两次结果的 A/B 对比
│   └── reports/              #   报告产物（不入库）
│
├── tests/                    # 单元 + 冒烟测试（平铺，97 passed）
│   ├── test_smoke.py         #   离线闭环：mock 模型跑完整循环
│   ├── test_stop_logic.py    #   停止逻辑（重复动作/无变化/步数上限）
│   ├── test_marks_quality.py / test_content_marks.py   # 标注质量与框校准
│   ├── test_a11y_generic.py  #   a11y 通用命名/去重/降噪
│   ├── test_input_module.py / test_input_replace.py    # 输入与旧内容覆盖
│   ├── test_memory.py        #   短期/长期记忆
│   └── test_open_app.py / test_responder.py / test_step_mode.py
│
├── scripts/run.sh            # 启动脚本
├── data/                     # 运行时数据（目录入库，数据不入库）
│   ├── config/user.yaml      #   API key / base_url（gitignore）
│   ├── logs/                 #   yu_agent.log
│   └── memory/               #   ★ 长期记忆落盘（*.sqlite 不入库；与 runs/ 严格分开）
└── runs/                     # ★ 运行产物，整目录 gitignore
    └── <timestamp>/          #   trace.jsonl + step_XX.png + 标注图
```

> 目录只保留真正在用的：早期规划里的 `examples/`、`notebooks/`、`assets/`、
> `eval/{tasks,runners,metrics}/`、`tests/{unit,integration,replay}/` 一直是空占位，
> 已全部删除。

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
3. **必须能证明有用**：目标是给 `eval/tasks.yaml` 加 `use_memory` 开关，同一套用例跑 on/off 对比成功率、平均步数、成本——目前还没接（`config/default.yaml` 里只留了 flag 名）。在没测出对比之前，不能假定记忆一定在帮忙。

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
