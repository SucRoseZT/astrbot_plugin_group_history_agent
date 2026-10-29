# -*- coding: utf-8 -*-
"""v2.5.0 修改点单测：软申请识别 / noflag 收录 / owner_only 开关 / soft 拦截。
复用 test_core_stub 的桩思路，但聚焦申请链路，自建最小桩。"""
import asyncio
import sys
import time
import types

# ---------- 桩模块（最小集） ----------
astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")
star_mod = types.ModuleType("astrbot.api.star")
platform_mod = types.ModuleType("astrbot.core.platform")
msgtype_mod = types.ModuleType("astrbot.core.platform.message_type")


class MessageType:
    GROUP_MESSAGE = "GROUP_MESSAGE"
    FRIEND_MESSAGE = "FRIEND_MESSAGE"
    OTHER_MESSAGE = "OTHER_MESSAGE"


class _Filter:
    def llm_tool(self, name=None, **kw):
        def deco(f):
            f._llm_tool_name = name
            return f
        return deco

    def __getattr__(self, name):
        # command / command_group / permission_type / event_message_type 等所有
        # 装饰器全部直通：单测只跑方法体，不关心注册机制。
        def deco(*a, **kw):
            if len(a) == 1 and callable(a[0]) and not kw:
                return a[0]
            def inner(f):
                return f
            return inner
        return deco

    def event_message_type(self, t, priority=0):
        def deco(f):
            return f
        return deco

    def after_message_sent(self):
        def deco(f):
            return f
        return deco

    class EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"


class _Logger:
    def info(self, *a):
        print("[INFO]", *a)

    def warning(self, *a):
        print("[WARN]", *a)

    def error(self, *a):
        print("[ERROR]", *a)

    def debug(self, *a):
        pass


class AstrBotConfig(dict):
    pass


class FakeMessageObj:
    def __init__(self):
        self.raw_message = None
        self.type = MessageType.FRIEND_MESSAGE


class FakeEvent:
    """模拟 AstrMessageEvent + aiocqhttp 事件组合。"""

    def __init__(self, text="", sender_id="", sender_name="", is_admin=False,
                 group=False, raw=None):
        self.message_str = text
        self.message_obj = FakeMessageObj()
        self.message_obj.raw_message = raw  # dict（OneBot 原始事件）
        self.message_obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        self.unified_msg_origin = "aiocqhttp:FriendMessage:x"
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._is_admin = is_admin

    def get_message_type(self):
        return self.message_obj.type

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_admin(self):
        return self._is_admin

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return ""


filter = _Filter()
event_mod.filter = filter
event_mod.AstrMessageEvent = FakeEvent
event_mod.MessageEventResult = object
event_mod.MessageChain = object
star_mod.Context = object
star_mod.Star = object
comp_mod = types.ModuleType("astrbot.api.message_components")


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _Image:
    @staticmethod
    def fromURL(url):
        return {"url": url}


comp_mod.Plain = _Plain
comp_mod.Image = _Image
astrbot_api.message_components = comp_mod
sys.modules["astrbot.api.message_components"] = comp_mod
astrbot_api.event = event_mod
astrbot_api.star = star_mod
astrbot_api.AstrBotConfig = AstrBotConfig
astrbot_api.logger = _Logger()
astrbot.api = astrbot_api
astrbot.core = types.ModuleType("astrbot.core")
msgtype_mod.MessageType = MessageType
astrbot.core.platform = platform_mod
platform_mod.message_type = msgtype_mod
sess_mod = types.ModuleType("astrbot.core.platform.message_session")
sess_mod.MessageSession = object
platform_mod.message_session = sess_mod
mod_pkg = types.ModuleType("astrbot.core.platform")
mod_pkg.__path__ = []  # 标记为包，让子模块导入走 sys.modules
platform_mod.__path__ = []
astrbot.core.__path__ = []
sys.modules.update({
    "astrbot": astrbot, "astrbot.api": astrbot_api,
    "astrbot.api.event": event_mod, "astrbot.api.star": star_mod,
    "astrbot.api.message_components": comp_mod,
    "astrbot.core": astrbot.core, "astrbot.core.platform": platform_mod,
    "astrbot.core.platform.message_type": msgtype_mod,
    "astrbot.core.platform.message_session": sess_mod,
})

import importlib.util
spec = importlib.util.spec_from_file_location(
    "gha_main",
    r"C:/Users/Administrator/.astrbot_launcher/instances/1d216365-92e9-4fc5-a411-3d8aa5ecc6c8/core/data/plugins/astrbot_plugin_group_history_agent/main.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
PluginCls = mod.GroupHistoryAgentPlugin

plugin = object.__new__(PluginCls)
plugin.config = AstrBotConfig({
    "requests": {
        "requests_enabled": True, "requests_ttl_hours": 24,
        "requests_max": 50, "owner_only": True,
    },
})
plugin.context = types.SimpleNamespace()
plugin._pending_requests = {}

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}", detail)
    if not cond:
        failures.append(name)


# ---------- 用例 ----------
async def run():
    # 1. NapCat 软申请文本（私聊）
    ev = FakeEvent(text="请求添加你为好友", sender_id="10001", sender_name="小明")
    r = plugin._catch_soft_request_text(ev)
    check("软申请: 私聊文本被收录", r is True)
    row = plugin._pending_requests.get("soft:10001")
    check("软申请: soft 标记与字段", bool(row) and row.get("soft") is True
          and row.get("user_id") == "10001" and row.get("kind") == "friend")

    # 2. 同人重复发不再堆叠（1 小时内有记录）
    ev2 = FakeEvent(text="请求添加你为好友", sender_id="10001", sender_name="小明")
    r2 = plugin._catch_soft_request_text(ev2)
    check("软申请: 重复文本去重", r2 is False and len(plugin._pending_requests) == 1)

    # 3. 群里的同款文本不认
    ev3 = FakeEvent(text="他刚才请求添加你为好友来着", sender_id="999", group=True)
    r3 = plugin._catch_soft_request_text(ev3)
    check("软申请: 群聊文本不认", r3 is False)

    # 4. 1 小时内有真申请时软申请不覆盖（不干扰真 flag）
    plugin._pending_requests["real_flag_x"] = {
        "kind": "friend", "user_id": "555", "nickname": "real", "ts": time.time(),
    }
    ev4 = FakeEvent(text="请求添加你为好友", sender_id="555", sender_name="real")
    r4 = plugin._catch_soft_request_text(ev4)
    check("软申请: 有真申请不覆盖", r4 is False and "soft:555" not in plugin._pending_requests)
    del plugin._pending_requests["real_flag_x"]

    # 5. 真 request 事件 flag 为空 → noflag 前缀照样收录
    ev5 = FakeEvent(raw={"post_type": "request", "request_type": "friend",
                         "user_id": 10001, "nickname": "小明", "flag": ""})
    r5 = plugin._catch_request_event(ev5)
    keys5 = list(plugin._pending_requests.keys())
    check("noflag: flag 空也收录", r5 is True and any(k.startswith("noflag:") for k in keys5), keys5)

    # 6. handle_request 对 soft 条目拦截
    # 6. handle_request 对 soft 条目拦截（doubt 接口无数据 → 降级提示）
    class FakeApi:
        def __init__(self, doubt=None):
            self.doubt = doubt or []
            self.calls = []
        async def call_action(self, action, **kw):
            self.calls.append((action, kw))
            if action == "get_doubt_friends_add_request":
                return self.doubt
            return {}
    class FakeClient:
        def __init__(self, api=None):
            self.api = api or FakeApi()
    plugin._fake_client = FakeClient()
    plugin._get_client = lambda event: plugin._fake_client
    res6 = await plugin.handle_request(FakeEvent(is_admin=True), flag="soft:10001", approve=True)
    check("handle: soft 拦截", "没有 flag" in res6 and "别再对这条调本工具" in res6)

    # 7. owner_only=true：非 admin 查列表/处理被拒
    res7 = await plugin.list_pending_requests(FakeEvent(is_admin=False))
    check("owner_only: 非 admin 查列表被拒", "权限拒绝" in res7)
    res7b = await plugin.handle_request(FakeEvent(is_admin=False), flag="1", approve=True)
    check("owner_only: 非 admin 处理被拒", "权限拒绝" in res7b)

    # 8. owner_only=true：admin 正常查列表（能看到 soft 条目与警告）
    res8 = await plugin.list_pending_requests(FakeEvent(is_admin=True))
    check("owner_only: admin 可查", "权限拒绝" not in res8 and "软申请" in res8)

    # 9. owner_only=false：非 admin 放行（处理 soft 仍被拦，返回的是 soft 提示而非权限拒绝）
    plugin.config["requests"]["owner_only"] = False
    res9 = await plugin.list_pending_requests(FakeEvent(is_admin=False))
    check("owner_only=off: 非 admin 可查", "权限拒绝" not in res9)
    res9b = await plugin.handle_request(FakeEvent(is_admin=False), flag="soft:10001", approve=True)
    check("owner_only=off: soft 仍拦（非权限原因）", "没有 flag" in res9b)
    plugin.config["requests"]["owner_only"] = True

    # 10. _fmt_request soft 展示
    s = plugin._fmt_request(1, "soft:10001", plugin._pending_requests["soft:10001"])
    check("fmt: soft 带警告", "软申请" in s and "不能代处理" in s)

    # 11. 真 flag 正常路径不受影响（mock client 成功）
    plugin._pending_requests["real_flag_y"] = {
        "kind": "friend", "user_id": "666", "nickname": "ok", "ts": time.time(),
    }
    plugin._fake_client = FakeClient()
    res11 = await plugin.handle_request(FakeEvent(is_admin=True), flag="real_flag_y", approve=True)
    check("handle: 正常 flag 走通", "已同意" in res11 and "real_flag_y" not in plugin._pending_requests)

    # 12. 可疑申请通道：doubt 接口返回真实 flag → soft 条目直接代处理
    plugin._pending_requests["soft:777"] = {
        "kind": "friend", "user_id": "777", "nickname": "doubt_guy",
        "comment": "请求添加你为好友", "ts": time.time(), "soft": True,
    }
    api12 = FakeApi(doubt=[{"user_id": 777, "nickname": "doubt_guy", "flag": "doubt_flag_777"}])
    plugin._fake_client = FakeClient(api12)
    res12 = await plugin.handle_request(FakeEvent(is_admin=True), flag="soft:777", approve=True)
    check("doubt: 可疑通道代处理成功", "已同意" in res12 and "soft:777" not in plugin._pending_requests,
          res12[:60])
    check("doubt: 调了 set_doubt_friends_add_request",
          any(c[0] == "set_doubt_friends_add_request" and c[1].get("flag") == "doubt_flag_777"
              for c in api12.calls))
    check("doubt: approve=True 传入",
          any(c[0] == "set_doubt_friends_add_request" and c[1].get("approve") is True
              for c in api12.calls))

    # 13. list_pending_requests 同步升级：soft 条目在列表时被升级为真 flag
    plugin._pending_requests["soft:888"] = {
        "kind": "friend", "user_id": "888", "nickname": "sync_guy",
        "comment": "请求添加你为好友", "ts": time.time(), "soft": True,
    }
    api13 = FakeApi(doubt=[{"user_id": 888, "nickname": "sync_guy", "flag": "doubt_flag_888"}])
    plugin._fake_client = FakeClient(api13)
    res13 = await plugin.list_pending_requests(FakeEvent(is_admin=True))
    check("list: soft 升级为真flag", "doubt_flag_888" in plugin._pending_requests
          and "soft:888" not in plugin._pending_requests)
    # 小明 的 soft 条目（doubt 列表里没有）保持原样，升级条目正常展示
    check("list: 升级条目展示/未升级条目保留",
          "sync_guy" in res13 and "doubt_flag_888" in res13 and "小明" in res13)

asyncio.run(run())
print()
print("FAILED:", failures if failures else "无，全部通过")
sys.exit(1 if failures else 0)
