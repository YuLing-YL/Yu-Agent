"""运行前清场（clean_desktop）与「清场模式」的回归测试。

背景：屏幕上的应用太多太杂时，SOM 编号表会被背景应用的按钮淹没，模型选错目标。
设计成两层：①尽力隐藏其它应用（⌘H）；②无论如何都把标记收窄到「当前应用 + 程序坞」。

实测限制（写在这里免得以后又以为是 bug）：从后台进程调
`NSRunningApplication.hide()` 会被 macOS 直接拒绝（返回 NO），
所以第 ① 层只能尽力而为，必须如实上报，不能假装成功。
"""
import sys

sys.path.insert(0, "src")

from yu_agent.actions import desktop as desk  # noqa: E402


class FakeApp:
    def __init__(self, name, pid, policy=0, hidden=False, can_hide=True):
        self._name = name
        self._pid = pid
        self._policy = policy
        self._hidden = hidden
        self._can_hide = can_hide
        self.hide_called = 0

    def activationPolicy(self):
        return self._policy

    def localizedName(self):
        return self._name

    def processIdentifier(self):
        return self._pid

    def isHidden(self):
        return self._hidden

    def hide(self):
        self.hide_called += 1
        if self._can_hide:
            self._hidden = True
        return self._can_hide


class FakeWorkspace:
    def __init__(self, apps):
        self._apps = apps

    def runningApplications(self):
        return list(self._apps)


def patch(apps, monkeypatch_cls=True):
    """把 AppKit.NSWorkspace 换成假的（clean_desktop 内部是 `from AppKit import ...`）。"""
    import AppKit

    orig = AppKit.NSWorkspace
    AppKit.NSWorkspace = type("NSWorkspace", (), {"sharedWorkspace": staticmethod(lambda: FakeWorkspace(apps))})
    return orig


def restore(orig):
    import AppKit

    AppKit.NSWorkspace = orig


def run_clean(apps, **kwargs):
    orig = patch(apps)
    try:
        return desk.clean_desktop(**kwargs)
    finally:
        restore(orig)


def test_hide_others_keep_whitelist():
    apps = [
        FakeApp("Finder", 100),
        FakeApp("Safari浏览器", 101),
        FakeApp("微信", 102),
        FakeApp("YuAgent(self)", 999),
        FakeApp("菜单栏小工具", 300, policy=1),  # accessory，不该动
    ]
    r = run_clean(apps, task="打开浏览器", exclude_pids=[999])
    assert set(r.hidden) == {"Safari浏览器", "微信"}, r.hidden
    assert "Finder" in r.kept, r.kept
    assert "YuAgent(self)" in r.kept, "自身进程必须保留"
    assert "菜单栏小工具" not in r.hidden + r.kept, "accessory 应用不该被处理"
    assert r.changed
    print(f"✓ 隐藏其它应用、保留白名单与自身（kept={r.kept}）")


def test_task_named_app_is_kept():
    apps = [FakeApp("Safari浏览器", 101), FakeApp("微信", 102)]
    r = run_clean(apps, task="在 Safari浏览器 里搜索天气")
    assert "Safari浏览器" in r.kept, f"任务点名的应用应保留：{r.kept}"
    assert r.hidden == ["微信"], r.hidden
    print("✓ 任务描述里点名的应用不会被隐藏")


def test_hide_failure_is_reported_honestly():
    apps = [FakeApp("Safari浏览器", 101, can_hide=False), FakeApp("微信", 102, can_hide=False)]
    r = run_clean(apps)
    assert not r.changed
    assert set(r.failed) == {"Safari浏览器", "微信"}, r.failed
    s = r.summary()
    assert "不允许隐藏" in s, f"隐藏失败必须如实说明：{s}"
    print(f"✓ 隐藏失败如实上报：{s}")


def test_already_hidden_not_touched():
    apps = [FakeApp("微信", 102, hidden=True)]
    r = run_clean(apps)
    assert r.already_hidden == ["微信"], r.already_hidden
    assert apps[0].hide_called == 0, "已经隐藏的应用不该重复操作"
    print("✓ 已隐藏的应用不重复处理")


def test_missing_appkit_reports_error():
    import AppKit

    orig = AppKit.NSWorkspace
    try:

        class Boom:
            @staticmethod
            def sharedWorkspace():
                raise RuntimeError("模拟：读取失败")

        AppKit.NSWorkspace = Boom
        r = desk.clean_desktop()
    finally:
        AppKit.NSWorkspace = orig
    assert r.error, "清场失败要带 error，不能静默"
    assert "失败" in r.summary()
    print("✓ 清场异常时返回 error，不影响主流程")


def test_loop_reset_mode_narrows_to_frontmost():
    """清场模式下 SOM 只标「当前最前应用 + 程序坞」，且首步有说明。"""
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import VLMResponse
    from yu_agent.skills import build_default_registry

    calls = []
    orig_clean = loopmod.clean_desktop
    loopmod.clean_desktop = lambda **kw: (
        calls.append(kw) or desk.CleanReport(hidden=["微信"], kept=["Finder"], windows_before=6, windows_after=2)
    )

    prompts = []

    class B:
        name = "fake"

        def chat(self, req):
            prompts.append(req.prompt)
            return VLMResponse(text='{"type": "done", "reason": "结束"}')

    class M(MemoryManager):
        pass

    try:
        lp = AgentLoop(
            B(), MemoryManager(store=MemoryStore(db_path=":memory:")),
            build_default_registry(),
            config={"max_steps": 3, "verify_after_action": False, "reset_desktop": True,
                    "som_max_apps": 6},
            dry_run=False,
        )
        lp._execute = lambda action: "跳过"          # 不真的动屏幕
        st = lp.run("打开浏览器搜索天气")
    finally:
        loopmod.clean_desktop = orig_clean

    assert lp.som_max_apps == 1, f"清场模式应只标当前应用，实际 {lp.som_max_apps}"
    assert calls and calls[0]["task"] == "打开浏览器搜索天气"
    assert "清场模式" in prompts[0], "首步提示词必须说明清场模式"
    assert "微信" in prompts[0] or "隐藏" in prompts[0]
    assert st.step == 1
    print(f"✓ 清场模式生效：som_max_apps={lp.som_max_apps}，首步有说明")


if __name__ == "__main__":
    test_hide_others_keep_whitelist()
    test_task_named_app_is_kept()
    test_hide_failure_is_reported_honestly()
    test_already_hidden_not_touched()
    test_missing_appkit_reports_error()
    test_loop_reset_mode_narrows_to_frontmost()
    print("\n=== 清场测试全部通过 ===")
