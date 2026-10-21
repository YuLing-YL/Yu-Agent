# Yu-Agent 评测集（core-v1）

10 个固定任务，每个跑 N 次，用**外部状态**判定成功，输出成功率 / 平均步数 / 自动停止次数。

存在的意义只有一个：把「感觉好像好一点了」变成可比较的数字。改提示词、改标注方式、
换模型之后，跑一遍就知道是真变好还是碰巧。

---

## 快速开始

```bash
# 1. 先看有哪些任务
python eval/runner.py --list

# 2. 先验证评测流程本身（mock 后端，不操作真实界面，2 分钟）
python eval/runner.py --self-test -n 1 --no-screenshot --yes

# 3. 真机冒烟：每个任务跑 1 次
python eval/runner.py -n 1

# 4. 正式评测：每个任务 3 次（约 10~20 分钟，取决于模型速度）
python eval/runner.py
```

跑完会打印汇总表，并生成：

```
eval/reports/<时间戳>/
├── report.md        # 给人看的报告
├── results.json     # 原始数据，给 compare 用
└── shots/           # 每次运行结束的截图（人工复核判据用）
```

## 怎么做 A/B 对比

```bash
# 改配置前
python eval/runner.py --tag before

# 改配置后
python eval/runner.py --tag after

# 对比最近两次
python eval/compare.py --latest
```

也可以临时覆盖标注模式，直接对比同一个任务在两种模式下的表现：

```bash
python eval/runner.py --tag marks  --som-mode marks
python eval/runner.py --tag coords --som-mode coords
python eval/compare.py --latest
```

`compare.py` 会输出总览差异 + 逐个任务的「变好 / 变差」，变差的会点名，通常一眼就能
指出是哪项能力被改坏了。

---

## 判据：为什么不用 `state.success`

Agent 主循环里的 `success` 有一部分来自兜底逻辑（「重复动作 + 屏幕无变化 → 判定已完成」），
历史上真的出现过「点了三次搜索框、一个字没输入，界面显示 ✅ 完成」。
评测如果采信它，等于让被测对象自己改考卷。

所以每个任务的判据都从**系统状态**读：

| 判据 | 强度 | 读什么 | 用在哪 |
| --- | --- | --- | --- |
| `frontmost_app` | 硬 | 前台应用名 | 「打开 X」类 |
| `window_title` | 硬 | 前台应用窗口标题 | 导航、搜索结果 |
| `file_exists` | 硬 | 文件系统 | 新建文件夹（副作用真的发生了） |
| `element_exists` | 软 | 可交互元素的名字 | 界面内容 |
| `screen_text` | 软 | 元素名字（同 `element_exists`） | 终端输出、输入结果 |
| `manual` | — | 不判定 | 只能人看截图 |

**软判据的行为**：自动匹配到 → `pass`；匹配不到 → `待人工`（不是 `fail`）。
因为很多应用（尤其 Electron / Canvas）辅助功能读不到文字，判它失败是冤枉。
要严格时给任务加 `strict: true`，软判据就退化成硬判据。

### 前置条件（`pre_check`）

「打开 Safari」这个任务，如果 Safari 本来就在前台，跑完必然 pass —— 这是假成功。

所以每个任务都有 `pre_check`：起点如果已经满足终点，该次运行记为 **`无效起点`**，
跳过不跑，且不计入成功率分母。跑的时候你会在终端看到 `⊘ invalid`。

同理，`finder_new_folder` 如果 `eval_ok` 文件夹已经存在，也会是 `无效起点` ——
删掉它再跑。

---

## 10 个任务覆盖什么

| id | 类别 | 考的能力 |
| --- | --- | --- |
| `finder_open` | 应用启动 | 从程序坞 / 聚焦搜索启动应用 |
| `safari_open` | 应用启动 | 同上（不同应用，验证泛化） |
| `finder_enter_dir` | 文件导航 | 地址栏 / 侧边栏 / 键盘路径输入 |
| `finder_multi_step` | 多步串联 | 打开 → 进入目录 → 双击文件，状态依赖 |
| `finder_new_folder` | 文件操作 | `cmd+shift+N` 快捷键 + 输入命名 |
| `safari_search_en` | 搜索-英文 | 聚焦地址栏（`cmd+l`）+ ASCII 输入 + 回车 |
| `safari_search_zh` | 搜索-中文 | **中文输入**（走剪贴板粘贴的那条路径） |
| `textedit_type` | 文本输入 | 新建文稿 + 长文本输入 |
| `settings_display` | 系统设置 | 多级导航，面对列表型界面 |
| `terminal_echo` | 终端命令 | 键盘输入 + 执行 |

设计时刻意做了三组对照：**启动类 vs 导航类**、**英文 vs 中文输入**、
**单步 vs 多步**。这样某一类掉分时能直接定位到能力维度，而不是「整体不太行」。

---

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `-t, --tasks` | 按 id / 类别筛选，逗号分隔，子串匹配（`-t 搜索` 跑三个搜索任务） |
| `-n, --runs` | 每个任务跑几次（默认 3） |
| `--tag` | 打标签，进报告文件名，A/B 对比用 |
| `--timeout` | 单次运行硬超时，默认 180s。超时会 `stop()` 并等它退 |
| `--cooldown` | 两次运行之间等待秒数（默认 3），给界面稳定时间 |
| `--som-mode` | 临时覆盖标注模式（`marks` / `coords` / `off`），做 A/B 用 |
| `--reset-apps` | 每次运行前退出 Safari / 文本编辑 / 系统设置，让「打开 X」类任务起点干净 |
| `--no-screenshot` | 跑完不存复核截图（快一点） |
| `--self-test` | mock 后端 + dry-run，只验证评测流程 |
| `--verbose` | 保留 agent 的实时日志（默认被静音，否则会把表格冲乱） |

---

## 已知限制（重要，别被数字骗）

1. **模型自己的日志被静音了**：默认只写 `data/logs/yu_agent.log`，控制台留给评测表格。
   想看实时日志加 `--verbose`。
2. **启动类任务的前后依赖**：上一轮把 Safari 留在前台，下一轮 `safari_open` 会记 `无效起点`。
   要么加 `--reset-apps`，要么接受这个 invalid（它不算失败，但也不贡献样本）。
3. **`textedit_type` 和 `terminal_echo` 是软判据**：自动匹配不到会变成 `待人工`，
   需要你打开报告里的截图看一眼。这是刻意的，避免 a11y 读不到文字就判失败。
4. **样本量**：3 次只能看大方向。成功率差 10pt 以内基本是噪声，改到 5 次以上再下结论。
5. **屏幕锁定/休眠时截屏会被系统阻塞**（实测能到 30s），评测会变慢。跑之前确认屏幕醒着。
6. **会真的操作鼠标键盘**。跑的时候别动鼠标；要中止按 `Ctrl+C`，已完成的结果会保存。

---

## 怎么加任务

在 `eval/tasks.yaml` 的 `tasks:` 后面追加即可，不需要改任何代码：

```yaml
  - id: notes_new_note
    category: 应用操作
    task: "打开备忘录，新建一条内容为 yu_eval 的备忘录"
    check:
      kind: window_title
      expect: ["yu_eval"]
    pre_check:            # 可选：起点不干净就跳过，避免假成功
      kind: not_frontmost_app
      expect: ["Notes", "备忘录"]
```

加完先 `--list` 看判据有没有解析对，再 `-t 新id -n 1` 单跑一次验证判据会不会误判。
**判据要先能判对，再谈成功率。**
