# 说明：常用任务入口。demo 走 `yu_agent test`（mock 模型，零依赖零花费）。
PY ?= .venv/bin/python
UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR

.PHONY: help venv install dev test lint fmt doctor screenshot demo gui clean

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv:  ## 创建虚拟环境
	uv venv --python 3.12 .venv

install: venv  ## 安装运行时依赖
	uv pip install --python $(PY) -e .

dev: install  ## 安装开发依赖（pytest/ruff/mypy）
	uv pip install --python $(PY) -e ".[dev]"

macos: install  ## 额外安装 macOS 桌面控制依赖
	uv pip install --python $(PY) -e ".[macos]"

test:  ## 跑单元 + 回放测试（不需要屏幕和 API key）
	$(PY) -m pytest

test-gui:  ## 跑需要真实屏幕/权限的测试
	RUN_GUI_TESTS=1 $(PY) -m pytest -m gui

lint:  ## 静态检查
	$(PY) -m ruff check src tests

fmt:  ## 自动格式化 + 修 lint
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

doctor:  ## 自检：依赖、权限、配置、屏幕
	$(PY) -m yu_agent doctor

screenshot:  ## 抓一张当前屏幕到 runs/，用于确认权限
	$(PY) -m yu_agent screenshot

demo:  ## 用 mock 模型跑一遍完整循环（零依赖、零花费、不需要 API key）
	$(PY) -m yu_agent test "打开浏览器"

gui:  ## 启动图形化界面
	$(PY) -m yu_agent gui

clean:  ## 清理缓存
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
