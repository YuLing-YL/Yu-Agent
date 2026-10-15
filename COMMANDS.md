# Yu-Agent 命令速查表

调试和日常使用时快速查阅的命令参考。所有命令都在项目根目录下执行。

## 前置

```bash
# 使用项目虚拟环境（推荐）
.venv/bin/python -m yu_agent <子命令>
# 或直接启动脚本
./scripts/run.sh
```

## 命令一览

| 命令 | 用途 | 是否需要 API Key |
|------|------|-----------------|
| `python -m yu_agent gui` | 启动图形界面 | 需要 |
| `python -m yu_agent run "任务"` | 命令行执行一个 GUI 任务 | 需要 |
| `python -m yu_agent test "任务"` | 离线测试完整闭环（mock 后端） | 不需要 |
| `python -m yu_agent screenshot` | 截屏保存到文件 | 不需要 |
| `python -m yu_agent doctor` | 检查环境依赖 | 不需要 |

## 详细说明

### 1. 启动图形界面

```bash
python -m yu_agent gui
# 或
./scripts/run.sh
```

### 2. 命令行执行任务

```bash
# 正常执行（需先配置 API）
python -m yu_agent run "打开浏览器搜索天气"

# 只规划不执行（安全调试）
python -m yu_agent run "打开浏览器搜索天气" --dry-run
```

### 3. 离线测试闭环

不需要 API Key，不需要屏幕权限，用内置 mock 后端 + 内存记忆跑一遍完整闭环，
用于验证「任务拆分 → 动作 → 结束」逻辑是否正常。

```bash
python -m yu_agent test "打开浏览器搜索天气"
# 不传任务用默认测试任务
python -m yu_agent test
```

### 4. 截屏测试

验证屏幕录制权限是否正常。

```bash
python -m yu_agent screenshot
# 指定输出路径
python -m yu_agent screenshot -o /tmp/screen.png
```

### 5. 环境自检

检查依赖是否齐全。

```bash
python -m yu_agent doctor
```

## 安装依赖

```bash
# 完整安装（macOS + GUI）
pip install -e ".[macos,gui]"

# 用 uv（项目默认）
uv pip install -e ".[macos,gui]" --python .venv/bin/python
```

## 常见问题排查

### 报 `No module named 'yu_agent'`
包未安装，执行 `uv pip install -e ".[gui,local]" --python .venv/bin/python`。

### 报 `No module named 'pydantic'` 等
依赖未装全，执行完整安装命令（见上）。

### 点击位置偏移
已实现 SOM 标记法（截图给可交互元素画编号框，模型输出编号 → 查表定位）。
若仍偏移，检查是否多显示器；或在 `config/default.yaml` 里关掉 `perception.use_som` 对比。

### 任务重复点击 / 完成后不停
已有三重自动停止：重复动作阈值、屏幕无变化阈值、最大步数。可在 `config/default.yaml` 调整：
- `agent.max_repeated_actions`（默认 3）：同一动作连续重复多少次判定卡住
- `agent.max_no_change_steps`（默认 4）：屏幕连续多少次无变化判定卡住
- `agent.max_steps`（默认 30）

### 任务运行时切到别的应用，找不到停止按钮
任务开始后会出现**置顶悬浮控制条**（默认屏幕右上角），跨桌面/全屏应用始终可见：
- 「■ 停止」立即停止任务
- 「主窗口」唤回主界面
- 可按住拖动到任意位置
- 任务成功结束后 10 秒自动隐藏；失败/被停止则保留，点「关闭」收起

### 截屏黑屏/报权限错误
macOS 需在「系统设置 → 隐私与安全性 → 屏幕录制」和「辅助功能」中授权。
辅助功能权限同时用于 SOM 元素枚举（没授权会拿不到可点击元素）。

### 模型连接失败
在 GUI「设置」页点「测试连接」排查；确认 base_url 正确（需带 `/v1` 结尾）。

### 修改参数后如何生效
改完 `config/default.yaml` 需重启程序；GUI 设置页（模型/API）保存后即时生效。

## API 配置位置

- 配置文件：`data/config/user.yaml`（GUI 设置页保存到这里）
- 环境变量：`.env` 里 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
