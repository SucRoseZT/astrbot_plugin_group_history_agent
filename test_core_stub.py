# -*- coding: utf-8 -*-
"""使用桩模块模拟 AstrBot 环境，实测插件的核心逻辑（不依赖真实 AstrBot）。"""
import asyncio
import sys
import types

# ---------- 桩模块 ----------
astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")
star_mod = types.ModuleType("astrbot.api.star")


class _Filter:
    def llm_tool(self, name=None, **kw):
        def deco(f):
            f._llm_tool_name = name
            return f
        return deco

    def command(self, name=None, **kw):
        def deco(f):
            f._command_name = name
            return f
        return deco


class AstrBotConfig(dict):
    pass


class _Logger:
    def info(self, *a):
        pass

    def warning(self, *a):
        print("[WARN]", *a)

    def error(self, *a):
        print("[ERROR]", *a)


class AstrMessageEvent:
    def __init__(self, platform="aiocqhttp", group_id="123456", umo="aiocqhttp:GROUP:123456", bot=None, message_str="", sender_id="", message=None):
        self._platform = platform
        self._gid = group_id
        self.unified_msg_origin = umo
        self.bot = bot
        self.message_str = message_str
        self.sender_id = sender_id
        self.message = message

    def get_platform_name(self):
        return self._platform

    def get_group_id(self):
        return self._gid

    def plain_result(self, text):
        return text


class MessageEventResult:
    pass


class Context:
    pass


class Star:
    def __init__(self, context):
        self.context = context


filter = _Filter()
event_mod.filter = filter
event_mod.AstrMessageEvent = AstrMessageEvent
event_mod.MessageEventResult = MessageEventResult
star_mod.Context = Context
star_mod.Star = Star
astrbot_api.event = event_mod
astrbot_api.star = star_mod
astrbot_api.AstrBotConfig = AstrBotConfig
astrbot_api.logger = _Logger()
astrbot.api = astrbot_api

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = astrbot_api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod

# 模拟 aiocqhttp 事件类所在模块，使插件内部 import 成功
aiocq_mod = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
aiocq_ev = types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
aiocq_ev.AiocqhttpMessageEvent = AstrMessageEvent
aiocq_mod.aiocqhttp_message_event = aiocq_ev
sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
sys.modules["astrbot.core.platform"] = types.ModuleType("astrbot.core.platform")
sys.modules["astrbot.core.platform.sources"] = types.ModuleType("astrbot.core.platform.sources")
sys.modules["astrbot.core.platform.sources.aiocqhttp"] = aiocq_mod
sys.modules["astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"] = aiocq_ev

# ---------- 导入被测插件 ----------
import importlib.util

spec = importlib.util.spec_from_file_location(
    "main", "/home/user/Doubao/chats/38448975758001666/astrbot_plugin_group_history_agent/main.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
PluginCls = mod.GroupHistoryAgentPlugin

# 构造插件实例（不触发 __init__ 的装饰器注册，直接用对象）
plugin = object.__new__(PluginCls)
plugin.config = AstrBotConfig({"default_count": 20, "max_count": 100, "max_content_length": 200, "enable_fallback": True})
plugin.context = Context()


async def _get_curr_cid(uid):
    return "cid-1"


async def _get_conv(uid, cid):
    return types.SimpleNamespace(
        history='[{"role":"user","content":"你好"},{"role":"assistant","content":"你好呀"}]'
    )


plugin.context.conversation_manager = types.SimpleNamespace(
    get_curr_conversation_id=_get_curr_cid,
    get_conversation=_get_conv,
)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


# 1. 条数规范化
check("normalize default", plugin._normalize_count("") == 20, plugin._normalize_count(""))
check("normalize clamp", plugin._normalize_count("9999") == 100)
check("normalize bad", plugin._normalize_count("abc") == 20)
check("normalize valid", plugin._normalize_count("5") == 5)

# 2. 消息段解析
seg_text = plugin._segments_to_text(
    [
        {"type": "text", "data": {"text": "大家好"}},
        {"type": "at", "data": {"qq": "0"}},
        {"type": "image", "data": {}},
        {"type": "face", "data": {}},
        {"type": "record", "data": {}},
        {"type": "unknown", "data": {}},
    ]
)
print("   segments ->", repr(seg_text))
check("segments text", "大家好" in seg_text and "@全体成员" in seg_text)
check("segments placeholders", "[图片]" in seg_text and "[语音]" in seg_text and "[unknown]" in seg_text)

# 3. 协议端返回结构兼容 + 格式化
sample_msgs = [
    {
        "time": 1700000010,
        "sender": {"user_id": 10001, "card": "阿伟", "nickname": "aw"},
        "message": [{"type": "text", "data": {"text": "第一条"}}],
    },
    {
        "time": 1700000020,
        "sender": {"user_id": 10002, "nickname": "路人"},
        "message": [{"type": "text", "data": {"text": "第二条 带很长内容" * 50}}],
    },
]
for wrapped in [{"data": {"messages": sample_msgs}}, {"messages": sample_msgs}, sample_msgs]:
    got = plugin._extract_messages(wrapped)
    check("extract len", len(got) == 2, type(wrapped).__name__)
formatted = plugin._format_messages(sample_msgs)
print(formatted)
check("format order", "第一条" in formatted.split("\n")[0])
check("format truncate", "…" in formatted)

# 4. 完整链路：模拟协议端 client
class FakeClient:
    def __init__(self, ret):
        self._ret = ret
        self.api = types.SimpleNamespace(call_action=self._call)

    async def _call(self, action, **kw):
        assert action == "get_group_msg_history"
        assert kw["group_id"] == 123456
        assert kw["count"] == 20
        return self._ret


async def run_chain():
    # 协议端可用
    ev = AstrMessageEvent(bot=FakeClient({"data": {"messages": sample_msgs}}))
    text = await plugin._read_recent(ev, "123456", 20)
    check("chain platform ok", "第一条" in text, "len=%d" % len(text))

    # 协议端返回空 -> 回退会话历史
    ev2 = AstrMessageEvent(bot=FakeClient({}))
    text2 = await plugin._read_recent(ev2, "123456", 20)
    check("chain fallback", "用户: 你好" in text2, repr(text2))

    # 非 aiocqhttp 平台 -> 回退会话历史
    ev3 = AstrMessageEvent(platform="telegram", bot=None)
    text3 = await plugin._read_recent(ev3, "123456", 20)
    check("chain non-aiocqhttp fallback", "机器人: 你好呀" in text3)

    # 工具装饰器已注册
    tool_fn = getattr(mod.GroupHistoryAgentPlugin, "read_group_recent_messages")
    check("llm_tool registered", getattr(tool_fn, "_llm_tool_name", None) == "read_group_recent_messages")
    cmd_fn = getattr(mod.GroupHistoryAgentPlugin, "recent_messages_command")
    check("command registered", getattr(cmd_fn, "_command_name", None) == "最近消息")
    sc_fn = getattr(mod.GroupHistoryAgentPlugin, "tool_self_check")
    check("selfcheck command registered", getattr(sc_fn, "_command_name", None) == "工具自检")
    brief_fn = getattr(mod.GroupHistoryAgentPlugin, "group_brief_command")
    check("brief command registered", getattr(brief_fn, "_command_name", None) == "群简报")


# 6. 自检指令：工具已注册 / 未注册 / 管理器不可用 三种分支
class FakeToolMgr:
    def __init__(self, has_tool=True):
        self.has_tool = has_tool
        self.func_list = [types.SimpleNamespace(name="read_group_recent_messages")] if has_tool else []

    def get_func(self, name):
        return object() if self.has_tool and name == "read_group_recent_messages" else None


async def collect(gen):
    out = []
    while True:
        try:
            out.append(await gen.__anext__())
        except StopAsyncIteration:
            break
    return out


async def run_selfcheck(plugin_ctx, ev):
    return await collect(plugin_ctx.tool_self_check(ev))


ev_sc = AstrMessageEvent()
p_found = PluginCls(Context(), None)
p_found.context = types.SimpleNamespace(get_llm_tool_manager=lambda: FakeToolMgr(True))
out = asyncio.run(run_selfcheck(p_found, ev_sc))
check("selfcheck found", "✅" in out[0], repr(out))

p_missing = PluginCls(Context(), None)
p_missing.context = types.SimpleNamespace(get_llm_tool_manager=lambda: FakeToolMgr(False))
out2 = asyncio.run(run_selfcheck(p_missing, ev_sc))
check("selfcheck missing", "❌" in out2[0], repr(out2))

p_nomgr = PluginCls(Context(), None)
p_nomgr.context = types.SimpleNamespace()
out3 = asyncio.run(run_selfcheck(p_nomgr, ev_sc))
check("selfcheck no mgr", "无法获取工具管理器" in out3[0], repr(out3))

asyncio.run(run_chain())

# 7. 群简报指令：读取记录 -> LLM 总结（新 API llm_generate）
async def fake_provider_id(umo):
    return "prov-1"


async def fake_llm_generate(chat_provider_id, prompt):
    assert chat_provider_id == "prov-1"
    assert "群聊记录" in prompt or "聊天记录" in prompt
    return types.SimpleNamespace(completion_text="【转述】小明今天讨论了新版本发布，大家约定周五前完成测试。")


brief_ctx = types.SimpleNamespace(
    get_current_chat_provider_id=fake_provider_id,
    llm_generate=fake_llm_generate,
)
p_brief = PluginCls(Context(), None)
p_brief.config = AstrBotConfig({"default_count": 20, "max_count": 100, "max_content_length": 200, "enable_fallback": True})
p_brief.context = brief_ctx
ev_brief = AstrMessageEvent(
    bot=FakeClient({"data": {"messages": sample_msgs}}), message_str="/群简报 20"
)
out_brief = asyncio.run(collect(p_brief.group_brief_command(ev_brief)))
check("brief summary new api", "【转述】" in out_brief[0], repr(out_brief))

# 8. 群简报：旧 API 回退（无 llm_generate 时走 text_chat）
class FakeProvider:
    async def text_chat(self, prompt, context, system_prompt):
        return types.SimpleNamespace(completion_text="【转述-旧API】老张说周末组织活动。")


async def fake_using_provider(umo):
    return FakeProvider()


p_brief2 = PluginCls(Context(), None)
p_brief2.config = AstrBotConfig({"default_count": 20, "max_count": 100, "max_content_length": 200, "enable_fallback": True})
p_brief2.context = types.SimpleNamespace(get_using_provider_async=fake_using_provider)
out_brief2 = asyncio.run(collect(p_brief2.group_brief_command(ev_brief)))
check("brief summary legacy api", "【转述-旧API】" in out_brief2[0], repr(out_brief2))

# 9. 群简报：无群号时给出用法提示
ev_no_group = AstrMessageEvent(platform="telegram", group_id="", bot=None, message_str="/群简报")
p_brief3 = PluginCls(Context(), None)
p_brief3.config = AstrBotConfig({})
p_brief3.context = types.SimpleNamespace()
out_brief3 = asyncio.run(collect(p_brief3.group_brief_command(ev_no_group)))
check("brief usage hint", "用法" in out_brief3[0], repr(out_brief3))

# 10. 群简报：配置独立 API 时优先走独立 API
async def fake_direct_api(prompt):
    return "【独立API】结论摘要"


p_brief_api = PluginCls(Context(), None)
p_brief_api.config = AstrBotConfig({"brief_model": "gpt-4o-mini", "brief_api_base": "https://x", "brief_api_key": "k"})
p_brief_api.context = types.SimpleNamespace()
p_brief_api._llm_summarize_direct_api = fake_direct_api
out_api = asyncio.run(collect(p_brief_api.group_brief_command(ev_brief)))
check("brief direct api priority", "【独立API】" in out_api[0], repr(out_api))

# 11. 群简报：全部 LLM 路径失败时输出原因 + 原始记录
p_brief_fail = PluginCls(Context(), None)
p_brief_fail.config = AstrBotConfig({"brief_model": "", "default_count": 20, "max_count": 100, "max_content_length": 200, "enable_fallback": True})
p_brief_fail.context = types.SimpleNamespace()
out_fail = asyncio.run(collect(p_brief_fail.group_brief_command(ev_brief)))
check("brief fail reason shown", "LLM 总结失败" in out_fail[0] and "以下是原始记录" in out_fail[0], repr(out_fail))

# 12. 群简报：遍历全部提供商兜底
class FakeProvider2:
    async def text_chat(self, prompt, context, system_prompt):
        return types.SimpleNamespace(completion_text="【兜底】张三李四聊了项目进度。")


p_brief_all = PluginCls(Context(), None)
p_brief_all.config = AstrBotConfig({"brief_model": "", "default_count": 20, "max_count": 100, "max_content_length": 200, "enable_fallback": True})
p_brief_all.context = types.SimpleNamespace(get_all_providers=lambda: [FakeProvider2()])
out_all = asyncio.run(collect(p_brief_all.group_brief_command(ev_brief)))
check("brief all-providers fallback", "【兜底】" in out_all[0], repr(out_all))

# 13. 按人检索：模糊匹配判断
check("match card", PluginCls._match_member({"sender": {"card": "正小豆", "user_id": 1}}, "正小"))
check("match nickname", PluginCls._match_member({"sender": {"nickname": "肥鱼", "user_id": 2}}, "肥鱼"))
check("match uid partial", PluginCls._match_member({"sender": {"nickname": "某人", "user_id": 123456}}, "1234"))
check("no match", not PluginCls._match_member({"sender": {"nickname": "甲", "user_id": 1}}, "乙"))

# 14. 群解析：数字群号 / 当前群 / 私聊缺群 / 群名模糊
async def _resolve_test(plugin_ctx, ev, q):
    return await plugin_ctx._resolve_group(ev, q)


p_res = PluginCls(Context(), None)
p_res.config = AstrBotConfig({})
# 数字群号
gid, label, err = asyncio.run(_resolve_test(p_res, AstrMessageEvent(platform="telegram", group_id=""), "123456"))
check("resolve numeric", gid == "123456" and err == "", (gid, label, err))
# 群聊中不传群 -> 当前群
gid2, _, err2 = asyncio.run(_resolve_test(p_res, AstrMessageEvent(group_id="888"), ""))
check("resolve current group", gid2 == "888" and err2 == "", (gid2, err2))
# 私聊不传群 -> 报错
_, _, err3 = asyncio.run(_resolve_test(p_res, AstrMessageEvent(platform="telegram", group_id=""), ""))
check("resolve private no group err", "私聊" in err3, err3)


# 15. 按人检索：协议端翻页 + 群名模糊（私聊可用）
class DispatchClient:
    def __init__(self, handler):
        self._handler = handler
        self.api = types.SimpleNamespace(call_action=self._call)

    async def _call(self, action, **kw):
        return await self._handler(action, kw)


def make_page_messages():
    return [
        {"time": 1700000003, "message_seq": 30, "message_id": "m30", "sender": {"user_id": 10001, "card": "正小豆", "nickname": "zd"},
         "message": [{"type": "text", "data": {"text": "早上好"}}]},
        {"time": 1700000002, "message_seq": 29, "message_id": "m29", "sender": {"user_id": 10002, "nickname": "路人甲"},
         "message": [{"type": "text", "data": {"text": "路过"}}]},
        {"time": 1700000001, "message_seq": 28, "message_id": "m28", "sender": {"user_id": 10001, "card": "正小豆", "nickname": "zd"},
         "message": [{"type": "text", "data": {"text": "早"}}]},
    ]


calls = []


async def history_handler(action, kw):
    calls.append((action, kw))
    if action == "get_group_list":
        return [{"group_id": "123456", "group_name": "测试群-项目A"}]
    if action == "get_group_msg_history":
        seq = kw.get("message_seq")
        if seq is None:
            return {"data": {"messages": make_page_messages()}}
        if seq == 28:
            return {"data": {"messages": [
                {"time": 1699999999, "message_seq": 27, "message_id": "m27", "sender": {"user_id": 10001, "card": "正小豆", "nickname": "zd"},
                 "message": [{"type": "text", "data": {"text": "下班聊"}}]},
            ]}}
        return {"data": {"messages": []}}


p_search = PluginCls(Context(), None)
p_search.config = AstrBotConfig({"enable_fallback": False, "search_max_hits": 20, "search_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_search.context = types.SimpleNamespace()
# 私聊场景：通过群名模糊定位 + 按人模糊查询
ev_search = AstrMessageEvent(
    platform="aiocqhttp", group_id="", bot=DispatchClient(history_handler),
    message_str="/查人 正小 项目",
)
out_search = asyncio.run(collect(p_search.search_member_command(ev_search)))
check("search by name fuzzy in private", "正小豆" in out_search[0] and "下班聊" in out_search[0], repr(out_search))
check("search pagination passed seq", any(kw.get("message_seq") == 28 for a, kw in calls if a == "get_group_msg_history"), str(calls))
check("search group fuzzy used list", any(a == "get_group_list" for a, _ in calls), str(calls))

# 16. 查不到时给出提示
ev_none = AstrMessageEvent(
    platform="aiocqhttp", group_id="123456", bot=DispatchClient(history_handler),
    message_str="/查人 不存在的名字",
)
out_none = asyncio.run(collect(p_search.search_member_command(ev_none)))
check("search no hit hint", "未找到" in out_none[0], repr(out_none))

# 17. 新工具与指令已注册
sm_fn = getattr(mod.GroupHistoryAgentPlugin, "search_group_member_messages")
check("member tool registered", getattr(sm_fn, "_llm_tool_name", None) == "search_group_member_messages")
sm_cmd = getattr(mod.GroupHistoryAgentPlugin, "search_member_command")
check("search command registered", getattr(sm_cmd, "_command_name", None) == "查人")

# 18. 私聊记录：当前对话对方解析
p_peer = PluginCls(Context(), None)
p_peer.config = AstrBotConfig({})
check("peer group none", PluginCls._current_peer_uid(p_peer, AstrMessageEvent(group_id="1")) == "")
check("peer sender_id", PluginCls._current_peer_uid(p_peer, AstrMessageEvent(group_id="", sender_id="77777")) == "77777")
check("peer message dict", PluginCls._current_peer_uid(p_peer, AstrMessageEvent(group_id="", message={"sender": {"user_id": 88888}})) == "88888")

# 19. 私聊记录：好友解析
async def _resolve_friend_test(p, ev, q):
    return await p._resolve_friend(ev, q)


p_fr = PluginCls(Context(), None)
p_fr.config = AstrBotConfig({})
_, label1, err1 = asyncio.run(_resolve_friend_test(p_fr, AstrMessageEvent(group_id="", sender_id="77777"), ""))
check("friend resolve current peer", label1 == "77777" and err1 == "", (label1, err1))
_, _, err2 = asyncio.run(_resolve_friend_test(p_fr, AstrMessageEvent(group_id="9"), ""))
check("friend resolve group no friend err", "私聊" in err2 or "指定" in err2, err2)
gid3, label3, err3 = asyncio.run(_resolve_friend_test(p_fr, AstrMessageEvent(group_id=""), "123456"))
check("friend resolve numeric", gid3 == "123456" and err3 == "", (gid3, label3, err3))

# 20. 私聊记录：协议端翻页 + 当前对方 + 名称模糊
friend_calls = []


async def friend_handler(action, kw):
    friend_calls.append((action, kw))
    if action == "get_friend_list":
        return [{"user_id": 55555, "nickname": "老张", "remark": "张工"}]
    if action == "get_friend_msg_history":
        seq = kw.get("message_seq")
        if seq is None:
            return {"data": {"messages": [
                {"time": 1700000002, "message_seq": 20, "message_id": "f20", "sender": {"user_id": 55555, "nickname": "老张"},
                 "message": [{"type": "text", "data": {"text": "好的没问题"}}]},
                {"time": 1700000001, "message_seq": 19, "message_id": "f19", "sender": {"user_id": 55555, "nickname": "老张"},
                 "message": [{"type": "text", "data": {"text": "在吗"}}]},
            ]}}
        if seq == 19:
            return {"data": {"messages": [
                {"time": 1700000000, "message_seq": 18, "message_id": "f18", "sender": {"user_id": 55555, "nickname": "老张"},
                 "message": [{"type": "text", "data": {"text": "早"}}]},
            ]}}
        return {"data": {"messages": []}}


p_fr2 = PluginCls(Context(), None)
p_fr2.config = AstrBotConfig({"enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_fr2.context = types.SimpleNamespace()
ev_fr = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str="/私聊记录 20")
out_fr = asyncio.run(collect(p_fr2.friend_history_command(ev_fr)))
check("friend history current peer", "好的没问题" in out_fr[0] and "早" in out_fr[0], repr(out_fr))
ev_fr2 = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="66666", bot=DispatchClient(friend_handler), message_str="/私聊记录 20 张工")
out_fr2 = asyncio.run(collect(p_fr2.friend_history_command(ev_fr2)))
check("friend history fuzzy name", "老张" in out_fr2[0] and "张工" in out_fr2[0], repr(out_fr2))
check("friend history used get_friend_list", any(a == "get_friend_list" for a, _ in friend_calls), str(friend_calls))

# 21. 私聊记录：回退会话历史
p_fr3 = PluginCls(Context(), None)
p_fr3.config = AstrBotConfig({"enable_fallback": True, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_fr3.context = types.SimpleNamespace(conversation_manager=types.SimpleNamespace(
    get_curr_conversation_id=_get_curr_cid,
    get_conversation=_get_conv,
))
ev_fr3 = AstrMessageEvent(platform="telegram", group_id="", sender_id="55555", message_str="/私聊记录 20")
out_fr3 = asyncio.run(collect(p_fr3.friend_history_command(ev_fr3)))
check("friend history fallback conv", "用户: 你好" in out_fr3[0], repr(out_fr3))

# 22. 私聊记录工具与指令已注册
fr_fn = getattr(mod.GroupHistoryAgentPlugin, "read_friend_recent_messages")
check("friend tool registered", getattr(fr_fn, "_llm_tool_name", None) == "read_friend_recent_messages")
fr_cmd = getattr(mod.GroupHistoryAgentPlugin, "friend_history_command")
check("friend command registered", getattr(fr_cmd, "_command_name", None) == "私聊记录")

# 23. 异常兜底：内部异常必须转成可读文本（绝不空返回）
class BadEvent:
    def get_group_id(self):
        raise RuntimeError("boom")
    def get_platform_name(self):
        return "aiocqhttp"
    def unified_msg_origin(self):
        return "x"
    def plain_result(self, t):
        return t


text_bad = asyncio.run(plugin._search_member(BadEvent(), "小红", "", 20))
check("search internal error text", "错误" in text_bad, repr(text_bad))
text_bad2 = asyncio.run(plugin._read_friend(BadEvent(), "", 20))
check("friend internal error text", "错误" in text_bad2, repr(text_bad2))

# 24. 群名匹配失败时提示改用群号
_, _, err_miss = asyncio.run(_resolve_test(p_res, AstrMessageEvent(platform="telegram", group_id=""), "不存在的群"))
check("resolve group name miss hint", "群号" in err_miss, err_miss)

# 25. 群聊工具在私聊报错时引导使用私聊工具
p_guide = PluginCls(Context(), None)
p_guide.config = AstrBotConfig({})
ev_guide = AstrMessageEvent(platform="telegram", group_id="", bot=None, message_str="")
out_guide = asyncio.run(p_guide.read_group_recent_messages(ev_guide))
check("group tool private hint", "read_friend_recent_messages" in out_guide, repr(out_guide))

# 26. 指令 LLM 加工输出模式（llm_output=true 且有可用 LLM）
p_llm = PluginCls(Context(), None)
p_llm.config = AstrBotConfig({"llm_output": True, "enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100, "search_max_hits": 20, "search_scan_limit": 200})
p_llm.context = types.SimpleNamespace(get_all_providers=lambda: [FakeProvider2()])
ev_llm = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str="/私聊记录 20 张工")
out_llm = asyncio.run(collect(p_llm.friend_history_command(ev_llm)))
check("llm output mode", "【兜底】" in out_llm[0], repr(out_llm))

# 27. LLM 失败时回退原文
p_llm2 = PluginCls(Context(), None)
p_llm2.config = AstrBotConfig({"llm_output": True, "enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_llm2.context = types.SimpleNamespace()
ev_llm2 = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str="/私聊记录 20 张工")
out_llm2 = asyncio.run(collect(p_llm2.friend_history_command(ev_llm2)))
check("llm fail fallback raw", "老张" in out_llm2[0], repr(out_llm2))

# 28. llm_output=false 直发原文
p_llm3 = PluginCls(Context(), None)
p_llm3.config = AstrBotConfig({"llm_output": False, "enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_llm3.context = types.SimpleNamespace()
ev_llm3 = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str="/私聊记录 20 张工")
out_llm3 = asyncio.run(collect(p_llm3.friend_history_command(ev_llm3)))
check("llm_output off raw", "老张" in out_llm3[0], repr(out_llm3))

# 29. 工具返回带"自然讨论"引导（不逐条复述/不全说完）
out_s_hint = asyncio.run(p_search.search_group_member_messages(
    AstrMessageEvent(platform="aiocqhttp", group_id="", bot=DispatchClient(history_handler), message_str=""),
    "正小", "项目", "20"))
check("search tool discuss hint", "仅供你参考" in out_s_hint, repr(out_s_hint))
p_fr_hint = PluginCls(Context(), None)
p_fr_hint.config = AstrBotConfig({"enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_fr_hint.context = types.SimpleNamespace()
out_fr_hint = asyncio.run(p_fr_hint.read_friend_recent_messages(
    AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str=""),
    "张工", "20"))
check("friend tool discuss hint", "仅供你参考" in out_fr_hint, repr(out_fr_hint))

# 30. 指令 LLM 提示词为"自然讨论"风格
class CaptureProvider:
    def __init__(self):
        self.prompt = ""
        self.system_prompt = ""

    async def text_chat(self, prompt, context, system_prompt):
        self.prompt = prompt
        self.system_prompt = system_prompt
        return types.SimpleNamespace(completion_text="嗯，我注意到他提到了蛋糕店的事。")


cp = CaptureProvider()
p_disc = PluginCls(Context(), None)
p_disc.config = AstrBotConfig({"llm_output": True, "enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_disc.context = types.SimpleNamespace(get_all_providers=lambda: [cp])
ev_disc = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str="/私聊记录 20 张工")
out_disc = asyncio.run(collect(p_disc.friend_history_command(ev_disc)))
check("discussion prompt style", "像朋友一样" in cp.prompt and "不要一次性" in cp.prompt and "分段" in cp.prompt, cp.prompt[:120])
check("discussion reply used", "蛋糕店" in out_disc[0], repr(out_disc))
check("no persona fallback sys", "自然的聊天者" in cp.system_prompt, repr(cp.system_prompt))

# 31. 读取 AstrBot 人格提示词作为系统提示词（保持人设口吻）
class PersonaCaptureProvider:
    def __init__(self):
        self.system_prompt = ""
        self.prompt = ""

    async def text_chat(self, prompt, context, system_prompt):
        self.system_prompt = system_prompt
        self.prompt = prompt
        return types.SimpleNamespace(completion_text="好呀~")


pca = PersonaCaptureProvider()
p_persona = PluginCls(Context(), None)
p_persona.config = AstrBotConfig({"llm_output": True, "enable_fallback": False, "friend_scan_limit": 200, "max_content_length": 200, "default_count": 20, "max_count": 100})
p_persona.context = types.SimpleNamespace(
    persona_manager=types.SimpleNamespace(
        get_selected_persona=lambda umo: types.SimpleNamespace(system_prompt="你是小肥鱼，说话活泼、会分段")
    ),
    get_all_providers=lambda: [pca],
)
ev_pe = AstrMessageEvent(platform="aiocqhttp", group_id="", sender_id="55555", bot=DispatchClient(friend_handler), message_str="/私聊记录 20 张工")
out_pe = asyncio.run(collect(p_persona.friend_history_command(ev_pe)))
check("persona prompt used", "小肥鱼" in pca.system_prompt, repr(pca.system_prompt))
check("persona prompt not in user text", pca.prompt.count("小肥鱼") == 0, pca.prompt[:80])

# 32. 工具描述（会进 LLM 工具 schema 的第一段）包含"无需任何权限"等引导
import re as _re

_p_doc = PluginCls(Context(), None)
_doc_first_par = {}
for _name, _fn in [
    ("read_group_recent_messages", _p_doc.read_group_recent_messages),
    ("search_group_member_messages", _p_doc.search_group_member_messages),
    ("read_friend_recent_messages", _p_doc.read_friend_recent_messages),
]:
    _doc = _fn.__doc__ or ""
    _first = _doc.split("\n\n", 1)[0]
    _doc_first_par[_name] = _first
    check(
        f"tool desc {_name} no-permission hint",
        "无需任何权限" in _first,
        _first[:80],
    )
check(
    "tool desc friend fallback hint",
    "因权限不足被拒绝" in _doc_first_par["read_friend_recent_messages"],
    _doc_first_par["read_friend_recent_messages"][:120],
)

# 5. 真实实例化：不传 config（对应缺少 _conf_schema.json 的场景）也能工作
p = PluginCls(Context(), None)
check("init no config ok", isinstance(p.config, dict))
check("normalize w/o config", p._normalize_count("") == 20)
check("clamp w/o config", p._normalize_count("9999") == 100)

print("\n==== RESULT:", "ALL PASS" if not failures else f"FAILED: {failures}", "====")
sys.exit(1 if failures else 0)
