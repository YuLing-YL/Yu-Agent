# Yu-Agent 命令速查表

调试和日常使用时快速查阅的命令参考。所有命令都在项目根目录下执行。

## 前置

```bash
# 使用项目虚拟环境（推荐）
.venv/bin/python -m yu_agent <子命令>
# 或直接启动脚本
./scripts/run_gui.sh
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
./scripts/run_gui.sh
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
已修复（坐标统一逻辑像素 + 比例还原）。若仍偏移，检查是否多显示器，或屏幕缩放设置。

### 截屏黑屏/报权限错误
macOS 需在「系统设置 → 隐私与安全性 → 屏幕录制」和「辅助功能」中授权。

### 模型连接失败
在 GUI「设置」页点「测试连接」排查；确认 base_url 正确（需带 `/v1` 结尾）。

## API 配置位置

- 配置文件：`data/config/user.yaml`（GUI 设置页保存到这里）
- 环境变量：`.env` 里 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
