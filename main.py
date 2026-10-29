# -*- coding: utf-8 -*-
"""
AstrBot 插件：群聊记录读取 Agent（Group History Agent）

功能
----
1. 注册 LLM 工具 ``read_group_recent_messages``，让 AI（agent）在群聊对话中能够
   自主调用该工具，读取当前群（或指定群）最近的聊天记录，用于总结、追问、复盘、
   情报整理等场景。
2. 注册 LLM 工具 ``search_group_member_messages``：按名称/QQ号**模糊查询**某个群
   成员最近说过的消息，支持私聊使用（群可用群号或群名指定，群名支持模糊匹配）。
3. 注册 LLM 工具 ``read_friend_recent_messages``：读取与某好友的**私聊记录**，
   私聊会话可直接查当前对话对象，也可按昵称/备注/QQ号模糊指定其他好友。
4. 提供 ``/最近消息 [条数]`` 指令，直接查看原始记录（便于手动测试数据源）。
5. 提供 ``/群简报 [条数] [群号]`` 指令：读取某群最近消息，调用 LLM 生成
   第三人称转述总结（"谁谁谁干了什么、大家聊了什么"），输出给当前会话的人。
6. 提供 ``/查人 [名称] [群号/群名]`` 指令，手动测试按人检索。
7. 提供 ``/私聊记录 [条数] [好友名/QQ]`` 指令，手动测试好友私聊记录。
8. 提供 ``/工具自检`` 指令，排查工具是否注册进 AstrBot 工具管理器。

数据来源
--------
- 首选：平台协议端 API（aiocqhttp / OneBot v11 扩展接口 ``get_group_msg_history``，
  适用于 NapCat、go-cqhttp 等协议端）；
- 回退：AstrBot 会话历史（ConversationManager），适用于所有平台，但仅包含与
  本机器人之间的对话。

要求
----
- AstrBot >= 4.5.1（群简报优先使用 v4.5.7 新增的 llm_generate，旧版本自动回退
  到 text_chat 调用方式）；
- 当前会话使用了支持工具调用（function calling）的模型，且人格（Persona）启用了
  该工具（默认人格默认启用全部工具）。
"""

import asyncio  # noqa: F401  保留导入：部分旧版本 AstrBot 在加载插件时需要
import datetime
import hashlib
import json
import os
import re
import time
import unicodedata
from collections import deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

# 单次工具结果的文本长度上限，防止向模型上下文写入过大的工具结果
_MAX_TOOL_RESULT_CHARS = 6000

# 归一化人名时先剥掉的不可见字符（零宽空格、方向标记、BOM、以及 QQ 昵称里
# 常见的 U+1160 填充符）。昵称「小豆ᅠ⁧喵ᅠ」和「小豆喵」在人眼里是同一个人，
# 不剥掉的话模糊匹配永远对不上。
_INVISIBLE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\u180e\ufeff\u1160\u3164]"
)


class GroupHistoryAgentPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        # AstrBot 仅在插件目录存在 _conf_schema.json 时才向 __init__ 传入 config。
        # 这里兼容缺失配置文件的场景：全部配置项在读取时都有默认值。
        self.config = config if config is not None else AstrBotConfig()
        # 会话消息缓存：{session_id: deque[{ts, sender, sender_id, text, mid, is_bot}]}
        # 为什么要自己存一份：协议端的 get_group_msg_history 经常不含机器人刚发的
        # 消息，也不保证把「正在发生的这几条」都吐出来。只看 API 的话，AI 复盘时
        # 会漏掉自己上一句说了什么。
        self._msg_cache: dict[str, deque] = {}
        self._cache_last_active: dict[str, float] = {}
        self._cache_cleaned_at = 0.0
        # 待处理的加群/好友申请：{flag: {kind, sub_type, user_id, nickname, group_id, comment, ts}}
        # flag 只在事件里出现一次，AI 说话时早过去了，必须自己攒着。
        # 落盘持久化：内存字典重启即丢，用户等 AI 处理申请时 bot 一重启申请就凭空消失。
        self._pending_requests: dict[str, dict] = {}
        self._load_pending_requests()
        # 转述防刷屏状态（原 astrbot_plugin_xrelay 移植）：
        # _relay_sent 记内容指纹（去重），_relay_target_ts 记「来源会话|目标」最近发送时间（冷却）
        self._relay_sent: dict[str, float] = {}
        self._relay_target_ts: dict[str, float] = {}
        # 主动发送（定时任务/跨会话转述）也要进缓存，否则 AI 复盘时看不到自己说过的话
        self._hook_context_send()

    # ------------------------------------------------------------------ #
    # 配置读取                                                            #
    # ------------------------------------------------------------------ #
    def _cfg(self, path: str, default=None):
        """按 "组.键" 读配置，读不到再退回顶层同名键。

        配置页已经改成分类式（read / cache / search / profile / brief / output），
        但老用户的配置文件还是扁平的。这里两级都认，避免升级后被静默重置成默认值。
        """
        node = self.config
        try:
            for part in str(path).split("."):
                node = node[part]
            if node is not None:
                return node
        except (KeyError, TypeError, AttributeError, IndexError):
            pass
        try:
            return self.config.get(str(path).split(".")[-1], default)
        except Exception:
            return default

    def _cfg_int(self, path: str, default: int) -> int:
        try:
            return int(self._cfg(path, default))
        except (TypeError, ValueError):
            return int(default)

    def _cfg_bool(self, path: str, default: bool) -> bool:
        try:
            return bool(self._cfg(path, default))
        except Exception:
            return bool(default)

    # ------------------------------------------------------------------ #
    # LLM 工具：AI 可自主调用的 agent 工具                                #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="read_group_recent_messages")
    async def read_group_recent_messages(
        self, event: AstrMessageEvent, count: str = "", group_id: str = "",
        keyword: str = "", sender: str = "",
        start_time: str = "", end_time: str = "",
    ) -> str:
        """读取当前群聊最近的聊天记录，返回按时间排列的最近若干条消息（每条包含发送者昵称、QQ号、发送时间与消息内容，可选带消息ID）。**此工具无需任何权限，群聊和私聊会话中都可以直接调用**（私聊里也可指定群号读取任意群）。当用户询问"群里刚才聊了什么"、"最近消息"、"帮我看看群里在聊什么"、"总结一下群里最近的讨论"等需要群聊近期内容的问题时使用；要精确找某句话或某个时间段，用 keyword / start_time / end_time 过滤，别把几百条全读进来。注意：本工具只读取群聊记录，查询与某位好友的私聊（QQ聊天）记录请使用 read_friend_recent_messages 工具，查询某个人在群里的发言请使用 search_group_member_messages 工具。

        Args:
            count(string): 要读取的消息条数（数字字符串，例如 "20"），不填则使用插件配置的默认条数
            group_id(string): 目标群号，不填则读取当前消息所在的群
            keyword(string): 只看包含这个词的消息，支持子串匹配（不区分大小写）
            sender(string): 只看这个人发的消息，填昵称或 QQ 号，支持模糊匹配
            start_time(string): 起始时间，格式 YYYY-MM-DD 或 "YYYY-MM-DD HH:MM:SS"
            end_time(string): 结束时间，同上
        """
        gid = group_id or event.get_group_id() or ""
        if not gid:
            return (
                "无法读取群聊记录：当前会话不是群聊，且未提供 group_id 参数。"
                "提示：若要查询与某位好友的私聊（QQ聊天）记录，应改用工具 "
                "read_friend_recent_messages（支持按昵称/备注/QQ号指定好友）；"
                "若要查询某个人在群里的发言，应改用 search_group_member_messages。"
            )

        count = self._normalize_count(count)
        text = await self._read_recent(
            event, gid, count,
            keyword=keyword, sender=sender,
            start_time=start_time, end_time=end_time,
        )
        if not text:
            hint = ""
            if keyword or sender or start_time or end_time:
                hint = "（已按你的筛选条件过滤，可能是条件太严，去掉几个再试）"
            return (
                f"未能读取到群 {gid} 的最近消息{hint}：协议端不支持 get_group_msg_history "
                "接口，且无可用会话历史。请确认协议端（NapCat / go-cqhttp 等）"
                "支持该接口，或检查插件配置中的回退开关。"
            )
        return (
            "（提示：以下记录仅供你参考。请自然地和用户讨论其中的内容，"
            "不要逐条复述，也不要一次性全部讲完。）\n" + text
        )

    # ------------------------------------------------------------------ #
    # LLM 工具：按人模糊查询（支持私聊）                                  #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="search_group_member_messages")
    async def search_group_member_messages(
        self, event: AstrMessageEvent, name: str, group_id: str = "", count: str = ""
    ) -> str:
        """按名称或QQ号模糊查询某个群成员最近说过的消息。**此工具无需任何权限，群聊和私聊会话中都可以直接调用**；群可用群号或群名称指定，均支持模糊匹配。当用户询问"某某某最近说了什么""查一下某个人在群里的发言""谁谁谁干了什么""XX 在群里说过什么"等需要按人检索群聊记录的问题时使用。**如果其他查询类工具因权限不足被拒绝，请优先改用本工具。**注意：群聊会话内可不填 group_id（自动用当前群）；跨群或私聊时 group_id 优先填群号，群名模糊匹配仅作备选。

        Args:
            name(string): 要查询的人，昵称、群名片或QQ号均可，支持模糊匹配
            group_id(string): 群号或群名称，支持模糊匹配；不填时群聊使用当前群，私聊时必须提供
            count(string): 最多返回的匹配消息条数（数字字符串，例如 "20"），不填使用默认值
        """
        try:
            name = (name or "").strip()
            if not name:
                return "请提供要查询的人名或QQ号。"
            if str(count).strip():
                max_hits = self._normalize_count(count)
            else:
                max_hits = int(self._cfg("search.search_max_hits", 20))
            text = await self._search_member(event, name, group_id, max_hits)
            if not text:
                return (
                    f"未找到与“{name}”匹配的发言记录（请确认名称，或在私聊时提供群号/群名）。"
                )
            return (
                "（提示：以下记录仅供你参考。请自然地和用户讨论其中的内容，"
                "不要逐条复述，也不要一次性全部讲完。）\n" + text
            )
        except Exception as e:
            logger.error(f"search_group_member_messages 内部异常: {type(e).__name__}: {e}")
            return (
                f"按人查询执行出错：{type(e).__name__}: {e}（可让用户改用群号指定群重试）"
            )

    # ------------------------------------------------------------------ #
    # LLM 工具：读取好友私聊记录（私聊查私聊）                            #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="read_friend_recent_messages")
    async def read_friend_recent_messages(
        self, event: AstrMessageEvent, friend: str = "", count: str = ""
    ) -> str:
        """读取与某位好友的私聊（QQ聊天）记录，返回按时间排列的最近若干条消息。**此工具无需任何权限，群聊和私聊会话中都可以直接调用**（群聊里查别人与 Bot 的私聊记录也无需额外权限）。当用户说"我和XX私聊过什么""看看和某人的聊天记录""查一下某QQ的私聊""帮我看看和xx的聊天记录"等问题时使用。**如果其他工具（如查询 Bot 与用户互动的工具）因权限不足被拒绝，请优先改用本工具。**注意：本工具只读好友私聊记录，与群聊无关；查询某人**在群里的发言**请改用 search_group_member_messages，读取**群聊**整体记录请用 read_group_recent_messages。在私聊会话中不填 friend 时默认读取当前对话对象；要查指定好友，务必把 friend 填成对方的昵称、备注名或QQ号。

        Args:
            friend(string): 要查询的好友，昵称、备注名或QQ号均可，支持模糊匹配；不填时读取当前私聊会话对方的记录
            count(string): 最多返回的消息条数（数字字符串，例如 "30"），不填使用默认值
        """
        try:
            if str(count).strip():
                total = self._normalize_count(count)
            else:
                total = int(self._cfg("read.default_count", 20))
            text = await self._read_friend(event, friend, total)
            if not text:
                return (
                    f"未能读取到该好友的私聊记录（请确认协议端支持 get_friend_msg_history，"
                    f"或在私聊中不指定好友直接查询当前对话）。"
                )
            return (
                "（提示：以下记录仅供你参考。请自然地和用户讨论其中的内容，"
                "不要逐条复述，也不要一次性全部讲完。）\n" + text
            )
        except Exception as e:
            logger.error(f"read_friend_recent_messages 内部异常: {type(e).__name__}: {e}")
            return (
                f"私聊记录查询执行出错：{type(e).__name__}: {e}（可改用QQ号指定好友重试）"
            )

    # ------------------------------------------------------------------ #
    # LLM 工具：按昵称模糊搜人（不用先知道 QQ 号）                        #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="search_qq_user")
    async def search_qq_user(
        self, event: AstrMessageEvent, name: str = "", group_id: str = ""
    ) -> str:
        """按昵称模糊搜索群成员，拿到对方的 QQ 号。**此工具无需任何权限，群聊和私聊都能用**（私聊里要指定群号或群名）。当用户说"找一下某某"、"群里有个叫XX的"、"谁叫XX"、"帮我搜一下这个人"、"XX的QQ是多少"这类只知道昵称、不知道 QQ 号的问题时使用。返回候选列表（群名片、QQ号、身份），拿到 QQ 号后再用 get_user_profile 看详细资料。注意：本工具只搜**群成员**，查不认识的陌生人请让用户直接给 QQ 号。

        Args:
            name(string): 要搜的昵称、群名片或 QQ 号，支持模糊匹配（给一部分就行）
            group_id(string): 群号或群名称，支持模糊匹配；不填时群聊用当前群，私聊必须提供
        """
        if not self._cfg_bool("profile.profile_enabled", True):
            return "搜人功能已被管理员关闭。"
        try:
            hits, label, err = await self._search_members(event, name, group_id)
            if err:
                return err
            if not hits:
                return (
                    f"在群「{label}」里没找到匹配「{name}」的成员。"
                    "可以试试只给名字里的一两个字，或者让用户直接提供 QQ 号。"
                )
            lines = [f"在群「{label}」里找到 {len(hits)} 个匹配「{name}」的成员："]
            for i, m in enumerate(hits, 1):
                lines.append(f"{i}. {m['label']} (QQ: {m['user_id']}){m['extra']}")
            lines.append("要查看其中某人的完整资料，把对应 QQ 号传给 get_user_profile。")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"search_qq_user 异常: {type(e).__name__}: {e}")
            return f"搜人出错：{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ #
    # LLM 工具：查看个人资料（头像/昵称/QQ 号可选）                       #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="get_user_profile")
    async def get_user_profile(
        self, event: AstrMessageEvent, user: str = "", group_id: str = ""
    ) -> str:
        """查看某个 QQ 用户的资料：群名片、昵称、QQ号、群身份、等级、头衔、性别年龄地区，头像可选。**无需任何权限**，群聊和私聊都能用。当用户说"看看这个人的资料"、"XX是谁"、"他QQ多少"、"这人什么来头"、"查一下这个号"时使用。user 可以填 QQ 号，也可以填昵称（会先模糊搜索，命中多个会列出来让你确认）。不填 user 时查当前说话的人。头像是否输出、昵称和 QQ 号是否显示，由插件配置决定。

        Args:
            user(string): QQ 号，或昵称/群名片（支持模糊）；不填则查当前说话的人
            group_id(string): 群号或群名，查群内身份时用；不填时群聊用当前群
        """
        if not self._cfg_bool("profile.profile_enabled", True):
            return "用户资料功能已被管理员关闭。"

        target = str(user or "").strip()
        gid, group_label = "", ""
        try:
            if target and not target.isdigit():
                hits, group_label, err = await self._search_members(event, target, group_id)
                if err:
                    return err
                if not hits:
                    return (
                        f"没找到匹配「{target}」的群成员。请让用户确认昵称，"
                        "或者直接提供 QQ 号。"
                    )
                if len(hits) > 1:
                    lines = [f"「{target}」匹配到 {len(hits)} 个人，请先确认是哪一个："]
                    for i, m in enumerate(hits, 1):
                        lines.append(f"{i}. {m['label']} (QQ: {m['user_id']}){m['extra']}")
                    lines.append("确认后把 QQ 号再传给本工具。")
                    return "\n".join(lines)
                target = str(hits[0]["user_id"])
            elif not target:
                target = str(event.get_sender_id() or "")
            if not target:
                return "没拿到要查的人，请提供 QQ 号或昵称。"

            # 群内身份要靠群号查；私聊里查不到群名片，只能拿陌生人资料
            if group_id:
                gid, group_label, _err = await self._resolve_group(event, group_id)
            else:
                gid = event.get_group_id() or ""

            info = await self._fetch_profile(event, target, gid)
            if not info:
                return f"没查到 QQ {target} 的资料（协议端不返回或对方设置了隐私）。"

            text = self._render_profile(info)
            as_image = self._cfg_bool("profile.avatar_as_image", True)
            if self._cfg_bool("profile.show_avatar", True):
                url = f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640"
                if as_image:
                    sent = await self._send_avatar(event, url)
                    if sent:
                        text += "\n（头像图片已发送给用户）"
                    else:
                        text += f"\n头像：{url}"
                else:
                    text += f"\n头像：{url}"
            if self._cfg_bool("profile.show_background", True):
                bg = self._pick_background(info)
                if bg:
                    text += f"\n资料卡背景：{bg}"
                    if as_image:
                        await self._send_avatar(event, bg)
            return text
        except Exception as e:
            logger.error(f"get_user_profile 异常: {type(e).__name__}: {e}")
            return f"查资料出错：{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ #
    # LLM 工具：等一等，看有没有后续消息                                  #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="refresh_messages")
    async def refresh_messages(
        self, event: AstrMessageEvent, duration: str = ""
    ) -> str:
        """等几秒，看当前会话有没有新消息进来。当用户说"等一下"、"我还没说完"、"先别急"，或者你觉得对方话像没说完、可能在打字时使用。只返回等待期间新到的消息，不会重复已有内容。

        Args:
            duration(string): 等多少秒（数字字符串，例如 "8"），不填默认 8 秒，范围 1~30
        """
        try:
            wait = float(str(duration).strip() or "8")
        except (TypeError, ValueError):
            wait = 8.0
        wait = max(1.0, min(wait, 30.0))
        sid = event.get_session_id()
        cur_mid = str(getattr(event.message_obj, "message_id", "") or "")
        start = time.time()
        await asyncio.sleep(wait)
        self._cache_cleanup()
        cached = list(self._msg_cache.get(sid) or [])
        fresh = [
            m for m in cached
            if m["ts"] >= start and str(m.get("mid") or "") != cur_mid
        ]
        if not fresh:
            return f"等了 {wait:.0f} 秒，没有新消息进来。"
        lines = []
        for m in fresh:
            tag = "[我]" if m.get("is_bot") else f"{m.get('sender')}({m.get('sender_id')})"
            lines.append(f"[{self._clock(m['ts'])}] {tag}: {m.get('text', '')}")
        return f"等待 {wait:.0f} 秒后收到 {len(fresh)} 条新消息：\n" + "\n".join(lines)

    # ------------------------------------------------------------------ #
    # LLM 工具：戳一戳                                                    #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="poke_user")
    async def poke_user(self, event: AstrMessageEvent, user: str = "") -> str:
        """戳一戳某人（双击头像那个效果），群聊和私聊都能用。**无需任何权限**。当用户说"戳一下XX"、"拍拍他"、"戳戳我"、"叫醒他"、"别装死"时使用。群聊里用群戳、私聊用好友戳，插件自己判断。不填 user 时戳当前说话的人。

        Args:
            user(string): 要戳的人，昵称/群名片/QQ号都行，支持模糊；不填则戳当前说话的人
        """
        if not self._cfg_bool("interact.interact_enabled", True):
            return "互动功能已被管理员关闭。"
        if not self._cfg_bool("interact.poke_enabled", True):
            return "戳一戳已被管理员关闭。"
        try:
            qq, label, err = await self._resolve_target(event, user)
            if err:
                return err
            client = self._get_client(event)
            if client is None:
                return "当前平台不支持戳一戳（只支持 OneBot/aiocqhttp 协议端）。"
            gid = event.get_group_id() or ""
            if gid:
                res = await client.api.call_action(
                    "group_poke", group_id=int(gid), user_id=int(qq)
                )
            else:
                res = await client.api.call_action("friend_poke", user_id=int(qq))
            action = self._poke_action(res)
            return f"你{action}{label}" if action else f"戳了戳 {label}（{qq}）"
        except Exception as e:
            logger.error(f"poke_user 异常: {type(e).__name__}: {e}")
            return (
                f"戳一戳失败：{type(e).__name__}: {e}"
                "（协议端可能不支持 group_poke / friend_poke）"
            )

    # ------------------------------------------------------------------ #
    # LLM 工具：给资料卡点赞                                              #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="like_user")
    async def like_user(
        self, event: AstrMessageEvent, user: str = "", times: str = ""
    ) -> str:
        """给某人的**资料卡**点赞（不是给说说/动态点赞）。**无需任何权限**。当用户说"给XX点个赞"、"赞一下他"、"点满"、"帮我给他点赞"时使用。QQ 限制每人每天约 10 次，超了协议端会返回失败，不要连续重试。不填 user 时赞当前说话的人。

        Args:
            user(string): 要点赞的人，昵称/群名片/QQ号都行，支持模糊；不填则赞当前说话的人
            times(string): 点几次（数字字符串 1~10），不填用插件配置的次数
        """
        if not self._cfg_bool("interact.interact_enabled", True):
            return "互动功能已被管理员关闭。"
        if not self._cfg_bool("interact.like_enabled", True):
            return "点赞已被管理员关闭。"
        try:
            try:
                n = int(str(times).strip())
            except (TypeError, ValueError):
                n = self._cfg_int("interact.like_times", 10)
            n = max(1, min(n, 10))
            qq, label, err = await self._resolve_target(event, user)
            if err:
                return err
            client = self._get_client(event)
            if client is None:
                return "当前平台不支持点赞（只支持 OneBot/aiocqhttp 协议端）。"
            await client.api.call_action("send_like", user_id=int(qq), times=n)
            return f"给 {label}（{qq}）点了 {n} 个赞。"
        except Exception as e:
            logger.error(f"like_user 异常: {type(e).__name__}: {e}")
            return (
                f"点赞失败：{type(e).__name__}: {e}"
                "（可能今天次数用完了，QQ 限制每人每天约 10 次）"
            )

    # ------------------------------------------------------------------ #
    # LLM 工具：撤回自己发的消息                                          #
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="recall_message")
    async def recall_message(self, event: AstrMessageEvent, count: str = "") -> str:
        """撤回**你自己（机器人）刚刚发出去**的消息。当用户说"撤回"、"收回"、"刚才那句删掉"、"别说了"、"说错了重说"、"把上面那几条撤了"时使用。只能撤自己的消息，撤不了别人的。QQ 限制发出去 2 分钟内才能撤，超时会失败，失败后不要反复重试。长回复被自动分成多段发送时，撤 1 条只撤最后一段，要整串撤掉就把 count 写大。

        Args:
            count(string): 撤回最近几条，默认 1，最多到插件配置的上限
        """
        if not self._cfg_bool("interact.interact_enabled", True):
            return "互动功能已被管理员关闭。"
        if not self._cfg_bool("interact.recall_enabled", True):
            return "撤回功能已被管理员关闭。"
        client = self._get_client(event)
        if client is None:
            return "当前平台不支持撤回（只支持 OneBot/aiocqhttp 协议端）。"
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return "拿不到机器人自己的 QQ 号，没法判断哪些消息是自己发的。"
        try:
            n = int(str(count).strip())
        except (TypeError, ValueError):
            n = 1
        limit = max(1, self._cfg_int("interact.recall_max_count", 5))
        n = max(1, min(n, limit))
        window = self._cfg_int("interact.recall_window_seconds", 120)
        try:
            mids = await self._find_own_message_ids(event, self_id, n, window)
        except Exception as e:
            logger.error(f"recall_message 查找消息失败: {type(e).__name__}: {e}")
            return f"撤回失败：读不到最近的聊天记录（{type(e).__name__}: {e}）。"
        if not mids:
            span = f"最近 {window} 秒内" if window > 0 else "最近的记录里"
            return (
                f"{span}没找到你自己发的消息，没什么可撤的。"
                "（如果你刚才确实说过话，可能是协议端的历史接口不返回机器人自己的消息）"
            )
        ok = 0
        failed = ""
        for mid in mids:
            try:
                arg = int(mid) if str(mid).isdigit() else mid
                await client.api.call_action("delete_msg", message_id=arg)
                ok += 1
            except Exception as e:
                failed = f"{type(e).__name__}: {e}"
                break
        if ok == 0:
            return (
                f"撤回失败：{failed}。"
                "常见原因：超过 2 分钟（QQ 限制）、协议端不支持 delete_msg、"
                "或消息 ID 不是自己的。别再重试了。"
            )
        msg = f"已撤回 {ok} 条自己发的消息。"
        if failed:
            msg += f"后面那条撤不掉（{failed}），可能超过 2 分钟了。"
        return msg

    async def _find_own_message_ids(
        self, event: AstrMessageEvent, self_id: str, want: int, window: int
    ) -> list[str]:
        """从协议端历史里挑出机器人自己发的最近 N 条 message_id（新的在前）。

        为什么不用本地缓存：after_message_sent 拿不到出站 message_id，
        缓存里 bot 记录的 id 是用户消息的（见 _cache_put），拿它撤会撤错人。
        """
        client = self._get_client(event)
        if client is None:
            return []
        gid = event.get_group_id() or ""
        count = max(50, want * 10)
        msgs: list = []
        if gid:
            ret = await client.api.call_action(
                "get_group_msg_history", group_id=int(gid), count=count
            )
        else:
            peer = str(event.get_sender_id() or "")
            if not peer:
                return []
            ret = await client.api.call_action(
                "get_friend_msg_history", user_id=int(peer), count=count
            )
        msgs = self._extract_messages(ret)
        now = time.time()
        own: list[tuple[float, str]] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            sender = m.get("sender") or {}
            uid = str(sender.get("user_id") or m.get("user_id") or "")
            mid = str(m.get("message_id") or "")
            if uid != self_id or not mid:
                continue
            ts = m.get("time") or 0
            if window > 0 and ts:
                try:
                    if now - float(ts) > window:
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                tsf = float(ts or 0)
            except (TypeError, ValueError):
                tsf = 0.0
            own.append((tsf, mid))
        own.sort(key=lambda x: x[0], reverse=True)
        return [mid for _, mid in own[:want]]

    # ------------------------------------------------------------------ #
    # 跨会话转述（原 astrbot_plugin_xrelay 融合进来）                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _relay_gc(store: dict, window: float, now: float) -> None:
        for stale in [k for k, t in store.items() if now - t > window]:
            store.pop(stale, None)

    def _relay_check_content_dup(self, key: str) -> bool:
        window = float(self._cfg("relay.dedup_window_seconds", 600) or 600)
        now = time.time()
        self._relay_gc(self._relay_sent, window, now)
        return key in self._relay_sent

    def _relay_check_cooldown(self, event: AstrMessageEvent, target: str) -> bool:
        cooldown = float(self._cfg("relay.target_cooldown_seconds", 120) or 0)
        if cooldown <= 0:
            return False
        now = time.time()
        self._relay_gc(self._relay_target_ts, max(cooldown * 4, 3600), now)
        slot = f"{event.unified_msg_origin}|{target}"
        last = self._relay_target_ts.get(slot)
        return last is not None and now - last < cooldown

    def _relay_mark_sent(self, event: AstrMessageEvent, target: str, key: str) -> None:
        now = time.time()
        self._relay_sent[key] = now
        self._relay_target_ts[f"{event.unified_msg_origin}|{target}"] = now

    _JUNK_STRONG_RE = re.compile(r"忽略|误发|误触|ignore", re.IGNORECASE)
    _JUNK_ONLY_RE = re.compile(
        r"^[\s（(【\[](?:测试|test|ignore|忽略|误发)[\s）)】\]]*[.。!！~]*$",
        re.IGNORECASE,
    )
    _PLACEHOLDER_WRAP_RE = re.compile(r"^[\s（(【\[].+[\s）)】\]][.。!！~]*$", re.DOTALL)
    _JUNK_WEAK_RE = re.compile(
        r"测试消息|测试一下|结束标记|开始标记|test\s*message|placeholder|占位",
        re.IGNORECASE,
    )

    @classmethod
    def _is_junk_message(cls, message: str) -> bool:
        if cls._JUNK_STRONG_RE.search(message):
            return True
        if cls._JUNK_ONLY_RE.match(message):
            return True
        return bool(
            len(message) <= 30
            and cls._PLACEHOLDER_WRAP_RE.match(message)
            and cls._JUNK_WEAK_RE.search(message)
        )

    @staticmethod
    def _parse_segments(raw, limit: int) -> list:
        """把分段参数拆成列表。||| 优先，其次按换行拆——两种都得认。"""
        text = str(raw or "").strip()
        if not text:
            return []
        parts = text.split("|||") if "|||" in text else text.splitlines()
        out = [str(p).strip() for p in parts if str(p).strip()]
        return out[:max(1, int(limit))]

    @filter.llm_tool(name="relay_message")
    async def relay_message(
        self, event: AstrMessageEvent, target: str, is_group: bool,
        message: str = "", segments: str = "",
    ):
        '''跨会话转述：把一条话带给其他 QQ 用户或群。

        两种用法二选一：
        - 转述一段完整的话 → 填 message，单条发出；
        - 用户要求"分段发""连发几条""一句一句发""像聊天那样刷过去" → 填 segments，
          用 ||| 分隔各段（也用换行），插件会按间隔一条条发出去，像对方在连发。
          条数上限由配置决定（默认 5）。

        ⚠️ 严格使用限制（必须遵守）：
        1. 当前会话的回复直接输出文本即可，绝不要调用本工具；本工具只用于"带话给不在场的人"。
        2. 仅当用户明确要求转述/带话/告知某个其他会话时才允许调用，一次请求最多调用一次。
        3. 严禁发送"（请忽略）""测试消息""误发"之类的无意义占位消息给目标。
        4. 同一目标有冷却时间，冷却期内再次调用会被拒绝且不会发送，换措辞也没用。
           分段连发是**一次调用**完成的，不要把"分段"拆成多次调用来绕冷却——
           第二次开始全会被拒，还会浪费一次机会。
        5. 发送成功后不要重发、不要二次确认，也不要向当前用户复述转述正文，只给一句简短回执。

        Args:
            target(string): 目标 QQ 号或群号
            is_group(boolean): 目标是否为群聊，true 为群，false 为私聊用户
            message(string): 要转述的完整文本（单条模式；与 segments 二选一）
            segments(string): 分段模式：要连发的多段文本，用 ||| 分隔（换行也行）
        '''
        if not event.is_admin() and not self._cfg_bool("relay.allow_members", False):
            return "权限拒绝：跨会话转述仅限主人使用。"
        target = str(target or "").strip()
        if not target:
            return "参数错误：target 不能为空。"

        allow_seg = self._cfg_bool("relay.allow_segments", True)
        max_seg = max(1, min(self._cfg_int("relay.max_segments", 5), 10))
        parts = self._parse_segments(segments, max_seg)
        if parts and not allow_seg:
            return "拦截：分段连发已被管理员关闭，请改用 message 发一条完整消息。"
        if not parts and not str(message or "").strip():
            return "参数错误：message 和 segments 至少填一个。"

        # 去重按「所有分段拼起来」算：同一组分段重复调用同样会被拦，
        # 不会因为拆成多条就绕过内容去重。
        body = "\n".join(parts) if parts else str(message).strip()
        if self._is_junk_message(body):
            logger.info("[QQTools·Relay] 废消息已拦截: target=%s msg=%r", target, body[:40])
            return (
                "拦截：检测到无意义占位消息（如\"请忽略/测试/误发\"），已拒绝且不会发送。"
                "当前没有真实转述任务，请直接用文本回复当前用户，不要再调用本工具。"
            )
        cooldown = float(self._cfg("relay.target_cooldown_seconds", 120) or 120)
        key = hashlib.sha256(
            f"{event.unified_msg_origin}|{target}|{body}".encode("utf-8")
        ).hexdigest()
        if self._relay_check_content_dup(key):
            logger.info("[QQTools·Relay] 内容重复已拦截: target=%s", target)
            return "拦截：相同内容刚已发送过，本次已拒绝，不要再重试。"
        if self._relay_check_cooldown(event, target):
            logger.info("[QQTools·Relay] 目标冷却中已拦截: target=%s", target)
            return (
                f"拦截：刚刚已向该目标发送过消息（冷却 {int(cooldown)} 秒），"
                "本次已拒绝且不会发送。转述内容请等用户下一次明确要求时再说。"
            )

        session = MessageSession(
            platform_name=event.get_platform_id(),
            message_type=MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE,
            session_id=target,
        )
        # 给记忆插件的显式标记：这次发送是「用户要求的转述」，不是插件自作主张发的。
        # 壳方案分不清转述和定时推送/自发通知，语义在业务动作处打标才准。
        # cause 直接用本轮用户原话，比记忆插件往回翻 12 条猜要精确。
        relay_tag = {
            "cause": str(getattr(event, "message_str", "") or "").strip()[:80],
            "source": str(getattr(event, "unified_msg_origin", "") or ""),
        }

        def _chain(text: str) -> MessageChain:
            c = MessageChain(chain=[Plain(text)])
            c._sugarmemory_relay = relay_tag
            return c

        if not parts:
            # ---- 单条模式 ----
            try:
                result = await self.context.send_message(
                    session=session,
                    message_chain=_chain(str(message).strip()),
                )
                if result is False:
                    return "发送失败：平台未找到或目标不存在，请检查 target 是否正确。"
                self._relay_mark_sent(event, target, key)
                return f"已发送给{'群' if is_group else '用户'} {target}。到此为止，不要重复发送。"
            except Exception as exc:
                logger.error("[QQTools·Relay] 发送失败: %s", exc)
                return f"发送失败：{exc}"

        # ---- 分段模式：一次调用内部连发，冷却只消耗一次 ----
        delay = max(0, self._cfg_int("relay.segment_delay_ms", 900)) / 1000.0
        sent = 0
        for idx, seg in enumerate(parts):
            try:
                result = await self.context.send_message(
                    session=session,
                    message_chain=_chain(seg),
                )
                if result is False:
                    logger.warning("[QQTools·Relay] 分段 %d/%d 发送返回 False", idx + 1, len(parts))
                    break
                sent += 1
                logger.info("[QQTools·Relay] 分段 %d/%d 已发: %r", idx + 1, len(parts), seg[:30])
            except Exception as exc:
                logger.error("[QQTools·Relay] 分段 %d/%d 发送失败: %s", idx + 1, len(parts), exc)
                break
            if idx < len(parts) - 1 and delay > 0:
                await asyncio.sleep(delay)

        if not sent:
            return "发送失败：一条都没发出去，请检查 target 是否正确。"
        # 只要发出去至少一条就记冷却，避免模型反复重试把对方刷屏
        self._relay_mark_sent(event, target, key)
        tail = "" if sent == len(parts) else f"（{len(parts)} 段里只有 {sent} 段成功）"
        return (
            f"已分 {sent} 段连发给{'群' if is_group else '用户'} {target}{tail}。"
            "到此为止，不要重复发送，也不要再调本工具补发。"
        )

    @filter.llm_tool(name="list_pending_requests")
    async def list_pending_requests(self, event: AstrMessageEvent, kind: str = "") -> str:
        '''列出还没处理的加群申请和好友申请，每条带编号、申请人、留言和 flag。

        用户问"有没有人申请加群""谁加我好友""待处理的申请"时用。处理申请必须先调
        这个拿到编号或 flag——flag 只在事件里出现一次，不攒起来就永远找不回来。

        Args:
            kind(string): 只看哪一类：group（加群）/ friend（好友）/ 留空或不认识的值就看全部
        '''
        if self._cfg_bool("requests.owner_only", True) and not event.is_admin():
            return "权限拒绝：查看待处理申请仅限主人使用。"
        kind = str(kind or "").strip().lower()
        if kind not in ("group", "friend"):
            kind = ""
        rows = self._request_rows(kind)
        # 软申请同步：列表阶段就尝试从 NapCat 可疑申请列表补真实 flag，
        # 拿到的条目直接升级成正常申请（后续 handle 走标准流程）。
        client = self._get_client(event)
        if client is not None and any(
                v.get("soft") or str(f).startswith(("soft:", "noflag:"))
                for f, v in rows):
            done_uids = set()
            upgraded = 0
            for f, v in rows:
                uid = str(v.get("user_id") or "").strip()
                if not uid or uid in done_uids:
                    continue
                done_uids.add(uid)
                doubt_flag = await self._resolve_doubt_flag(client, uid)
                if doubt_flag:
                    self._pending_requests.pop(f, None)
                    self._pending_requests[doubt_flag] = {
                        "kind": v.get("kind") or "friend",
                        "sub_type": v.get("sub_type") or "",
                        "user_id": uid,
                        "nickname": v.get("nickname") or "",
                        "group_id": v.get("group_id") or "",
                        "comment": v.get("comment") or "（可疑好友申请列表）",
                        "ts": float(v.get("ts") or time.time()),
                    }
                    upgraded += 1
                    logger.info(f"[QQTools·请求] 软申请升级为真flag: user={uid} "
                                f"flag={doubt_flag[:16]}…")
            if upgraded:
                self._save_pending_requests()
                rows = self._request_rows(kind)
        if not rows:
            if self._cfg_bool("requests.requests_enabled", True):
                return ("当前没有待处理的申请。注意：机器人重启后这个列表会清空，"
                        "重启前收到的申请就没法处理了，只能让对方重新申请。")
            return "待处理申请功能已关闭（requests.requests_enabled=false）。"
        lines = [self._fmt_request(i, f, v) for i, (f, v) in enumerate(rows, 1)]
        tip = ("\n\n处理时给 handle_request 传序号（如 \"1\"）或 QQ 号都行，"
               "不用抄那串长 flag。")
        return "待处理申请：\n" + "\n".join(lines) + tip

    @filter.llm_tool(name="handle_request")
    async def handle_request(
        self, event: AstrMessageEvent, flag: str, approve: bool, reason: str = ""
    ):
        '''同意或拒绝一条加群申请／好友申请。

        先调 list_pending_requests 看有哪些待处理，再把那里的编号（"1"、"2"）或
        申请人 QQ 号传进来；直接传 flag 也行。插件会自动分辨是群还是好友申请。

        ⚠️ 这是不可逆操作，会真的把人放进来或拒之门外：
        - 用户没明说同意谁，就不要擅自同意，先问一句；
        - 拒绝时 reason 会被对方看到，写清楚理由，别留空着伤人的话；
        - 处理完不要重复调用，同一条 flag 处理两次会报错。

        Args:
            flag(string): 待处理申请的编号（"1"）、申请人 QQ 号，或 list_pending_requests 给的 flag 原文
            approve(boolean): true 同意，false 拒绝
            reason(string): 拒绝理由，会推给对方看（同意时不用填）
        '''
        if self._cfg_bool("requests.owner_only", True) and not event.is_admin():
            return "权限拒绝：处理加群/好友申请仅限主人使用。"
        if not self._cfg_bool("requests.requests_enabled", True):
            return "申请处理功能已关闭。"
        key = str(flag or "").strip()
        real_flag, info = self._find_request(key)
        if not real_flag:
            rows = self._request_rows()
            n = len(rows)
            return (f"没找到编号/QQ/flag 为「{key}」的待处理申请。"
                    f"{f'当前有 {n} 条待处理，' if n else '当前没有待处理的申请，'}"
                    "先调 list_pending_requests 看一下。")
        who = info.get("nickname") or info.get("user_id") or real_flag[:12]
        client = self._get_client(event)
        if client is None:
            return "当前平台不是 aiocqhttp（OneBot 协议端），无法处理申请。"
        if info.get("soft") or str(real_flag).startswith(("soft:", "noflag:")):
            # 软申请 / 无 flag 申请：协议端没推 request 事件。先试试 NapCat 的
            # 可疑好友申请接口（QQ 把「允许任何人临时会话」下发的申请归入该列表，
            # 拉得到真实 flag，能直接代处理）。
            uid = str(info.get("user_id") or "").strip()
            doubt_flag = await self._resolve_doubt_flag(client, uid) if uid else None
            if doubt_flag:
                try:
                    await client.api.call_action(
                        "set_doubt_friends_add_request",
                        flag=doubt_flag,
                        approve=bool(approve),
                    )
                except Exception as e:
                    logger.error(f"[QQTools·请求] 可疑申请处理失败: {type(e).__name__}: {e}")
                    return (f"在可疑申请列表里找到了 {who} 的真实 flag，但处理失败："
                            f"{type(e).__name__}: {e}。别反复重试，让主人在 QQ 客户端"
                            "「联系人-新朋友」里手动处理。")
                self._pending_requests.pop(real_flag, None)
                self._save_pending_requests()
                logger.info(f"[QQTools·请求] 已通过可疑申请通道{'同意' if approve else '拒绝'}: "
                            f"user={uid} flag={doubt_flag[:16]}…")
                return (f"已{'同意' if approve else '拒绝'} {who} 的好友申请"
                        + ("（已加到好友列表）" if approve else "")
                        + "。走的是可疑申请通道。到此为止，别再调一次。")
            return (f"这一条（{who}）没有 flag，可疑申请列表里也找不到 TA，机器人无法"
                    "代为同意或拒绝。让主人在 QQ 客户端的「联系人-新朋友」里手动处理，"
                    "或者让对方删了重新加一次。别再对这条调本工具。")
        try:
            if info.get("kind") == "group":
                await client.api.call_action(
                    "set_group_add_request",
                    flag=real_flag,
                    sub_type=info.get("sub_type") or "add",
                    approve=bool(approve),
                    reason="" if approve else (reason or ""),
                )
            else:
                await client.api.call_action(
                    "set_friend_add_request",
                    flag=real_flag,
                    approve=bool(approve),
                    remark="" if not approve else (reason or "")[:60],
                )
        except Exception as e:
            logger.error(f"[QQTools·请求] 处理申请失败: {type(e).__name__}: {e}")
            return (f"处理失败：{type(e).__name__}: {e}。"
                    "常见原因：flag 过期（QQ 那边一般就几分钟~几小时）、已经处理过了、"
                    "或协议端不支持。别反复重试。")
        # 只有成功才弹出缓存：失败还能再试一次
        self._pending_requests.pop(real_flag, None)
        self._save_pending_requests()
        if info.get("kind") == "group":
            gid = info.get("group_id") or "?"
            return (f"已{'同意' if approve else '拒绝'}{who} 对群 {gid} 的申请。"
                    + ("" if approve else f"理由：{reason or '（未填）'}")) + "到此为止，别再调一次。"
        return (f"已{'同意' if approve else '拒绝'}{who} 的好友申请。"
                + ("（已加到好友列表）" if approve else "")) + "到此为止，别再调一次。"

    @filter.llm_tool(name="send_friend_request")
    async def send_friend_request(
        self, event: AstrMessageEvent, user_id: str, message: str = ""
    ):
        '''主动向某个 QQ 号发送好友申请（不是处理别人的申请）。

        注意这不在 OneBot v11 标准接口里，能不能成功取决于协议端版本。NapCat
        较新的版本才有；不支持时会明确告诉你失败，别反复重试。

        Args:
            user_id(string): 要加的 QQ 号
            message(string): 验证留言，会推给对方看（可不填）
        '''
        if not event.is_admin():
            return "权限拒绝：主动加好友仅限主人使用。"
        if not self._cfg_bool("requests.allow_send_friend_request", True):
            return "主动加好友已关闭（requests.allow_send_friend_request=false）。"
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return "参数错误：user_id 必须是纯数字 QQ 号。"
        # 参数校验放前面：QQ 号都不对就没必要先去找协议端，也免得真正的错误
        # 被「当前平台不支持」这句盖掉。
        client = self._get_client(event)
        if client is None:
            return "当前平台不是 aiocqhttp（OneBot 协议端），无法发送好友申请。"
        # 各协议端给这个能力起的名不统一，挨个试，都失败就如实说
        last_err = ""
        for action in ("add_friend", "friend_add", "ArkFriendAdd"):
            try:
                await client.api.call_action(action, user_id=int(uid), message=message or "")
                logger.info(f"[QQTools·请求] 已发送好友申请 action={action} uid={uid}")
                return (f"已向 {uid} 发送好友申请"
                        + (f"（留言：{message}）" if message else "")
                        + f"。用的是协议端的 {action} 接口。到此为止，别重复发送。")
            except Exception as e:
                last_err = f"{action}: {type(e).__name__}: {e}"
                continue
        logger.warning(f"[QQTools·请求] 主动加好友全部失败: {last_err}")
        return (f"发送失败，当前协议端没有可用的主动加好友接口（试过 add_friend / "
                f"friend_add / ArkFriendAdd）。最后错误：{last_err}。"
                "可以换个支持该接口的协议端版本，或手动去加。")

    # ------------------------------------------------------------------ #
    # 手动指令：/申请 [群/好友]                                            #
    # ------------------------------------------------------------------ #
    @filter.command("申请")
    async def requests_command(self, event: AstrMessageEvent):
        """列出待处理的加群/好友申请，例如 /申请、/申请 好友"""
        parts = event.message_str.strip().split()
        kind = parts[1] if len(parts) > 1 else ""
        if kind in ("好友", "friend"):
            kind = "friend"
        elif kind in ("群", "group"):
            kind = "group"
        else:
            kind = ""
        yield event.plain_result(await self.list_pending_requests(event, kind))

    # ------------------------------------------------------------------ #
    # 手动测试指令：/撤回 [条数]                                          #
    # ------------------------------------------------------------------ #
    @filter.command("撤回")
    async def recall_command(self, event: AstrMessageEvent):
        """手动撤回机器人自己刚发的消息（测试用）"""
        parts = event.message_str.strip().split()
        arg = parts[1] if len(parts) > 1 else "1"
        yield event.plain_result(
            await self.recall_message(event, arg)
        )

    # ------------------------------------------------------------------ #
    # 手动测试指令：/最近消息 [条数]                                      #
    # ------------------------------------------------------------------ #
    @filter.command("最近消息")
    async def recent_messages_command(self, event: AstrMessageEvent):
        """读取当前群最近的消息（手动测试工具的数据源）"""
        gid = event.get_group_id() or ""
        if not gid:
            yield event.plain_result("当前不是群聊会话，无法读取群消息。")
            return
        parts = event.message_str.strip().split()
        count = self._normalize_count(parts[1] if len(parts) > 1 else "")
        text = await self._read_recent(event, gid, count)
        if not text:
            yield event.plain_result(
                "未能读取到群聊消息：协议端不支持 get_group_msg_history 接口，"
                "且无可用会话历史。"
            )
            return
        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 群简报指令：/群简报 [条数] [群号]                                    #
    # ------------------------------------------------------------------ #
    @filter.command("群简报")
    async def group_brief_command(self, event: AstrMessageEvent):
        """读取某群最近消息，并调用 LLM 生成第三人称转述总结（谁谁谁干了什么、聊了什么）"""
        parts = event.message_str.strip().split()
        count = self._normalize_count(parts[1] if len(parts) > 1 else "50")
        gid = parts[2] if len(parts) > 2 else (event.get_group_id() or "")
        if not gid:
            yield event.plain_result(
                "用法：/群简报 [条数] [群号]。\n在本群内可直接 /群简报 50；"
                "在私聊里想转述别的群，可写 /群简报 50 群号。"
            )
            return
        records = await self._read_recent(event, gid, count)
        if not records:
            yield event.plain_result(f"未能读取到群 {gid} 的最近消息（协议端不支持或无可回退记录）。")
            return
        summary = await self._llm_summarize(event, gid, count, records)
        if not summary:
            err = getattr(self, "_brief_last_error", "") or "未知原因"
            yield event.plain_result(
                "LLM 总结失败（原因：" + err + "）。\n"
                "可在插件配置中填写 brief_api_base / brief_api_key / brief_model "
                "使用独立 API 生成简报。\n以下是原始记录：\n" + records
            )
            return
        yield event.plain_result(summary)

    # ------------------------------------------------------------------ #
    # 查人指令：/查人 [名称] [群号/群名]                                  #
    # ------------------------------------------------------------------ #
    @filter.command("查人")
    async def search_member_command(self, event: AstrMessageEvent):
        """按名称/QQ号模糊查询某群成员的最近发言（支持私聊指定群；默认由 LLM 加工回复，可用配置关闭）"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/查人 [名称] [群号或群名]。\n例如：/查人 正小豆；"
                "私聊里可写 /查人 正小豆 群名"
            )
            return
        name = parts[1]
        group_query = parts[2] if len(parts) > 2 else ""
        max_hits = int(self._cfg("search.search_max_hits", 20))
        text = await self._search_member(event, name, group_query, max_hits)
        if not text:
            yield event.plain_result(
                f"未找到与“{name}”匹配的发言记录（请确认名称，或在私聊时提供群号/群名）。"
            )
            return
        if self._cfg("output.llm_output", True):
            reply = await self._llm_generate_any(
                event,
                f"你刚读完了群聊记录（用户发起的指令：{event.message_str}）。"
                f"记录内容如下：\n{text}\n\n"
                "请像朋友一样，基于这些记录跟用户自然地聊起来：可以说说你注意到了什么、"
                "谁干了什么有意思的事、有哪些值得聊的点或疑问。要求：不要输出结构化总结"
                "或列表，不要一次性把所有内容都讲完，只挑最值得聊的一两个点，用简短自然"
                "的口吻说，像日常对话那样，给用户留出继续追问的空间。"
                "同时注意适当分段换行（每段两三句），让消息好读，不要一大段连在一起。",
            )
            if reply:
                yield event.plain_result(reply)
                return
        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 私聊记录指令：/私聊记录 [条数] [好友名/QQ]                         #
    # ------------------------------------------------------------------ #
    @filter.command("私聊记录")
    async def friend_history_command(self, event: AstrMessageEvent):
        """读取与某好友的私聊记录（私聊中不写好友则查当前对话对象；默认由 LLM 加工回复，可用配置关闭）"""
        parts = event.message_str.strip().split()
        count = self._normalize_count(parts[1] if len(parts) > 1 else "")
        friend = parts[2] if len(parts) > 2 else ""
        text = await self._read_friend(event, friend, count)
        if not text:
            yield event.plain_result(
                f"未能读取到该好友的私聊记录（请确认协议端支持 get_friend_msg_history，"
                f"或在私聊中不指定好友直接查询当前对话）。"
            )
            return
        if self._cfg("output.llm_output", True):
            reply = await self._llm_generate_any(
                event,
                f"你刚读完了私聊记录（用户发起的指令：{event.message_str}）。"
                f"记录内容如下：\n{text}\n\n"
                "请像朋友一样，基于这些记录跟用户自然地聊起来：可以说说你注意到了什么、"
                "对方聊了什么有意思的事、有哪些值得聊的点或疑问。要求：不要输出结构化总结"
                "或列表，不要一次性把所有内容都讲完，只挑最值得聊的一两个点，用简短自然"
                "的口吻说，像日常对话那样，给用户留出继续追问的空间。"
                "同时注意适当分段换行（每段两三句），让消息好读，不要一大段连在一起。",
            )
            if reply:
                yield event.plain_result(reply)
                return
        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 搜人指令：/搜人 [昵称或QQ] [群号/群名]                              #
    # ------------------------------------------------------------------ #
    @filter.command("搜人")
    async def search_user_command(self, event: AstrMessageEvent):
        """按昵称模糊搜索群成员，列出候选人和 QQ 号"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/搜人 [昵称或QQ] [群号或群名]\n"
                "例如：/搜人 小豆　　或　　/搜人 小豆 测试群\n"
                "昵称给一部分就行，多个匹配会全部列出来。"
            )
            return
        name = parts[1]
        group_query = parts[2] if len(parts) > 2 else ""
        hits, label, err = await self._search_members(event, name, group_query)
        if err:
            yield event.plain_result(err)
            return
        if not hits:
            yield event.plain_result(f"在群「{label}」里没找到匹配「{name}」的成员。")
            return
        lines = [f"群「{label}」里匹配「{name}」的成员（{len(hits)} 个）："]
        for i, m in enumerate(hits, 1):
            lines.append(f"{i}. {m['label']} (QQ: {m['user_id']}){m['extra']}")
        lines.append("看某人的资料卡：/资料 QQ号")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 资料指令：/资料 [昵称或QQ] [群号/群名]                              #
    # ------------------------------------------------------------------ #
    @filter.command("资料")
    async def profile_command(self, event: AstrMessageEvent):
        """查看某人的资料卡；头像按配置决定发图片还是只给链接"""
        if not self._cfg_bool("profile.profile_enabled", True):
            yield event.plain_result("用户资料功能已被管理员关闭。")
            return
        parts = event.message_str.strip().split()
        target = parts[1] if len(parts) > 1 else ""
        group_query = parts[2] if len(parts) > 2 else ""
        if not target:
            target = str(event.get_sender_id() or "")

        if target and not target.isdigit():
            hits, _label, err = await self._search_members(event, target, group_query)
            if err:
                yield event.plain_result(err)
                return
            if not hits:
                yield event.plain_result(f"没找到匹配「{target}」的群成员。")
                return
            if len(hits) > 1:
                lines = [f"「{target}」匹配到 {len(hits)} 个人，要查哪一个？"]
                for i, m in enumerate(hits, 1):
                    lines.append(f"{i}. {m['label']} (QQ: {m['user_id']}){m['extra']}")
                lines.append("用法：/资料 QQ号")
                yield event.plain_result("\n".join(lines))
                return
            target = str(hits[0]["user_id"])

        gid = event.get_group_id() or ""
        if group_query:
            resolved, _lbl, _err = await self._resolve_group(event, group_query)
            if resolved:
                gid = resolved

        info = await self._fetch_profile(event, target, gid)
        if not info:
            yield event.plain_result(f"没查到 QQ {target} 的资料（协议端没返回或对方设置了隐私）。")
            return
        yield event.plain_result(self._render_profile(info))
        as_image = self._cfg_bool("profile.avatar_as_image", True)
        if self._cfg_bool("profile.show_avatar", True) and as_image:
            try:
                yield event.chain_result(
                    [Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640")]
                )
            except Exception as e:
                logger.warning(f"发送头像失败: {type(e).__name__}: {e}")
        if self._cfg_bool("profile.show_background", True):
            bg = self._pick_background(info)
            if bg and as_image:
                try:
                    yield event.chain_result([Image.fromURL(bg)])
                except Exception as e:
                    logger.warning(f"发送背景图失败: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ #
    # 戳一戳指令：/戳 [昵称或QQ]                                          #
    # ------------------------------------------------------------------ #
    @filter.command("戳")
    async def poke_command(self, event: AstrMessageEvent):
        """戳一戳某人；不写人就戳自己"""
        if not self._cfg_bool("interact.interact_enabled", True) or not self._cfg_bool(
            "interact.poke_enabled", True
        ):
            yield event.plain_result("戳一戳功能已关闭。")
            return
        parts = event.message_str.strip().split()
        target = parts[1] if len(parts) > 1 else ""
        qq, label, err = await self._resolve_target(event, target)
        if err:
            yield event.plain_result(err)
            return
        client = self._get_client(event)
        if client is None:
            yield event.plain_result("当前平台不支持戳一戳（只支持 OneBot/aiocqhttp 协议端）。")
            return
        try:
            gid = event.get_group_id() or ""
            if gid:
                res = await client.api.call_action(
                    "group_poke", group_id=int(gid), user_id=int(qq)
                )
            else:
                res = await client.api.call_action("friend_poke", user_id=int(qq))
            action = self._poke_action(res)
            yield event.plain_result(f"你{action}{label}" if action else f"戳了戳 {label}（{qq}）")
        except Exception as e:
            logger.error(f"poke_command 异常: {type(e).__name__}: {e}")
            yield event.plain_result(f"戳一戳失败：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ #
    # 点赞指令：/点赞 [昵称或QQ] [次数]                                   #
    # ------------------------------------------------------------------ #
    @filter.command("点赞")
    async def like_command(self, event: AstrMessageEvent):
        """给某人的资料卡点赞；次数 1~10"""
        if not self._cfg_bool("interact.interact_enabled", True) or not self._cfg_bool(
            "interact.like_enabled", True
        ):
            yield event.plain_result("点赞功能已关闭。")
            return
        parts = event.message_str.strip().split()
        target = parts[1] if len(parts) > 1 else ""
        times_raw = parts[2] if len(parts) > 2 else ""
        try:
            n = int(times_raw)
        except (TypeError, ValueError):
            n = self._cfg_int("interact.like_times", 10)
        n = max(1, min(n, 10))

        qq, label, err = await self._resolve_target(event, target)
        if err:
            yield event.plain_result(err)
            return
        client = self._get_client(event)
        if client is None:
            yield event.plain_result("当前平台不支持点赞（只支持 OneBot/aiocqhttp 协议端）。")
            return
        try:
            await client.api.call_action("send_like", user_id=int(qq), times=n)
            yield event.plain_result(f"给 {label}（{qq}）点了 {n} 个赞。")
        except Exception as e:
            logger.error(f"like_command 异常: {type(e).__name__}: {e}")
            yield event.plain_result(
                f"点赞失败：{type(e).__name__}: {e}（可能今天次数用完了）"
            )

    # ------------------------------------------------------------------ #
    # 自检指令：/工具自检                                                 #
    # ------------------------------------------------------------------ #
    @filter.command("工具自检")
    async def tool_self_check(self, event: AstrMessageEvent):
        """检查本插件注册的工具是否已进入 AstrBot 的工具管理器（排查 AI 无法调用工具的硬证据）"""
        try:
            mgr = self.context.get_llm_tool_manager()
        except Exception:
            mgr = None
        if mgr is None:
            try:
                mgr = self.context.provider_manager.llm_tools
            except Exception:
                mgr = None
        if mgr is None:
            yield event.plain_result(
                "工具自检：无法获取工具管理器（AstrBot 版本差异，建议升级到 >=4.5.7）。"
            )
            return

        wanted = [
            "read_group_recent_messages",
            "search_group_member_messages",
            "read_friend_recent_messages",
            "refresh_messages",
            "search_qq_user",
            "get_user_profile",
            "poke_user",
            "like_user",
            "recall_message",
            "relay_message",
            "list_pending_requests",
            "handle_request",
            "send_friend_request",
        ]
        ok, missing = [], []
        for name in wanted:
            try:
                hit = mgr.get_func(name) is not None
            except Exception:
                hit = False
            (ok if hit else missing).append(name)

        if not missing:
            yield event.plain_result(
                f"工具自检：✅ {len(ok)} 个工具全部已注册（{'、'.join(ok)}）。"
                "如果 AI 仍说没有某个工具，请检查：人格工具列表是否勾选并保存、"
                "会话是否用了自定义规则覆盖人格。"
            )
            return

        try:
            names = [getattr(t, "name", "?") for t in mgr.func_list]
        except Exception:
            names = []
        lines = []
        if ok:
            lines.append(f"已注册（{len(ok)}）：{'、'.join(ok)}")
        lines.append(f"缺失（{len(missing)}）：{'、'.join(missing)}")
        lines.append(f"管理器当前工具总数 {len(names)}：{names}")
        yield event.plain_result("工具自检：\n" + "\n".join(lines))

    # ------------------------------------------------------------------ #
    # 核心读取逻辑                                                        #
    # ------------------------------------------------------------------ #
    async def _read_recent(
        self, event: AstrMessageEvent, group_id: str, count: int,
        keyword: str = "", sender: str = "",
        start_time: str = "", end_time: str = "",
    ) -> str:
        """读群消息：协议端 API + 本地缓存合并去重，再按条件筛选。

        为什么要合并：协议端的 get_group_msg_history 经常不含机器人自己刚发的话，
        也可能不把「正在进行」的那几条吐出来。只查 API 会漏掉最新现场。
        """
        raw = []
        extra = max(0, self._cfg_int("cache.api_history_count", 30))
        try:
            raw = await self._fetch_raw_from_platform(event, group_id, count + extra)
        except Exception as e:
            logger.warning(f"从协议端读取群消息失败，将尝试回退: {e}")

        # 缓存是会话维度存的，只有查「当前这个群」时才能合并进来
        if group_id and str(group_id) == str(event.get_group_id() or ""):
            raw = self._merge_raw(raw, self._cache_to_onebot(self._cache_key(event)))

        filtered = self._filter_raw(
            raw, keyword=keyword, sender=sender,
            start_time=start_time, end_time=end_time,
        )
        if filtered:
            return self._format_messages(filtered[-count:])

        # 回退：AstrBot 会话历史（所有平台可用）
        if not raw and self._cfg("read.enable_fallback", True):
            try:
                text = await self._fetch_from_conversation(event, count)
                if text:
                    return (
                        "（注：以下记录来自 AstrBot 会话历史，仅包含与本机器人的"
                        "对话，非完整群聊记录）\n" + text
                    )
            except Exception as e:
                logger.warning(f"从会话历史读取失败: {e}")
        return ""

    async def _fetch_raw_from_platform(self, event: AstrMessageEvent, group_id: str, count: int) -> list:
        """拿原始消息对象列表（不格式化），供合并/筛选使用。"""
        client = self._get_client(event)
        if client is None:
            return []
        ret = await client.api.call_action(
            "get_group_msg_history", group_id=int(group_id), count=count
        )
        return self._extract_messages(ret)

    @staticmethod
    def _merge_raw(*batches) -> list:
        """多路消息按 message_id 去重、按时间升序。"""
        seen, out = set(), []
        for msgs in batches:
            for m in msgs or []:
                if not isinstance(m, dict):
                    continue
                key = str(m.get("message_id") or "")
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                out.append(m)
        out.sort(key=lambda m: m.get("time") or 0)
        return out

    def _filter_raw(
        self, msgs: list, keyword: str = "", sender: str = "",
        start_time: str = "", end_time: str = "",
    ) -> list:
        """按关键词 / 发送者 / 时间范围筛选。任一条件为空即不限制。"""
        kw = str(keyword or "").strip().lower()
        se = self._norm_name(sender)
        t0 = self._parse_time(start_time, 0.0)
        t1 = self._parse_time(end_time, float("inf"))
        out = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            ts = m.get("time") or 0
            try:
                if not (t0 <= float(ts) <= t1):
                    continue
            except (TypeError, ValueError):
                pass
            if kw:
                body = self._segments_to_text(m.get("message")).lower()
                if kw not in body:
                    continue
            if se:
                s = m.get("sender") or {}
                name = self._norm_name(s.get("card") or s.get("nickname"))
                uid = str(s.get("user_id") or "").lower()
                if se not in name and se not in uid:
                    continue
            out.append(m)
        return out

    @staticmethod
    def _parse_time(value, default):
        """解析 YYYY-MM-DD[ HH:MM[:SS]]，解析不了就用默认值（不因为一个烂参数整单作废）。"""
        s = str(value or "").strip()
        if not s:
            return default
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
        return default

    async def _fetch_from_conversation(
        self, event: AstrMessageEvent, count: int
    ) -> str:
        """回退方案：读取 AstrBot 当前会话的 LLM 对话历史（全平台可用）。"""
        uid = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        if not curr_cid:
            return ""
        conversation = await conv_mgr.get_conversation(uid, curr_cid)
        history = getattr(conversation, "history", "") or ""
        if not history:
            return ""
        try:
            records = json.loads(history)
        except (TypeError, ValueError):
            return ""
        if not isinstance(records, list) or not records:
            return ""

        lines = []
        for item in records[-count:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "?")
            content = item.get("content", "")
            if isinstance(content, list):
                # 兼容分段存储格式：[{"type": "text", "text": "..."}, ...]
                content = " ".join(
                    str(p.get("text", ""))
                    for p in content
                    if isinstance(p, dict)
                )
            if role == "user":
                role_name = "用户"
            elif role == "assistant":
                role_name = "机器人"
            else:
                role_name = str(role)
            lines.append(f"{role_name}: {content}")
        return "\n".join(lines)

    async def _llm_summarize(
        self, event: AstrMessageEvent, group_id: str, count: int, records: str
    ) -> str:
        """将群聊记录交给 LLM，生成第三人称转述总结。

        调用优先级（从高到低）：
        1. 插件配置的独立简报 API（OpenAI 兼容：brief_api_base/brief_api_key/brief_model）；
        2. AstrBot 当前会话的对话 AI（v4.5.7+ 的 llm_generate）；
        3. AstrBot 当前会话的提供商 text_chat（旧版 API）；
        4. 遍历 AstrBot 全部提供商逐个尝试 text_chat。

        全部失败时返回空字符串，并把失败原因写入 self._brief_last_error 和日志。
        """
        prompt = (
            "你是一名群聊观察员。以下是群号 " + str(group_id) + " 最近 "
            + str(count) + " 条聊天记录"
            "（格式：时间 昵称(QQ号): 内容，按时间顺序排列）：\n\n"
            + records
            + "\n\n请以第三人称客观转述并总结以上群聊内容，重点包括："
            "1) 谁（用昵称）做了什么、说了什么关键内容；"
            "2) 大家讨论了哪些话题，有哪些关键信息、结论或待办事项；"
            "3) 有争议或情绪化的地方（如有）。"
            "要求：用中文输出，条理清晰、自然叙述，不要逐条罗列消息，"
            "总字数控制在 300 字左右。"
        )
        errors = []

        # 路径1：独立简报 API（用户可选的"简报生成 API"）
        if str(self._cfg("brief.brief_model", "")).strip():
            try:
                text = await self._llm_summarize_direct_api(prompt)
                if text:
                    return text
                errors.append("独立简报 API 返回为空")
            except Exception as e:
                errors.append(f"独立简报 API 失败: {e}")

        umo = event.unified_msg_origin

        # 路径2：v4.5.7+ llm_generate
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if provider_id:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=prompt
                )
                text = resp.completion_text
                if text:
                    return text
                errors.append("llm_generate 返回为空")
        except Exception as e:
            errors.append(f"llm_generate: {e}")

        # 路径3：当前会话提供商 text_chat（旧 API）
        try:
            prov = await self.context.get_using_provider_async(umo=umo)
            if prov:
                resp = await prov.text_chat(
                    prompt=prompt,
                    context=[],
                    system_prompt="你是一名客观的群聊观察员。",
                )
                text = resp.completion_text
                if text:
                    return text
                errors.append("text_chat 返回为空")
        except Exception as e:
            errors.append(f"text_chat: {e}")

        # 路径4：遍历全部提供商
        try:
            for prov in self.context.get_all_providers():
                try:
                    resp = await prov.text_chat(
                        prompt=prompt,
                        context=[],
                        system_prompt="你是一名客观的群聊观察员。",
                    )
                    text = resp.completion_text
                    if text:
                        return text
                except Exception as e:
                    errors.append(f"provider[{getattr(prov, 'id', '?')}] text_chat: {e}")
        except Exception as e:
            errors.append(f"get_all_providers: {e}")

        reason = "; ".join(errors) if errors else "未知原因"
        logger.error(f"群简报 LLM 生成全部失败: {reason}")
        self._brief_last_error = reason
        return ""

    async def _llm_generate_any(
        self, event: AstrMessageEvent, prompt: str, system_prompt: str = ""
    ) -> str:
        """通用 LLM 生成（4 级兜底），供指令读取记录后"由 LLM 加工回复"使用。

        优先级：独立 API → llm_generate → text_chat → 遍历全部提供商。
        系统提示词优先使用 AstrBot 当前会话的人格提示词（保持与主对话一致的
        口吻、人设与分段习惯）；拿不到人格时才用传入的 system_prompt 或中性兜底。
        全部失败返回空字符串（调用方自行决定回退原文）。
        """
        persona_prompt = self._get_persona_prompt(event)
        effective_sys = persona_prompt or system_prompt or (
            "你是一个自然的聊天者。请像朋友一样，用第一人称、日常对话的口吻说话，"
            "适当分段换行，不要一大段连在一起，不要输出结构化的列表或总结。"
        )
        errors = []
        if str(self._cfg("brief.brief_model", "")).strip():
            try:
                text = await self._llm_summarize_direct_api(prompt, effective_sys)
                if text:
                    return text
                errors.append("独立 API 返回为空")
            except Exception as e:
                errors.append(f"独立 API 失败: {e}")

        umo = event.unified_msg_origin
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if provider_id:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=prompt
                )
                text = resp.completion_text
                if text:
                    return text
                errors.append("llm_generate 返回为空")
        except Exception as e:
            errors.append(f"llm_generate: {e}")

        try:
            prov = await self.context.get_using_provider_async(umo=umo)
            if prov:
                resp = await prov.text_chat(
                    prompt=prompt,
                    context=[],
                    system_prompt=effective_sys,
                )
                text = resp.completion_text
                if text:
                    return text
                errors.append("text_chat 返回为空")
        except Exception as e:
            errors.append(f"text_chat: {e}")

        try:
            for prov in self.context.get_all_providers():
                try:
                    resp = await prov.text_chat(
                        prompt=prompt,
                        context=[],
                        system_prompt=effective_sys,
                    )
                    text = resp.completion_text
                    if text:
                        return text
                except Exception as e:
                    errors.append(f"provider[{getattr(prov, 'id', '?')}] text_chat: {e}")
        except Exception as e:
            errors.append(f"get_all_providers: {e}")

        reason = "; ".join(errors) if errors else "未知原因"
        logger.warning(f"LLM 生成失败（指令加工回复）: {reason}")
        return ""

    def _get_persona_prompt(self, event: AstrMessageEvent) -> str:
        """读取 AstrBot 当前会话人格（persona）的系统提示词，用于让插件调 LLM
        时保持与主对话一致的口吻与人设。兼容不同版本的 persona 管理器 API，
        找不到时返回空串。
        """
        try:
            mgr = self.context.persona_manager
        except Exception:
            mgr = None
        if mgr is None:
            return ""
        persona = None
        for method in ("get_selected_persona", "get_persona", "get_current_persona"):
            fn = getattr(mgr, method, None)
            if fn is None:
                continue
            try:
                persona = fn(event.unified_msg_origin)
                if persona is not None:
                    break
            except Exception:
                persona = None
        if persona is None:
            return ""
        for attr in ("system_prompt", "prompt", "content", "text"):
            v = getattr(persona, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    async def _llm_summarize_direct_api(
        self, prompt: str, system_prompt: str = ""
    ) -> str:
        """通过插件配置的独立 OpenAI 兼容 API 生成简报（不依赖 AstrBot 提供商）。"""
        base = str(self._cfg("brief.brief_api_base", "")).strip().rstrip("/")
        key = str(self._cfg("brief.brief_api_key", "")).strip()
        model = str(self._cfg("brief.brief_model", "")).strip()
        if not (base and key and model):
            return ""
        try:
            import aiohttp
        except ImportError:
            logger.error("群简报独立 API 需要 aiohttp（AstrBot 通常自带该依赖）")
            return ""
        url = base + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt or "你是一名客观的群聊观察员。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    data = await resp.json()
            choices = data.get("choices") or []
            if not choices:
                logger.error(f"群简报独立 API 响应中无 choices: {data}")
                return ""
            content = (choices[0].get("message") or {}).get("content") or ""
            return content.strip()
        except Exception as e:
            logger.error(f"群简报独立 API 调用失败: {e}")
            return ""

    # ------------------------------------------------------------------ #
    # 按人检索：/查人 与 search_group_member_messages                      #
    # ------------------------------------------------------------------ #
    async def _search_member(
        self, event: AstrMessageEvent, name: str, group_query: str, max_hits: int
    ) -> str:
        """按名称/QQ号模糊查询某个群成员的最近发言。

        群解析：群号（数字）或群名（模糊，经 get_group_list）；私聊时必须提供。
        数据源：优先协议端 get_group_msg_history 翻页扫描；失败回退会话历史。
        任何异常都会被捕获并转成文本，保证调用方永远拿到可读结果。
        """
        try:
            gid, label, err = await self._resolve_group(event, group_query)
            if err:
                return err
            try:
                text = await self._search_from_platform(event, gid, name, max_hits)
                if text:
                    return f"群 {label} 中与“{name}”匹配的最近发言：\n" + text
            except Exception as e:
                logger.warning(f"按人检索（协议端）失败，尝试回退会话历史: {e}")
            if self._cfg("read.enable_fallback", True):
                try:
                    text = await self._search_from_conversation(event, name, max_hits)
                    if text:
                        return "（注：以下来自 AstrBot 会话历史，仅含与机器人的对话）\n" + text
                except Exception as e:
                    logger.warning(f"按人检索（会话历史）失败: {e}")
            return ""
        except Exception as e:
            logger.error(f"_search_member 异常: {type(e).__name__}: {e}")
            return f"按人查询内部错误：{type(e).__name__}: {e}（可改用群号指定群重试）"

    async def _resolve_group(
        self, event: AstrMessageEvent, group_query: str
    ) -> tuple:
        """解析群：返回 (gid, label, err)。群号数字直接匹配；非数字按群名模糊匹配。"""
        q = (group_query or "").strip()
        if not q:
            gid = event.get_group_id() or ""
            if gid:
                return gid, gid, ""
            return "", "", (
                "当前是私聊会话且未提供群号/群名。请在参数中指定要查询的群，"
                "群号最稳妥，群名也支持模糊（如群名含“项目”就写“项目”）。"
            )
        if str(q).isdigit():
            return q, q, ""
        gid, label = await self._fuzzy_group_by_name(event, q)
        if gid:
            return gid, label, ""
        return "", "", (
            f"未找到名称包含“{q}”的群。按群名匹配需要协议端支持 get_group_list；"
            f"若群名匹配不可用，请改用群号指定。"
        )

    async def _fuzzy_group_by_name(self, event: AstrMessageEvent, q: str) -> tuple:
        """通过 get_group_list 按群名模糊匹配，返回 (group_id, group_name)。"""
        client = self._get_client(event)
        if client is None:
            logger.info(f"按群名匹配“{q}”失败：无法获取协议端 client（非 aiocqhttp 或事件类型不符）")
            return "", ""
        try:
            ret = await client.api.call_action("get_group_list")
        except Exception as e:
            logger.warning(f"get_group_list 失败: {type(e).__name__}: {e}")
            return "", ""
        groups = ret if isinstance(ret, list) else (
            ret.get("data") if isinstance(ret, dict) else None
        )
        if not isinstance(groups, list):
            logger.warning(f"get_group_list 返回结构异常: {type(ret).__name__}: {str(ret)[:200]}")
            return "", ""
        logger.info(f"get_group_list 返回 {len(groups)} 个群")
        q_l = q.lower()
        hits = [
            g for g in groups
            if isinstance(g, dict) and q_l in str(g.get("group_name", "")).lower()
        ]
        if not hits:
            logger.info(f"群名“{q}”未匹配到任何群，现有群名: {[str(g.get('group_name','')) for g in groups if isinstance(g, dict)][:10]}")
            return "", ""
        gid = str(hits[0].get("group_id", ""))
        label = str(hits[0].get("group_name", gid))
        logger.info(f"群名“{q}”匹配到群: {label}({gid})")
        return gid, label

    async def _search_from_platform(
        self, event: AstrMessageEvent, gid: str, name: str, max_hits: int
    ) -> str:
        """翻页读取群历史消息，按发送者（群名片/昵称/QQ号）模糊过滤。"""
        client = self._get_client(event)
        if client is None:
            return ""
        scan_limit = int(self._cfg("search.search_scan_limit", 200))
        page_size = 20
        max_pages = max(1, min(20, (scan_limit + page_size - 1) // page_size))
        scanned = 0
        next_seq = None
        seen_ids = set()
        hits = []
        name_l = str(name).lower()

        for page_idx in range(max_pages):
            payload = {"group_id": int(gid), "count": page_size}
            if next_seq:
                payload["message_seq"] = next_seq
            ret = await client.api.call_action("get_group_msg_history", **payload)
            messages = self._extract_messages(ret)
            if not messages:
                logger.info(f"按人检索：群 {gid} 第 {page_idx+1} 页无消息，停止翻页")
                break
            seqs = [
                m.get("message_seq")
                for m in messages
                if isinstance(m, dict) and m.get("message_seq")
            ]
            added_scan = 0
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = m.get("message_id")
                if mid is not None:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                added_scan += 1
                scanned += 1
                if self._match_member(m, name_l):
                    hits.append(m)
                    if len(hits) >= max_hits:
                        logger.info(f"按人检索：群 {gid} 已收集 {len(hits)} 条匹配（扫描 {scanned} 条）")
                        return self._format_messages(hits)
            if added_scan == 0 or scanned >= scan_limit:
                logger.info(f"按人检索：群 {gid} 扫描达上限 {scan_limit} 条（匹配 {len(hits)} 条），停止")
                break
            if seqs:
                next_seq = min(seqs)
            else:
                logger.info(f"按人检索：群 {gid} 消息无 message_seq，无法继续翻页（已扫 {scanned} 条）")
                break  # 消息里没有 message_seq，无法继续翻页
        if hits:
            logger.info(f"按人检索：群 {gid} 共匹配 {len(hits)} 条（扫描 {scanned} 条）")
            return self._format_messages(hits)
        logger.info(f"按人检索：群 {gid} 扫描 {scanned} 条，未找到“{name}”的发言")
        return ""

    @staticmethod
    def _match_member(m: dict, name_l: str) -> bool:
        """判断某条消息是否由与 name_l（小写）模糊匹配的成员发送。"""
        sender = m.get("sender") or {}
        nickname = str(sender.get("card") or sender.get("nickname") or "")
        uid = str(sender.get("user_id") or "")
        return name_l in nickname.lower() or name_l in uid.lower()

    async def _search_from_conversation(
        self, event: AstrMessageEvent, name: str, max_hits: int
    ) -> str:
        """回退方案：在 AstrBot 会话历史里按名称过滤发言。"""
        uid = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        if not curr_cid:
            return ""
        conversation = await conv_mgr.get_conversation(uid, curr_cid)
        history = getattr(conversation, "history", "") or ""
        if not history:
            return ""
        try:
            records = json.loads(history)
        except (TypeError, ValueError):
            return ""
        if not isinstance(records, list):
            return ""
        name_l = str(name).lower()
        lines = []
        for item in records[-200:]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                )
            text = str(content)
            if name_l in text.lower():
                role = "用户" if item.get("role") == "user" else (
                    "机器人" if item.get("role") == "assistant" else str(item.get("role", "?"))
                )
                lines.append(f"{role}: {text[:200]}")
                if len(lines) >= max_hits:
                    break
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 私聊记录：/私聊记录 与 read_friend_recent_messages                  #
    # ------------------------------------------------------------------ #
    async def _read_friend(
        self, event: AstrMessageEvent, friend: str, count: int
    ) -> str:
        """读取与某个好友的私聊记录。

        目标好友解析：不填=当前私聊会话的对方；数字=QQ号；其他=按昵称/备注模糊匹配。
        数据源：优先协议端 get_friend_msg_history（NapCat 扩展）；失败回退会话历史。
        任何异常都会被捕获并转成文本，保证调用方永远拿到可读结果。
        """
        try:
            uid, label, err = await self._resolve_friend(event, friend)
            if err:
                return err
            try:
                text = await self._fetch_friend_from_platform(event, uid, count)
                if text:
                    return f"与 {label} 的最近私聊记录：\n" + text
            except Exception as e:
                logger.warning(f"好友私聊记录（协议端）失败，尝试回退会话历史: {e}")
            if self._cfg("read.enable_fallback", True):
                try:
                    text = await self._fetch_from_conversation(event, count)
                    if text:
                        return (
                            "（注：以下来自 AstrBot 会话历史，仅含与机器人的对话）\n" + text
                        )
                except Exception as e:
                    logger.warning(f"好友私聊记录（会话历史）失败: {e}")
            return ""
        except Exception as e:
            logger.error(f"_read_friend 异常: {type(e).__name__}: {e}")
            return f"私聊记录查询内部错误：{type(e).__name__}: {e}（可改用QQ号指定好友重试）"

    async def _resolve_friend(self, event: AstrMessageEvent, friend: str) -> tuple:
        """解析目标好友：返回 (user_id, label, err)。"""
        q = (friend or "").strip()
        if not q:
            uid = self._current_peer_uid(event)
            if uid:
                return uid, uid, ""
            return "", "", (
                "当前不是与好友的私聊会话且未指定好友。请在参数中给出好友的"
                "昵称、备注或QQ号（名称支持模糊匹配）。"
            )
        if str(q).isdigit():
            return q, q, ""
        uid, label = await self._fuzzy_friend(event, q)
        if uid:
            return uid, label, ""
        return "", "", (label or (
            f"未找到与“{q}”匹配的好友（按名称匹配需协议端支持 get_friend_list，"
            f"或直接用QQ号指定）。"
        ))

    @staticmethod
    def _norm_qname(s: str) -> str:
        """名字归一：剥掉所有不可见格式字符（零宽、方向翻转、韩文填充等）后 casefold。
        只按手写零宽区间过滤会漏（U+115F、U+2067 都不属于 Cf），按 Unicode
        category=Cf 整类剥才干净。"""
        s = "".join(ch for ch in str(s or "") if unicodedata.category(ch) != "Cf")
        s = re.sub(r"[\u115f\u1160\s]+", "", s)
        return s.casefold()

    async def _fuzzy_friend(self, event: AstrMessageEvent, q: str) -> tuple:
        """通过 get_friend_list 按备注名/昵称/QQ号模糊匹配，返回 (user_id, 名称)。

        备注和昵称必须**分开**比对：之前 `remark or nickname` 只留一个字段，
        好友一旦设了备注（哪怕备注和昵称完全无关），昵称里明明有的名字也匹配不上。
        匹配失败时把候选名单返回给 AI，它能自己改用正确名字或 QQ 号重试。
        """
        client = self._get_client(event)
        if client is None:
            logger.info(f"按好友名匹配“{q}”失败：无法获取协议端 client")
            return "", ""
        try:
            ret = await client.api.call_action("get_friend_list")
        except Exception as e:
            logger.warning(f"get_friend_list 失败: {type(e).__name__}: {e}")
            return "", ""
        friends = ret if isinstance(ret, list) else (
            ret.get("data") if isinstance(ret, dict) else None
        )
        if not isinstance(friends, list):
            logger.warning(f"get_friend_list 返回结构异常: {type(ret).__name__}: {str(ret)[:200]}")
            return "", ""
        logger.info(f"get_friend_list 返回 {len(friends)} 个好友")
        q_l = self._norm_qname(q)
        hits = []
        for f in friends:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("user_id") or "")
            names = [self._norm_qname(f.get("remark")),
                     self._norm_qname(f.get("nickname"))]
            if (q_l and any(n and (q_l in n or (len(n) >= 2 and n in q_l)) for n in names)) \
                    or (q_l and q_l in uid) or (uid and uid in q_l):
                hits.append(f)
        if not hits:
            cand = [
                f"{f.get('remark') or f.get('nickname') or '?'}#{f.get('user_id')}"
                for f in friends if isinstance(f, dict)
            ]
            logger.info(f"好友名“{q}”未匹配到任何人，列表里有: {cand}")
            return "", (
                f"未找到与“{q}”匹配的好友。好友列表里有：{'、'.join(cand[:8])}。"
                f"可改用列表里的名字重试，或直接用QQ号。"
            )
        f0 = hits[0]
        uid = str(f0.get("user_id", ""))
        label = str(f0.get("remark") or f0.get("nickname") or uid)
        logger.info(f"好友名“{q}”匹配到 {label}({uid})")
        return uid, label

    def _current_peer_uid(self, event: AstrMessageEvent) -> str:
        """私聊会话中，当前对话对方的 QQ 号；群聊或无法取得时返回空串。"""
        if event.get_group_id():
            return ""
        v = getattr(event, "sender_id", None)
        if v:
            return str(v)
        for attr in ("message_obj", "message"):
            m = getattr(event, attr, None)
            if isinstance(m, dict):
                sender = m.get("sender") or {}
                uid = sender.get("user_id")
                if uid:
                    return str(uid)
        return ""

    async def _fetch_friend_from_platform(
        self, event: AstrMessageEvent, user_id: str, count: int
    ) -> str:
        """翻页读取与某好友的私聊消息历史（get_friend_msg_history）。"""
        client = self._get_client(event)
        if client is None:
            return ""
        scan_limit = int(self._cfg("search.friend_scan_limit", 200))
        page_size = 20
        max_pages = max(1, min(20, (scan_limit + page_size - 1) // page_size))
        seen_ids = set()
        collected = []
        next_seq = None

        for _ in range(max_pages):
            payload = {"user_id": int(user_id), "count": page_size}
            if next_seq:
                payload["message_seq"] = next_seq
            ret = await client.api.call_action("get_friend_msg_history", **payload)
            messages = self._extract_messages(ret)
            if not messages:
                break
            fresh = []
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = m.get("message_id")
                if mid is not None:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                fresh.append(m)
            if not fresh:
                break
            collected.extend(fresh)
            if len(collected) >= scan_limit:
                break
            seqs = [
                m.get("message_seq")
                for m in messages
                if isinstance(m, dict) and m.get("message_seq")
            ]
            if seqs:
                next_seq = min(seqs)
            else:
                break  # 消息里没有 message_seq，无法继续翻页
        if not collected:
            return ""
        # 收集顺序为"新→旧"，取最新 count 条，交给统一格式化（会按时间升序重排）
        msgs = collected[:count] if len(collected) > count else collected
        return self._format_messages(msgs)

    def _get_client(self, event: AstrMessageEvent):
        """从 aiocqhttp 事件中获取协议端 client；其他平台返回 None。"""
        if event.get_platform_name() != "aiocqhttp":
            return None
        event_cls = self._get_aiocqhttp_event_cls()
        if event_cls is None or not isinstance(event, event_cls):
            return None
        client = getattr(event, "bot", None)
        if client is None or not hasattr(client, "api"):
            return None
        return client

    # ------------------------------------------------------------------ #
    # 会话消息缓存                                                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clock(ts: float) -> str:
        try:
            return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
        except (TypeError, ValueError, OSError):
            return "--:--:--"

    def _cache_key(self, event: AstrMessageEvent) -> str:
        try:
            return event.get_session_id() or ""
        except Exception:
            return ""

    @staticmethod
    def _sid_from_session(session) -> str:
        """unified_msg_origin（111:FriendMessage:10001）→ 缓存键。

        缓存键用的是 event.get_session_id()：aiocqhttp 下它就是「群号」或「对方
        QQ 号」，跟 MessageSession.session_id 是同一个值，所以主动发送的会话能
        落进 AI 平时读的那个格子。解析不出来宁可返回空（不写），也不要写进一个
        错的格子去污染别的会话。
        """
        s = str(session or "").strip()
        if not s:
            return ""
        try:
            from astrbot.core.platform.message_session import MessageSession

            return str(MessageSession.from_str(s).session_id or "")
        except Exception:
            parts = s.split(":")
            return parts[-1] if len(parts) >= 3 else ""

    def _cache_append(
        self, sid: str, body: str, *, sender: str, sender_id: str,
        mid: str, is_bot: bool,
    ) -> None:
        """往指定会话的缓存尾部追加一条。所有写入口最终都走这里。"""
        body = str(body or "").strip()
        if not sid or not body:
            return
        size = max(20, self._cfg_int("cache.cache_size", 300))
        buf = self._msg_cache.get(sid)
        # maxlen 变了（用户改了配置）就重建一次，否则 deque 会一直用旧上限
        if buf is None or buf.maxlen != size:
            buf = deque(list(buf or []), maxlen=size)
            self._msg_cache[sid] = buf
        if is_bot and buf:
            # 插件之间会互相 hook context.send_message 叠成一条链，同一条消息可能
            # 被记两遍（sugarmemory 也挂了同一个方法）。按「3 秒内同文本」吞掉。
            last = buf[-1]
            if (
                last.get("is_bot")
                and str(last.get("text") or "") == body[:1000]
                and time.time() - float(last.get("ts") or 0) < 3.0
            ):
                return
        now = time.time()
        buf.append({
            "ts": now,
            "sender": sender,
            "sender_id": sender_id,
            "text": body[:1000],
            "mid": mid,
            "is_bot": is_bot,
        })
        self._cache_last_active[sid] = now
        self._cache_cleanup()

    def _cache_put(self, event: AstrMessageEvent, text: str = "", is_bot: bool = False) -> None:
        """把一条消息塞进内存缓存（同会话按时间顺序，超长自动丢最早的）。"""
        if not self._cfg_bool("cache.cache_enabled", True):
            return
        if is_bot and not self._cfg_bool("cache.cache_bot_messages", True):
            return
        sid = self._cache_key(event)
        if not sid:
            return
        body = str(text or ("" if is_bot else getattr(event, "message_str", "")) or "").strip()
        if not body:
            return
        if is_bot:
            # 自己发的消息拿不到出站 message_id：after_message_sent 拿到的 event
            # 是「触发这次回复的那条入站消息」，它的 message_id 属于用户。存进来
            # 会跟用户那条撞 id，合并去重时把 bot 消息误删，撤回时更会撤掉别人的话。
            mid = ""
        else:
            try:
                mid = str(getattr(event.message_obj, "message_id", "") or "")
            except Exception:
                mid = ""
        try:
            sender = "我" if is_bot else (event.get_sender_name() or "")
        except Exception:
            sender = "我" if is_bot else ""
        try:
            sender_id = "bot" if is_bot else str(event.get_sender_id() or "")
        except Exception:
            sender_id = "bot" if is_bot else ""
        self._cache_append(
            sid, body, sender=sender, sender_id=sender_id, mid=mid, is_bot=is_bot
        )

    def _cache_put_outbound(self, session, text: str) -> None:
        """记录「AI 主动发到某个会话」的消息（不经过回复流水线的那些）。"""
        if not self._cfg_bool("cache.cache_enabled", True):
            return
        if not self._cfg_bool("cache.cache_bot_messages", True):
            return
        sid = self._sid_from_session(session)
        if not sid:
            return
        self._cache_append(
            sid, text, sender="我", sender_id="bot", mid="", is_bot=True
        )

    def _hook_context_send(self) -> None:
        """给 context.send_message 套一层，把 AI 主动发出的消息也记进缓存。

        为什么必须 hook：定时任务（sugarcron）和跨会话转述走的是
        ``context.send_message``，它直接调 platform.send_by_session，压根不经过
        「回复当前事件」那条流水线；而 ``_cache_on_bot_sent`` 挂的
        ``after_message_sent`` 在整个 AstrBot 里只有 respond/stage.py 一处触发点。
        结果就是：AI 到点在群里发了提醒，回头复盘却「看不到自己说过的话」，
        跟用户对质时只能否认。sugarmemory 里有个同名同因的 _hook_context_send，
        踩的是同一个坑（见它的 main.py）。

        只加记录，不改原行为：返回值原样透传，异常照抛，失败只记 debug。
        """
        try:
            ctx = self.context
            orig = getattr(ctx, "send_message", None)
            if orig is None:
                return
            my_id = id(self)
            # 本实例已经挂过了就直接走（自愈路径每条消息都会进来一次，不能重挂）
            probe, seen = orig, set()
            while probe is not None and id(probe) not in seen:
                if getattr(probe, "_ghagent_owner", None) == my_id:
                    return
                seen.add(id(probe))
                probe = getattr(probe, "_ghagent_orig", None)
            # 重载后旧实例留下的那层要剥掉：它的 _msg_cache 已经没人读了，留着
            # 只会让链条越挂越长、消息写进一个死缓存。
            while getattr(orig, "_ghagent_wrapped", False):
                orig = getattr(orig, "_ghagent_orig", None)
                if orig is None:
                    return
            plugin = self

            async def _wrapped(session, message_chain, *args, **kwargs):
                result = await orig(session, message_chain, *args, **kwargs)
                try:
                    # MessageChain 没有 __iter__（对 it 直接 for 会 TypeError），
                    # 真正的消息段在 .chain 这个 list 上；也兼容有人直接传 list。
                    parts = getattr(message_chain, "chain", None)
                    if parts is None:
                        parts = (
                            message_chain
                            if isinstance(message_chain, (list, tuple))
                            else []
                        )
                    text = "".join(str(getattr(p, "text", "") or "") for p in parts)
                    if text.strip():
                        plugin._cache_put_outbound(session, text)
                except Exception:
                    logger.debug("[GroupHistory] 缓存主动发送失败", exc_info=True)
                return result

            _wrapped._ghagent_wrapped = True
            _wrapped._ghagent_orig = orig
            _wrapped._ghagent_owner = my_id
            ctx.send_message = _wrapped
            logger.info(
                "[GroupHistory] 已挂载主动发送缓存（定时任务/转述发的消息也会进缓存）"
            )
        except Exception:
            logger.warning("[GroupHistory] 挂载主动发送缓存失败", exc_info=True)

    def _cache_cleanup(self) -> None:
        """清掉太久没动静的会话缓存。最多 60 秒做一次，别每条消息都全表扫。"""
        timeout = self._cfg_int("cache.cache_inactive_timeout", 3600)
        if timeout <= 0:
            return
        now = time.time()
        if now - self._cache_cleaned_at < 60:
            return
        self._cache_cleaned_at = now
        dead = [k for k, ts in self._cache_last_active.items() if now - ts > timeout]
        for k in dead:
            self._msg_cache.pop(k, None)
            self._cache_last_active.pop(k, None)
        if dead:
            logger.debug(f"清掉 {len(dead)} 个冷会话缓存")

    def _cache_to_onebot(self, sid: str) -> list:
        """把缓存里的消息转成 OneBot 消息格式，好跟协议端结果合并去重。"""
        out = []
        for m in list(self._msg_cache.get(sid) or []):
            out.append({
                "time": int(m.get("ts") or 0),
                "message_id": m.get("mid") or "",
                "message": [{"type": "text", "data": {"text": m.get("text", "")}}],
                "sender": {
                    "user_id": m.get("sender_id") or "",
                    "nickname": m.get("sender") or "",
                },
                "_from_cache": True,
            })
        return out

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def _cache_on_message(self, event: AstrMessageEvent):
        """把收到的消息存进缓存（优先级 100：尽早拿到，别被别的插件拦掉）。

        顺手把「加群/好友申请」这类 request 事件分流出去：它长得像消息（有 group_id
        时甚至会被当成群消息），塞进聊天缓存会污染「最近消息」，而它真正有用的是
        里面那串 flag——处理申请全靠它。
        """
        try:
            # 自愈：context 是所有插件共享的，别的插件重载时可能把 send_message
            # 整体还原，把我们挂的那层一起抹掉。收消息时顺手确认一次，几微秒的事，
            # 总比缓存悄悄失效、AI 又开始否认自己说过话强。
            self._hook_context_send()
            if self._catch_request_event(event):
                return
            if self._catch_soft_request_text(event):
                # 软申请：NapCat 文本已单独入档，不再当普通聊天缓存（避免 AI
                # 在「最近消息」里看到它却不知道那是申请信号）。
                return
            self._cache_put(event)
        except Exception:
            logger.debug("缓存入站消息失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 申请（加群 / 加好友）收集与处理                                      #
    # ------------------------------------------------------------------ #
    def _pending_store_path(self) -> str:
        base = os.path.join("data", "plugin_data",
                            "astrbot_plugin_group_history_agent")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        return os.path.join(base, "pending_requests.json")

    def _load_pending_requests(self):
        """启动时从磁盘恢复待处理申请。过期的（超过 requests_ttl_hours）直接丢。"""
        try:
            p = self._pending_store_path()
            if not os.path.exists(p):
                return
            with open(p, "r", encoding="utf-8") as f:
                data = json.loads(f.read() or "{}")
            if not isinstance(data, dict):
                return
            now = time.time()
            ttl = max(0, self._cfg_int("requests.requests_ttl_hours", 24)) * 3600
            for k, v in data.items():
                if not isinstance(v, dict):
                    continue
                if ttl and now - float(v.get("ts") or 0) > ttl:
                    continue
                self._pending_requests[k] = v
            if self._pending_requests:
                logger.info(f"[QQTools·请求] 从磁盘恢复 "
                            f"{len(self._pending_requests)} 条待处理申请")
        except Exception:
            logger.debug("恢复待处理申请失败", exc_info=True)

    def _save_pending_requests(self):
        try:
            p = self._pending_store_path()
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._pending_requests, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            logger.debug("保存待处理申请失败", exc_info=True)

    def _raw_request(self, event: AstrMessageEvent) -> dict:
        """取出 OneBot 原始的 request 事件字典，不是就返回空。"""
        mo = getattr(event, "message_obj", None)
        raw = getattr(mo, "raw_request", None)
        if not isinstance(raw, dict):
            raw = getattr(mo, "raw_message", None)
        if not isinstance(raw, dict):
            raw = getattr(mo, "raw", None)
        if not isinstance(raw, dict) or raw.get("post_type") != "request":
            return {}
        return raw

    def _catch_request_event(self, event: AstrMessageEvent) -> bool:
        """收一条申请事件。返回 True 表示这是申请，调用方别再当消息处理。"""
        if not self._cfg_bool("requests.requests_enabled", True):
            return False
        raw = self._raw_request(event)
        if not raw:
            return False
        flag = str(raw.get("flag") or "").strip()
        rtype = str(raw.get("request_type") or "").strip()
        if rtype not in ("friend", "group"):
            return False
        max_n = max(1, self._cfg_int("requests.requests_max", 50))
        ttl = max(0, self._cfg_int("requests.requests_ttl_hours", 24)) * 3600
        now = time.time()
        # 先清过期的和超限的（按时间最旧的丢），再塞新的
        for k in [k for k, v in self._pending_requests.items()
                  if (ttl and now - float(v.get("ts") or 0) > ttl)]:
            self._pending_requests.pop(k, None)
        if len(self._pending_requests) >= max_n:
            for k in sorted(self._pending_requests,
                            key=lambda x: float(self._pending_requests[x].get("ts") or 0)
                            )[: len(self._pending_requests) - max_n + 1]:
                self._pending_requests.pop(k, None)
        if not flag:
            # 实测（2026-10-28 WS 伪造实验）：链路全通、代码能捕，flag 为空是唯一
            # 会静默漏掉申请的口子——以前这里直接 return False，一条日志都不留。
            # 现在照样收录（flag 用占位），AI 至少能看见申请，只是处理时会被拦。
            flag = f"noflag:{raw.get('user_id')}:{int(now)}"
            logger.info(f"[QQTools·请求] 收到{rtype}申请但 flag 为空（协议端未给，"
                        f"无法调 API 处理，仅记录）: user={raw.get('user_id')}")
        self._pending_requests[flag] = {
            "kind": rtype,  # friend | group
            "sub_type": str(raw.get("sub_type") or ""),  # 群：add（主动申请）/ invite（被邀请）
            "user_id": str(raw.get("user_id") or ""),
            "nickname": str(raw.get("nickname") or raw.get("invitor_nick") or ""),
            "group_id": str(raw.get("group_id") or ""),
            "comment": str(raw.get("comment") or ""),
            "ts": now,
        }
        logger.info(f"[QQTools·请求] 收到{rtype}申请: user={raw.get('user_id')} "
                    f"group={raw.get('group_id') or '-'} flag={flag[:16]}…")
        self._save_pending_requests()
        return True

    # NapCat 在「加好友验证=允许任何人临时会话」模式下，好友申请不推 OneBot 标准
    # request 事件（实测 2026-10-28 00:56 小明 申请：event_bus 只有一条空 notice，
    # 没有 post_type=request），而是往消息流里塞一条「请求添加你为好友」的文本。
    # 这条文本没有 flag，无法调 set_friend_add_request，但它是「有人想加好友」的
    # 唯一信号——不识别的话 AI 会当没看见，或像实测那样瞎编「我没有权限处理」。
    _SOFT_REQUEST_PATTERNS = (
        "请求添加你为好友", "请求添加你", "申请添加你为好友", "请求加你为好友",
    )

    def _catch_soft_request_text(self, event: AstrMessageEvent) -> bool:
        """识别 NapCat 塞进消息流的「请求添加你为好友」文本，记为无 flag 的软申请。"""
        if not self._cfg_bool("requests.requests_enabled", True):
            return False
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            return False  # 群里出现的同款文本是转发/聊天内容，不认
        text = str(event.message_str or "").strip()
        if not text or not any(p in text for p in self._SOFT_REQUEST_PATTERNS):
            return False
        # 同一个人 1 小时内已有记录（真申请或软申请）就不再堆软条目；真 flag
        # 走 _catch_request_event 的独立 key，不受这里影响。
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            return False
        for v in self._pending_requests.values():
            if str(v.get("user_id")) == uid \
                    and time.time() - float(v.get("ts") or 0) < 3600:
                return False
        self._pending_requests[f"soft:{uid}"] = {
            "kind": "friend",
            "sub_type": "",
            "user_id": uid,
            "nickname": str(event.get_sender_name() or ""),
            "group_id": "",
            "comment": text,
            "soft": True,  # 无 flag：set_friend_add_request 调不了，只能主人手动
            "ts": time.time(),
        }
        logger.info(f"[QQTools·请求] 收到软好友申请（NapCat 文本，无 flag，仅提示用）: "
                    f"user={uid} nickname={event.get_sender_name()}")
        self._save_pending_requests()
        return True

    def _request_rows(self, kind: str = "") -> list[tuple[str, dict]]:
        """按类型列出待处理申请，新的在前。"""
        rows = [(f, v) for f, v in self._pending_requests.items()
                if not kind or v.get("kind") == kind]
        rows.sort(key=lambda x: float(x[1].get("ts") or 0), reverse=True)
        return rows

    async def _resolve_doubt_flag(self, client, uid: str):
        """尝试从 NapCat 的「可疑好友申请」列表里找到 uid 的真实 flag。

        实测场景（2026-10-28）：QQ 设「允许任何人临时会话」时 NapCat 不推
        request 事件，申请被 QQ 归入可疑/过滤列表，但 get_doubt_friends_add_request
        拉得到（带 flag），set_doubt_friends_add_request 能直接处理。
        找不到或接口不支持时返回 None。"""
        try:
            rows = await client.api.call_action(
                "get_doubt_friends_add_request", count=20)
        except Exception as e:
            logger.info(f"[QQTools·请求] 可疑申请接口不可用: {type(e).__name__}: {e}")
            return None
        logger.info(f"[QQTools·请求] 可疑申请接口返回: type={type(rows).__name__} "
                    f"content={str(rows)[:800]}")
        if not rows:
            logger.info("[QQTools·请求] 可疑申请列表为空")
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_uid = str(row.get("user_id") or row.get("uin") or "")
            if row_uid == str(uid):
                flag = str(row.get("flag") or "").strip()
                if flag:
                    return flag
        return None

    def _find_request(self, key: str):
        """按 flag / 列表序号 / QQ 号 定位一条申请。AI 拿长 flag 容易抄错，得让它用序号或 QQ。"""
        key = str(key or "").strip()
        if not key:
            return None, None
        if key in self._pending_requests:
            return key, self._pending_requests[key]
        rows = self._request_rows()
        if key.isdigit() and 1 <= int(key) <= len(rows):
            f, v = rows[int(key) - 1]
            return f, v
        for f, v in rows:  # QQ 号：同一人可能有多条，给最新的那条
            if str(v.get("user_id")) == key:
                return f, v
        return None, None

    @staticmethod
    def _fmt_request(idx: int, flag: str, v: dict) -> str:
        who = v.get("nickname") or v.get("user_id") or "?"
        qq = v.get("user_id") or "?"
        age = ""
        ts = float(v.get("ts") or 0)
        if ts:
            mins = int((time.time() - ts) / 60)
            age = f"{mins}分钟前" if mins < 60 else f"{mins // 60}小时前"
        if v.get("kind") == "group":
            kind_cn = "被邀请进群" if v.get("sub_type") == "invite" else "申请加群"
            head = f"#{idx} [群申请] {kind_cn} 群{v.get('group_id') or '?'}｜{who}({qq})"
        else:
            head = f"#{idx} [好友申请] {who}({qq})"
        if v.get("soft"):
            head += "｜⚠️ 软申请（NapCat 未推申请事件，无 flag）：机器人不能代处理，需主人在 QQ 客户端「新朋友」里手动通过"
        tail = f"｜留言：{v['comment']}" if v.get("comment") else ""
        return f"{head}{tail}｜{age}\n  flag={flag}"

    @filter.after_message_sent()
    async def _cache_on_bot_sent(self, event: AstrMessageEvent):
        """机器人自己发出去的消息也存一份。

        很多协议端的历史接口不含 bot 消息，不存的话 AI 复盘时会以为自己没说过话，
        而且 refresh_messages 也看不到自己刚发的那条。
        """
        try:
            if not self._cfg_bool("cache.cache_bot_messages", True):
                return
            text = ""
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
            for part in chain:
                piece = getattr(part, "text", None)
                if piece:
                    text += str(piece)
            if text.strip():
                self._cache_put(event, text=text, is_bot=True)
        except Exception:
            logger.debug("缓存机器人消息失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 搜人 / 资料                                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _norm_name(value) -> str:
        """人名归一：剥掉不可见字符、统一大小写，用于比对（不影响展示）。"""
        return _INVISIBLE.sub("", str(value or "")).strip().casefold()

    ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "成员"}

    def _member_label(self, member: dict) -> tuple:
        """返回 (展示名带身份, 附加说明)。展示优先级：群名片 > 昵称 > QQ号。"""
        uid = str(member.get("user_id") or "")
        nick = str(member.get("nickname") or "").strip()
        card = str(member.get("card") or "").strip()
        role = self.ROLE_CN.get(str(member.get("role") or ""), "成员")
        display = card or nick or f"用户{uid}"
        label = f"[{role}] {display}"
        bits = []
        if card and nick and card != nick:
            bits.append(f"原名: {nick}")
        title = str(member.get("title") or "").strip()
        if title:
            bits.append(f"头衔: {title}")
        extra = ("  " + "  ".join(bits)) if bits else ""
        return label, extra

    def _match_members(self, members: list, query: str) -> list:
        """在群成员列表里按昵称模糊找人。

        打分优先级：QQ 号全等 > 昵称/群名片全等 > 子串包含 > 字符全覆盖。
        最后一条是为了对付「小豆喵」和「喵小豆」这种顺序不一样、人却认得出来的写法；
        但只在查询词至少两个字时才启用，否则单字会把整个群都匹配进来。
        """
        qn = self._norm_name(query)
        digits = "".join(ch for ch in str(query) if ch.isdigit())
        q_chars = set(qn) - {" "}
        # 至少两个字、且字不重复才启用字符集合匹配：
        # 「豆豆」去重后只剩一个「豆」，拿它当集合会把所有带「豆」的人都捞出来。
        use_set = len(qn) >= 2 and len(q_chars) >= 2
        scored = []
        for m in members:
            if not isinstance(m, dict):
                continue
            uid = str(m.get("user_id") or "")
            nick = self._norm_name(m.get("nickname"))
            card = self._norm_name(m.get("card"))
            score = 0
            if digits and uid == digits:
                score = 100
            elif digits and len(digits) >= 3 and uid.startswith(digits):
                score = 80
            if not score and qn:
                if qn == nick or qn == card:
                    score = 90
                elif qn in nick or qn in card:
                    score = 60
                elif use_set and q_chars <= (set(nick) | set(card)):
                    score = 30
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: (-x[0], str(x[1].get("user_id") or "")))

        hits = []
        for score, m in scored[:10]:
            label, extra = self._member_label(m)
            hits.append({
                "user_id": str(m.get("user_id") or ""),
                "label": label,
                "extra": extra,
                "score": score,
            })
        return hits

    async def _search_members(self, event: AstrMessageEvent, name: str, group_query: str) -> tuple:
        """搜群成员，返回 (hits, 群显示名, 错误文本)。"""
        q = str(name or "").strip()
        if not q:
            return [], "", "请提供要搜索的昵称或 QQ 号。"
        gid, label, err = await self._resolve_group(event, group_query)
        if err:
            return [], "", err
        client = self._get_client(event)
        if client is None:
            return [], label, "当前平台不支持搜人（只支持 OneBot/aiocqhttp 协议端）。"
        try:
            ret = await client.api.call_action("get_group_member_list", group_id=int(gid))
        except Exception as e:
            logger.warning(f"get_group_member_list 失败: {type(e).__name__}: {e}")
            return [], label, f"获取群「{label}」成员列表失败：{type(e).__name__}: {e}"
        members = ret if isinstance(ret, list) else (
            ret.get("data") if isinstance(ret, dict) else None
        )
        if not isinstance(members, list):
            return [], label, f"群「{label}」成员列表返回格式异常（协议端可能不支持）。"
        if not members:
            return [], label, f"群「{label}」成员列表为空。"
        return self._match_members(members, q), label, ""

    async def _search_friends(self, event: AstrMessageEvent, name: str) -> list:
        """在好友列表里按昵称/备注模糊找人。

        私聊场景没有群成员列表，按昵称戳人/点赞只能靠这份好友列表兜底。
        """
        client = self._get_client(event)
        if client is None:
            return []
        try:
            ret = await client.api.call_action("get_friend_list")
        except Exception as e:
            logger.debug(f"get_friend_list 失败: {e}")
            return []
        friends = ret if isinstance(ret, list) else (
            ret.get("data") if isinstance(ret, dict) else None
        )
        if not isinstance(friends, list):
            return []
        qn = self._norm_name(name)
        if not qn:
            return []
        hits = []
        for f in friends:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("user_id") or "")
            nick = self._norm_name(f.get("nickname"))
            remark = self._norm_name(f.get("remark"))
            if qn in nick or qn in remark:
                display = str(f.get("remark") or f.get("nickname") or uid)
                hits.append({"user_id": uid, "label": display,
                             "extra": "", "score": 60})
        return hits[:10]

    async def _resolve_target(
        self, event: AstrMessageEvent, user: str, group_query: str = ""
    ) -> tuple:
        """把「昵称 / QQ号 / 空」解析成 (QQ, 显示名, 错误文本)。

        空表示当前说话的人。昵称先搜群成员，搜不到再退好友列表——私聊里根本没
        有群成员列表可搜。
        """
        raw = str(user or "").strip()
        if not raw:
            uid = str(event.get_sender_id() or "")
            if not uid:
                return "", "", "没拿到当前说话人的 QQ 号。"
            name = ""
            try:
                name = event.get_sender_name() or ""
            except Exception:
                pass
            return uid, (name or uid), ""
        if raw.isdigit():
            return raw, raw, ""

        hits, _label, _err = await self._search_members(event, raw, group_query)
        if not hits:
            hits = await self._search_friends(event, raw)
        if not hits:
            return "", "", f"没找到匹配「{raw}」的人，请确认昵称或直接给 QQ 号。"
        if len(hits) > 1:
            lines = [f"「{raw}」匹配到 {len(hits)} 个人，要操作哪一个？"]
            for i, m in enumerate(hits, 1):
                lines.append(f"{i}. {m['label']} (QQ: {m['user_id']})")
            lines.append("请让用户确认后再带 QQ 号重试。")
            return "", "", "\n".join(lines)
        return str(hits[0]["user_id"]), str(hits[0]["label"]), ""

    @staticmethod
    def _poke_action(res) -> str:
        """从协议端返回里抠出「拍了拍」这种动作文案，抠不到返回空串。

        NapCat 不同版本放在 raw_info / raw_message / action 里，都是可选的，
        所以这里逐个试，全部失败就让调用方用默认文案。
        """
        if not isinstance(res, dict):
            return ""
        for node in (res.get("data"), res):
            if not isinstance(node, dict):
                continue
            for key in ("raw_info", "raw_message", "action", "txt"):
                val = node.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict) and str(item.get("txt") or "").strip():
                            return str(item["txt"]).strip()[:8]
                elif isinstance(val, str) and val.strip():
                    return val.strip()[:8]
        return ""

    @staticmethod
    def _bkn(skey: str) -> int:
        """QQ 空间的 g_tk：skey 算出来的校验码，空间接口必带。"""
        h = 5381
        for ch in str(skey or "").lstrip("@"):
            h += (h << 5) + ord(ch)
        return h & 0x7FFFFFFF

    async def _fetch_qzone_detail(self, event: AstrMessageEvent, qq: str) -> dict:
        """借 QQ 空间登录态补资料卡字段：生日、公司、职业、省市、学校、签名。

        协议端的 get_stranger_info 根本不返回这些，只能走空间的
        cgi_userinfo_get_all。登录态从 NapCat 的 get_cookies 拿；拿不到就
        返回空字典，调用方当"没这个字段"处理，绝不因为这一项失败影响整张资料卡。
        """
        if not self._cfg_bool("profile.show_qzone_detail", True):
            return {}
        client = self._get_client(event)
        if client is None:
            return {}
        try:
            cred = await client.api.call_action(
                "get_cookies", domain="user.qzone.qq.com"
            )
        except Exception as e:
            logger.debug(f"get_cookies 不可用（协议端不支持）: {type(e).__name__}: {e}")
            return {}

        node = cred.get("data") if isinstance(cred, dict) else None
        if not isinstance(node, dict):
            node = cred if isinstance(cred, dict) else {}
        cookies = str(node.get("cookies") or node.get("cookie") or "")
        if not cookies:
            logger.debug("get_cookies 返回里没有 cookies 字段，跳过空间资料")
            return {}

        # p_skey 优先（空间接口认它），没有就用 skey
        skey = ""
        for seg in cookies.split(";"):
            key, _, val = seg.strip().partition("=")
            if key == "skey" and val:
                skey = val
            elif key == "p_skey" and val:
                skey = val
                break
        try:
            bkn = int(node.get("bkn") or self._bkn(skey))
        except (TypeError, ValueError):
            bkn = self._bkn(skey)

        self_id = str(event.get_self_id() or "")
        url = (
            "https://h5.qzone.qq.com/proxy/domain/base.qzone.qq.com/cgi-bin/user/"
            f"cgi_userinfo_get_all?uin={qq}&vuin={self_id}&g_tk={bkn}&fupdate=1"
        )
        headers = {
            "Cookie": cookies,
            "Referer": f"https://user.qzone.qq.com/{self_id}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        }
        text = ""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    text = await resp.text()
        except Exception as e:
            logger.debug(f"空间资料请求失败: {type(e).__name__}: {e}")
            return {}

        # 返回是 JSONP（_Callback({...});），剥壳后按 JSON 解析
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            logger.debug("空间资料返回里没有 JSON 片段")
            return {}
        try:
            payload = json.loads(m.group(0))
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        # 没登录态时常见返回 {"code": -3000} 或空对象
        if not payload or ("code" in payload and not payload.get("nickname")):
            logger.debug(f"空间资料拿不到（可能是登录态失效）: {str(payload)[:120]}")
            return {}
        logger.debug(f"空间资料字段: {[k for k, v in payload.items() if v not in (None, '', 0)]}")
        return payload

    @staticmethod
    def _pick_background(info: dict) -> str:
        """从资料字典里找背景图地址。不同协议端/版本的键名不一样，逐个试。"""
        keys = ("bgimg", "background", "background_url", "bgUrl", "bg", "cover",
                "backgroundUrl", "cover_url")
        for key in keys:
            val = info.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
            if isinstance(val, dict):
                for sub in ("url", "img", "pic", "src"):
                    inner = val.get(sub)
                    if isinstance(inner, str) and inner.startswith("http"):
                        return inner
        return ""

    async def _fetch_profile(self, event: AstrMessageEvent, qq: str, gid: str) -> dict:
        """查资料：群内优先 get_group_member_info，拿不到再退 get_stranger_info。"""
        client = self._get_client(event)
        if client is None:
            return {}
        try:
            uid = int(qq)
        except (TypeError, ValueError):
            return {}
        info = None
        if gid:
            try:
                info = await client.api.call_action(
                    "get_group_member_info", group_id=int(gid), user_id=uid
                )
            except Exception as e:
                logger.debug(f"get_group_member_info 失败（{qq}@群{gid}）: {e}")
        if not isinstance(info, dict) or not info:
            try:
                info = await client.api.call_action("get_stranger_info", user_id=uid)
            except Exception as e:
                logger.warning(f"get_stranger_info 失败（{qq}）: {type(e).__name__}: {e}")
                return {}
        if not isinstance(info, dict):
            return {}
        data = info.get("data")
        if not isinstance(data, dict):
            data = info

        # 群成员信息里没有签名/VIP/生日/公司这些，stranger_info 才有；补一层
        if data.get("card") or data.get("role"):
            extra = {}
            try:
                extra = await client.api.call_action("get_stranger_info", user_id=uid)
            except Exception as e:
                logger.debug(f"补 get_stranger_info 失败（{qq}）: {e}")
            if isinstance(extra, dict):
                ed = extra.get("data")
                if isinstance(ed, dict):
                    for key, val in ed.items():
                        if val not in (None, "", 0, "0") and not data.get(key):
                            data[key] = val

        # 空间深度资料（生日/公司/职业/省市）：协议端不返回，只能另查。
        # 拿不到就当没有，不能因为它把整张资料卡搞挂。
        detail = await self._fetch_qzone_detail(event, qq)
        for key, val in (detail or {}).items():
            if val in (None, "", 0, "0"):
                continue
            if not data.get(key):
                data[key] = val
        return data

    @staticmethod
    def _fmt_tags(val) -> str:
        """个性标签可能是列表也可能是逗号串，统一成人能读的一行。"""
        if isinstance(val, (list, tuple, set)):
            items = [str(x).strip() for x in val if str(x).strip()]
            return "、".join(items)[:60]
        text = str(val or "").strip()
        return text[:60]

    @staticmethod
    def _fmt_birthday(info: dict) -> str:
        """生日：空间接口给的 birthday 常是 "3-15"，配合 birthyear 才完整。"""
        bd = str(info.get("birthday") or "").strip()
        if not bd or bd in ("0", "0-0"):
            return ""
        year = info.get("birthyear")
        try:
            y = int(year)
        except (TypeError, ValueError):
            y = 0
        if y and str(y) not in bd:
            return f"{y}-{bd}"
        return bd

    def _render_profile(self, info: dict) -> str:
        """按配置拼资料卡。每个字段都能单独关，关到没内容时给一句兜底。"""
        uid = str(info.get("user_id") or "")
        nick = str(info.get("nickname") or "").strip()
        card = str(info.get("card") or "").strip()
        show_nick = self._cfg_bool("profile.show_nickname", True)
        show_qq = self._cfg_bool("profile.show_qq", True)
        lines = []
        if card and self._cfg_bool("profile.show_card", True):
            lines.append(f"群名片：{card}")
        if show_nick and nick:
            lines.append(f"昵称：{nick}")
        elif nick and not card:
            # 昵称开关关了、又没有群名片，那这人不就没名可叫了——留个兜底
            lines.append(f"昵称：{nick}")
        if show_qq and uid:
            lines.append(f"QQ：{uid}")

        # 个性签名：stranger_info 的 long_nick，空间接口叫 signature
        sig = str(info.get("long_nick") or info.get("signature") or "").strip()
        if sig:
            lines.append(f"个性签名：{sig}")

        role = self.ROLE_CN.get(str(info.get("role") or ""), "")
        if role:
            lines.append(f"群身份：{role}")

        if self._cfg_bool("profile.show_level", True):
            # qqLevel 是 QQ 等级，level 是群等级，两个概念，都会出现
            qlv = info.get("qqLevel")
            if qlv not in (None, "", 0, "0"):
                lines.append(f"QQ 等级：{qlv}")
            level = info.get("level")
            if level not in (None, "", 0, "0"):
                lines.append(f"群等级：{level}")
            title = str(info.get("title") or "").strip()
            if title:
                lines.append(f"群头衔：{title}")
            bits = []
            if info.get("is_vip") or info.get("is_years_vip"):
                vip = f"VIP{info.get('vip_level') or ''}"
                if info.get("is_years_vip"):
                    vip = "年费" + vip
                bits.append(vip)
            days = info.get("login_days")
            if days not in (None, "", 0, "0"):
                bits.append(f"登录 {days} 天")
            if bits:
                lines.append("｜".join(bits))

        if self._cfg_bool("profile.show_detail", True):
            bits = []
            sex = str(info.get("sex") or "").strip()
            sex_map = {"male": "男", "female": "女"}
            sex = sex_map.get(sex, sex)
            if sex and sex not in ("unknown", "未知"):
                bits.append(f"性别 {sex}")
            age = info.get("age")
            if age not in (None, "", 0, "0", "未知"):
                bits.append(f"年龄 {age}")
            area = str(info.get("area") or "").strip()
            if area:
                bits.append(f"地区 {area}")
            if bits:
                lines.append("｜".join(bits))

        # 空间深度资料：协议端不返回，靠 _fetch_qzone_detail 尽力补
        if self._cfg_bool("profile.show_qzone_detail", True):
            bd = self._fmt_birthday(info)
            if bd:
                lines.append(f"生日：{bd}")
            loc = " ".join(
                str(info.get(k) or "").strip()
                for k in ("country", "province", "city")
                if str(info.get(k) or "").strip()
            )
            if loc:
                lines.append(f"所在地：{loc}")
            career = str(info.get("career") or "").strip()
            if career:
                lines.append(f"职业：{career}")
            company = str(info.get("company") or "").strip()
            if company:
                lines.append(f"公司：{company}")
            school = str(info.get("school") or info.get("college") or "").strip()
            if school:
                lines.append(f"学校：{school}")
            tags = self._fmt_tags(info.get("labels") or info.get("tags"))
            if tags:
                lines.append(f"个性标签：{tags}")

        if not lines:
            return "这个人的资料全是关掉的字段，没有可说的。"
        return "用户资料：\n" + "\n".join(lines)

    async def _send_avatar(self, event: AstrMessageEvent, url: str) -> bool:
        """把头像直接发成图片。失败返回 False，调用方退回文本链接。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain
        except ImportError:
            return False
        try:
            await event.send(MessageChain([Image.fromURL(url)]))
            return True
        except Exception as e:
            logger.warning(f"发送头像失败: {type(e).__name__}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # 工具函数                                                            #
    # ------------------------------------------------------------------ #
    def _normalize_count(self, value) -> int:
        """将 LLM 传入的条数参数规范化为 [1, max_count] 内的整数。"""
        default = int(self._cfg("read.default_count", 20))
        max_count = int(self._cfg("read.max_count", 100))
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return max(1, min(n, max_count))

    @staticmethod
    def _get_aiocqhttp_event_cls():
        """按文档路径导入 AiocqhttpMessageEvent，兼容不同版本目录结构。"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            return AiocqhttpMessageEvent
        except ImportError:
            pass
        try:
            from astrbot.platforms.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            return AiocqhttpMessageEvent
        except ImportError:
            return None

    @staticmethod
    def _extract_messages(ret) -> list:
        """兼容多种返回结构：{data:{messages:[...]}} / {messages:[...]} / 直接列表。"""
        if isinstance(ret, list):
            return ret
        if not isinstance(ret, dict):
            return []
        data = ret.get("data", ret)
        if isinstance(data, dict):
            msgs = data.get("messages", [])
        elif isinstance(data, list):
            msgs = data
        else:
            msgs = []
        return msgs if isinstance(msgs, list) else []

    def _format_messages(self, messages: list) -> str:
        """将 OneBot 消息对象列表格式化为可读文本（按时间升序）。"""
        messages = sorted(
            [m for m in messages if isinstance(m, dict)],
            key=lambda m: m.get("time") or 0,
        )
        show_mid = self._cfg_bool("search.show_message_id", True)
        lines = []
        for m in messages:
            ts = m.get("time")
            if ts:
                time_str = datetime.datetime.fromtimestamp(int(ts)).strftime(
                    "%m-%d %H:%M:%S"
                )
            else:
                time_str = "--:--:--"
            sender = m.get("sender") or {}
            uid = str(sender.get("user_id") or m.get("user_id") or "?")
            nickname = str(sender.get("card") or sender.get("nickname") or f"用户{uid}")
            content = self._segments_to_text(m.get("message"))
            max_len = int(self._cfg("read.max_content_length", 200))
            if len(content) > max_len:
                content = content[:max_len] + "…"
            line = f"[{time_str}] {nickname}({uid}): {content}"
            # message_id 是引用、撤回、查详情的前提；只做总结时可以关掉省 token
            mid = str(m.get("message_id") or "")
            if show_mid and mid:
                line += f" (ID: {mid})"
            lines.append(line)

        text = "\n".join(lines)
        # 结果过长时丢弃最早记录，保留最近的消息
        while len(text) > _MAX_TOOL_RESULT_CHARS and len(lines) > 1:
            lines.pop(0)
            text = "\n".join(lines)
        return text

    @staticmethod
    def _segments_to_text(segments) -> str:
        """将 OneBot 消息段列表转换为纯文本表示。"""
        if isinstance(segments, str):
            return segments
        if not isinstance(segments, list):
            return ""
        parts = []
        for seg in segments:
            if not isinstance(seg, dict):
                parts.append(str(seg))
                continue
            t = seg.get("type", "")
            d = seg.get("data") or {}
            if t == "text":
                parts.append(str(d.get("text", "")))
            elif t == "at":
                qq = d.get("qq", "")
                parts.append("@" + ("全体成员" if str(qq) in ("0", "") else str(qq)))
            elif t == "face":
                parts.append("[表情]")
            elif t == "image":
                parts.append("[图片]")
            elif t == "record":
                parts.append("[语音]")
            elif t == "video":
                parts.append("[视频]")
            elif t == "reply":
                parts.append("[回复消息]")
            elif t == "forward":
                parts.append("[合并转发]")
            elif t == "node":
                parts.append("[转发节点]")
            else:
                parts.append(f"[{t}]")
        return "".join(parts).strip()
