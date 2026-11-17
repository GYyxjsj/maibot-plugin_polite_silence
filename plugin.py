"""礼貌性沉默（MaiBot 版）插件入口。

AI 通过 ``ignore_qq_user`` 工具把骚扰者加入沉默名单，插件在设定时长内
拦截该 QQ 号的消息；拦截、工具与管理指令均通过 MaiBot 官方接口完成，
不修改主程序。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json
import re
import time

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .config import PoliteSilenceSettings
from .helpers import clamp_ignore_minutes, parse_manual_ignore_args

_GLOBAL_SCOPE = "global"
_BAN_STORE_FILENAME = "bans.json"
_BAN_STORE_VERSION = 1
_RECENT_LIMIT = 50
_MAX_SESSIONS = 256
_ADMIN_COMMAND_PATTERN = re.compile(
    r"^/(?:unignore(?:\s+\S+)?|ignore(?:\s+\S+(?:\s+\S+)?)?|ignorelist)$"
)


@dataclass(slots=True)
class SeenMessage:
    """用于解析昵称对应 QQ 号的一条入站消息。"""

    message_id: str
    user_id: str
    name: str
    timestamp: float


@dataclass(slots=True)
class SessionState:
    """一个聊天流的参与者与上下文缓存。"""

    is_group: bool
    group_id: str
    group_name: str
    platform: str
    last_seen: float
    recent: deque[SeenMessage] = field(default_factory=lambda: deque(maxlen=_RECENT_LIMIT))


@dataclass(slots=True)
class BanRecord:
    """一条拒答记录。"""

    expire_at: float
    name: str
    index: int
    scope_label: str
    permanent: bool = False


class PoliteSilencePlugin(MaiBotPlugin):
    """礼貌性沉默插件。"""

    config_model = PoliteSilenceSettings

    def __init__(self) -> None:
        super().__init__()
        # 键为 (QQ号, 作用域)，作用域为 "global" 或聊天流 session_id
        self._bans: dict[tuple[str, str], BanRecord] = {}
        self._sessions: dict[str, SessionState] = {}

    # ── 生命周期 ───────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """读取配置、恢复本地拒答名单并输出生效摘要。"""
        self._load_bans()
        config = self.config.silence
        self._log_info(
            f"[礼貌性沉默] 已加载: 时长={config.min_ignore_minutes}~{config.max_ignore_minutes}分钟, "
            f"全局拒答={'开' if config.is_global_ignore else '关'}, "
            f"白名单={len(config.whitelist_users)}人, "
            f"恢复拒答记录={len(self._bans)}条"
        )

    async def on_unload(self) -> None:
        """卸载时保存拒答名单并清空内存状态。"""
        self._save_bans()
        self._bans.clear()
        self._sessions.clear()
        self._log_info("[礼貌性沉默] 已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新后输出新的生效摘要。"""
        del config_data
        del version
        if scope != "self":
            return None
        config = self.config.silence
        self._log_info(
            f"[礼貌性沉默] 配置已热更新: "
            f"全局拒答={'开' if config.is_global_ignore else '关'}, "
            f"白名单={len(config.whitelist_users)}人, "
            f"日志={'开' if self.config.plugin.enable_log else '关'}"
        )
        return None

    # ── Hook: 拒答期内拦截入站消息 ────────────────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="polite_silence_interceptor",
        description="拒答期内忽略指定用户的消息，并记录会话参与者供昵称解析使用",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def intercept_banned_user(self, **kwargs: Any) -> dict[str, Any]:
        """在消息进入预处理前拦截拒答用户的普通消息。"""
        base = {"action": "continue", "modified_kwargs": kwargs}
        if not self.config.plugin.enabled:
            return base

        message = kwargs.get("message")
        if not isinstance(message, dict) or bool(message.get("is_notify")):
            return base

        session_id, user_id, name = self._extract_sender(message)
        if not session_id or not user_id:
            return base

        # 适配器把机器人自身的消息回送时不参与拒答，避免 AI 误伤自己的账号
        account_id = str(message.get("account_id") or "").strip()
        if account_id and account_id == user_id:
            return base

        # 管理指令不经过模型，保留放行，避免管理员被拒答后无法自行解封
        if self._is_admin_command(message):
            return base

        if self._is_whitelisted(user_id):
            self._remember_message(message, session_id, user_id, name)
            return base

        ban = self._find_active_ban(user_id, session_id)
        if ban is not None:
            remaining = "永久" if ban.permanent else f"{max(0, int((ban.expire_at - time.time()) / 60))} 分钟"
            self._log_info(
                f"[礼貌性沉默] 忽略用户 {name}({user_id}) 的消息 | "
                f"范围={ban.scope_label} | 剩余 {remaining}"
            )
            return {"action": "abort"}

        self._remember_message(message, session_id, user_id, name)
        state = self._sessions.get(session_id)
        self._log_debug(
            f"[礼貌性沉默] 入站消息 | 会话={self._scope_text(state)} | "
            f"发送者={name}({user_id}) | 未命中拒答"
        )
        return base

    # ── Tool: 让模型主动沉默用户 ──────────────────────────────────────

    @Tool(
        "ignore_qq_user",
        description=(
            "把指定 QQ 用户加入沉默名单，在设定时长内不再接收和处理该用户的消息。"
            "当某用户持续骚扰、辱骂或提出不当要求，你决定停止回应时使用。"
            "目标可以填 QQ 号，也可以填当前会话中该用户的昵称；省略则默认当前发言者。"
            "该操作仅在内部生效，不要在回复里向用户解释或复述。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="要忽略的 QQ 号或当前会话中的用户昵称；留空表示当前发言者",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="minutes",
                param_type=ToolParamType.INTEGER,
                description="忽略时长（分钟），-1 表示永久；留空或 0 表示使用配置的最短时长",
                required=False,
                default=0,
            ),
        ],
        core_tool=True,
        visibility="visible",
    )
    async def tool_ignore_user(self, target: str = "", minutes: int = 0, **kwargs: Any) -> dict[str, Any]:
        """处理模型发起的沉默请求。"""
        tool_name = "ignore_qq_user"
        if not self.config.plugin.enabled:
            return {"name": tool_name, "content": "礼貌性沉默插件未启用，无法忽略用户。"}

        stream_id = str(kwargs.get("stream_id") or "").strip()
        state = self._sessions.get(stream_id)
        target_text = str(target or "").strip().strip("[]")
        if not target_text:
            target_text = str(kwargs.get("user_id") or "").strip()
        target_id = self._resolve_target_qq(target_text, state)
        if not target_id:
            return {
                "name": tool_name,
                "content": f"无法确定目标「{target_text or '当前发言者'}」的 QQ 号，请提供明确的 QQ 号。",
            }

        config = self.config.silence
        if self._is_whitelisted(target_id):
            return {"name": tool_name, "content": f"{target_id} 在白名单中，无法忽略。"}
        if not config.is_global_ignore and not stream_id:
            return {"name": tool_name, "content": "当前会话缺少 stream_id，无法建立局部拒答记录。"}

        try:
            requested_minutes = int(minutes)
        except (TypeError, ValueError):
            requested_minutes = 0
        permanent = requested_minutes == -1
        if permanent:
            expire_at = float("inf")
            duration_text = "永久"
        else:
            applied_minutes = clamp_ignore_minutes(
                requested_minutes or None,
                config.min_ignore_minutes,
                config.max_ignore_minutes,
            )
            expire_at = time.time() + applied_minutes * 60
            duration_text = f"{applied_minutes} 分钟"

        scope_key = _GLOBAL_SCOPE if config.is_global_ignore else stream_id
        scope_label = "全局" if config.is_global_ignore else (self._scope_text(state) if state else "当前聊天流")
        name = self._resolve_user_name(state, target_id)
        self._bans[(target_id, scope_key)] = BanRecord(
            expire_at=expire_at,
            name=name,
            index=0,
            scope_label=scope_label,
            permanent=permanent,
        )
        self._rebuild_indexes()
        self._save_bans()
        self._log_info(
            f"[礼貌性沉默] 模型拒答 {name}({target_id}) | "
            f"范围={scope_label} | 时长={duration_text}"
        )
        return {
            "name": tool_name,
            "content": f"已忽略 {name}({target_id}) | 范围: {scope_label} | 时长: {duration_text}",
        }

    # ── 管理指令 ──────────────────────────────────────────────────────

    @Command(
        name="ignore",
        pattern=r"^/ignore(?:\s+(?P<target>\S+))?(?:\s+(?P<minutes>\S+))?$",
        description="手动让 AI 忽略指定 QQ 号一段时间",
        permission="operator",
    )
    async def cmd_ignore(self, **kwargs: Any) -> tuple[bool, str, int]:
        """按 QQ 号与可选分钟数直接写入拒答记录。"""
        if not self.config.plugin.enabled:
            await self._send_command_text(kwargs, "礼貌性沉默插件未启用。")
            return False, "插件未启用", 2

        matched_groups = kwargs.get("matched_groups")
        target_raw = ""
        minutes_raw = ""
        if isinstance(matched_groups, dict):
            target_raw = matched_groups.get("target") or ""
            minutes_raw = matched_groups.get("minutes") or ""

        config = self.config.silence
        target, minutes, error = parse_manual_ignore_args(
            target_raw,
            minutes_raw,
            config.min_ignore_minutes,
            config.max_ignore_minutes,
        )
        if error == "missing_target":
            reply = "用法：/ignore <QQ号> [分钟数]"
            await self._send_command_text(kwargs, reply)
            return False, "缺少目标 QQ 号", 2
        if error == "invalid_target":
            reply = "目标必须是 QQ 号（纯数字）。用法：/ignore <QQ号> [分钟数]"
            await self._send_command_text(kwargs, reply)
            return False, "目标不是 QQ 号", 2
        if error == "invalid_minutes":
            reply = "分钟数必须是正整数或 -1。用法：/ignore <QQ号> [分钟数]"
            await self._send_command_text(kwargs, reply)
            return False, "分钟数无效", 2

        if self._is_whitelisted(target):
            reply = f"目标 {target} 在白名单中，无法拒答。"
            await self._send_command_text(kwargs, reply)
            return False, "目标在白名单中", 2

        message = kwargs.get("message")
        account_id = str(message.get("account_id") or "").strip() if isinstance(message, dict) else ""
        if account_id and target == account_id:
            reply = f"不能忽略机器人自身（{target}）。"
            await self._send_command_text(kwargs, reply)
            return False, "不能忽略机器人自身", 2

        session_id = str(kwargs.get("stream_id") or "").strip()
        if not config.is_global_ignore and not session_id:
            reply = "当前会话缺少 stream_id，无法建立局部拒答记录。"
            await self._send_command_text(kwargs, reply)
            return False, "缺少会话标识", 2

        state = self._sessions.get(session_id) if session_id else None
        scope_key = _GLOBAL_SCOPE if config.is_global_ignore else session_id
        scope_label = "全局" if config.is_global_ignore else (self._scope_text(state) if state else "当前聊天流")
        name = self._resolve_user_name(state, target)
        permanent = minutes == -1
        self._bans[(target, scope_key)] = BanRecord(
            expire_at=float("inf") if permanent else time.time() + minutes * 60,
            name=name,
            index=0,
            scope_label=scope_label,
            permanent=permanent,
        )
        self._rebuild_indexes()
        self._save_bans()
        duration_text = "永久" if permanent else f"{minutes} 分钟"
        self._log_info(
            f"[礼貌性沉默] 手动拒答 {name}({target}) | "
            f"范围={scope_label} | 时长={duration_text}"
        )
        reply = f"已让 AI 忽略 {name}({target}) | 范围: {scope_label} | 时长: {duration_text}"
        await self._send_command_text(kwargs, reply)
        return True, reply, 2

    @Command(
        name="unignore",
        pattern=r"^/unignore(?:\s+(?P<target>\S+))?$",
        description="解除 AI 对指定 QQ 号或序号的全部拒答记录",
        permission="operator",
    )
    async def cmd_unignore(self, **kwargs: Any) -> tuple[bool, str, int]:
        """按 QQ 号或列表序号解除拒答。"""
        if not self.config.plugin.enabled:
            await self._send_command_text(kwargs, "礼貌性沉默插件未启用。")
            return False, "插件未启用", 2

        matched_groups = kwargs.get("matched_groups")
        target = ""
        if isinstance(matched_groups, dict):
            target = str(matched_groups.get("target") or "").strip()
        target = target.strip("[]")
        if not target:
            await self._send_command_text(kwargs, "用法：/unignore <QQ号或序号>")
            return False, "缺少目标 QQ 号", 2

        self._rebuild_indexes()
        keys_to_delete = [
            key
            for key, record in self._bans.items()
            if key[0] == target or str(record.index) == target
        ]
        if not keys_to_delete:
            await self._send_command_text(kwargs, f"目标 {target} 当前不在拒答名单中。")
            return False, "目标不在拒答名单中", 2

        for key in keys_to_delete:
            self._bans.pop(key, None)
        self._rebuild_indexes()
        self._save_bans()
        self._log_info(f"[礼貌性沉默] 已解除对 {target} 的拒答")
        reply = f"已恢复与目标 {target} 的对话。"
        await self._send_command_text(kwargs, reply)
        return True, reply, 2

    @Command(
        name="ignorelist",
        pattern=r"^/ignorelist$",
        description="查看当前被 AI 拒绝回应的用户列表",
        permission="operator",
    )
    async def cmd_ignorelist(self, **kwargs: Any) -> tuple[bool, str, int]:
        """列出有效拒答记录、作用域与剩余时长。"""
        if not self.config.plugin.enabled:
            await self._send_command_text(kwargs, "礼貌性沉默插件未启用。")
            return False, "插件未启用", 2

        self._rebuild_indexes()
        if not self._bans:
            reply = "当前没有正在被拒答的用户。"
            await self._send_command_text(kwargs, reply)
            return True, reply, 2

        now = time.time()
        lines = ["当前被 AI 拒绝回应的成员名单："]
        for (target_id, _scope_key), record in self._bans.items():
            remaining = "永久" if record.permanent else f"{max(0, int((record.expire_at - now) / 60))} 分钟"
            lines.append(
                f"[{record.index}] {record.name} ({target_id}) | "
                f"范围: {record.scope_label} | 剩余: {remaining}"
            )
        reply = "\n".join(lines)
        await self._send_command_text(kwargs, reply)
        return True, reply, 2

    # ── 内部工具 ──────────────────────────────────────────────────────

    def _log_info(self, message: str) -> None:
        """按配置输出普通日志。"""
        if self.config.plugin.enable_log:
            self.ctx.logger.info(message)

    def _log_debug(self, message: str) -> None:
        """按配置输出调试日志。"""
        if self.config.plugin.enable_log:
            self.ctx.logger.debug(message)

    async def _send_command_text(self, kwargs: dict[str, Any], text: str) -> bool:
        """把指令结果发送到当前聊天流。"""
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            self.ctx.logger.warning("[礼貌性沉默] 指令缺少 stream_id，无法发送结果")
            return False
        try:
            sent = await self.ctx.send.text(text=text, stream_id=stream_id)
        except Exception as exc:
            self.ctx.logger.error(f"[礼貌性沉默] 指令结果发送异常: {exc}")
            return False
        if not sent:
            self.ctx.logger.warning("[礼貌性沉默] 指令结果发送失败（send.text 返回 False）")
        return bool(sent)

    @staticmethod
    def _extract_sender(message: dict[str, Any]) -> tuple[str, str, str]:
        """从序列化消息中提取会话 ID、发送人 ID 与展示名。"""
        session_id = str(message.get("session_id") or "").strip()
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return session_id, "", ""
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return session_id, "", ""
        user_id = str(user_info.get("user_id") or "").strip()
        name = str(user_info.get("user_cardname") or user_info.get("user_nickname") or user_id).strip()
        return session_id, user_id, name

    def _remember_message(
        self,
        message: dict[str, Any],
        session_id: str,
        user_id: str,
        name: str,
    ) -> None:
        """记录会话元信息与最近的发送人，供昵称解析使用。"""
        now = time.time()
        message_info = message.get("message_info")
        group_info = message_info.get("group_info") if isinstance(message_info, dict) else None
        is_group = isinstance(group_info, dict) and bool(str(group_info.get("group_id") or "").strip())
        group_id = str(group_info.get("group_id") or "").strip() if is_group else ""
        group_name = str(group_info.get("group_name") or "").strip() if is_group else ""
        platform = str(message.get("platform") or "").strip()

        state = self._sessions.get(session_id)
        if state is None:
            state = SessionState(
                is_group=is_group,
                group_id=group_id,
                group_name=group_name,
                platform=platform,
                last_seen=now,
            )
            self._sessions[session_id] = state
            self._prune_sessions()
        else:
            state.is_group = is_group
            state.group_id = group_id
            state.group_name = group_name
            state.platform = platform
            state.last_seen = now

        message_id = str(message.get("message_id") or "").strip()
        if state.recent and state.recent[-1].message_id == message_id:
            return
        state.recent.append(
            SeenMessage(
                message_id=message_id,
                user_id=user_id,
                name=name or user_id,
                timestamp=now,
            )
        )

    def _prune_sessions(self) -> None:
        """会话缓存超过上限时丢弃最久未活动的记录。"""
        if len(self._sessions) <= _MAX_SESSIONS:
            return
        overflow = len(self._sessions) - _MAX_SESSIONS
        stale = sorted(self._sessions.items(), key=lambda item: item[1].last_seen)[:overflow]
        for session_id, _state in stale:
            self._sessions.pop(session_id, None)

    def _whitelist_ids(self) -> set[str]:
        """返回规范化后的白名单 QQ 号集合。"""
        return {
            str(user_id).strip()
            for user_id in self.config.silence.whitelist_users
            if str(user_id).strip()
        }

    def _is_whitelisted(self, user_id: str) -> bool:
        """判断用户是否在白名单中。"""
        return user_id in self._whitelist_ids()

    def _find_active_ban(self, user_id: str, session_id: str) -> BanRecord | None:
        """查找全局或当前会话的有效拒答记录，顺手清理已过期记录。"""
        now = time.time()
        for scope_key in (_GLOBAL_SCOPE, session_id):
            record = self._bans.get((user_id, scope_key))
            if record is None:
                continue
            if record.permanent or record.expire_at > now:
                return record
            self._bans.pop((user_id, scope_key), None)
            self._log_info(
                f"[礼貌性沉默] 用户 {record.name}({user_id}) 的拒答已到期，自动恢复回应"
            )
        return None

    def _rebuild_indexes(self) -> None:
        """移除过期记录，并让有效记录从 1 开始连续编号。"""
        now = time.time()
        valid: dict[tuple[str, str], BanRecord] = {}
        for key, record in self._bans.items():
            if record.permanent or record.expire_at > now:
                record.index = len(valid) + 1
                valid[key] = record
        self._bans = valid

    def _ban_store_path(self) -> Path:
        """返回拒答名单的本地存储路径。"""
        return Path(self.ctx.paths.data_dir) / _BAN_STORE_FILENAME

    def _save_bans(self) -> None:
        """把当前有效拒答记录原子写入插件数据目录。"""
        now = time.time()
        bans: list[dict[str, Any]] = []
        for (user_id, scope_key), record in self._bans.items():
            if not record.permanent and record.expire_at <= now:
                continue
            bans.append(
                {
                    "user_id": user_id,
                    "scope": scope_key,
                    "name": record.name,
                    "scope_label": record.scope_label,
                    "expire_at": None if record.permanent else record.expire_at,
                    "permanent": record.permanent,
                }
            )

        payload = {"version": _BAN_STORE_VERSION, "saved_at": now, "bans": bans}
        path = self._ban_store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(path.name + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            self.ctx.logger.warning(f"[礼貌性沉默] 拒答名单保存失败: {exc}")

    def _load_bans(self) -> None:
        """从插件数据目录恢复未过期的拒答记录。"""
        path = self._ban_store_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.ctx.logger.warning(f"[礼貌性沉默] 拒答名单读取失败: {exc}")
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.ctx.logger.warning(f"[礼貌性沉默] 拒答名单格式损坏，已忽略: {exc}")
            return

        raw_bans = payload.get("bans") if isinstance(payload, dict) else None
        if not isinstance(raw_bans, list):
            self.ctx.logger.warning("[礼貌性沉默] 拒答名单缺少 bans 列表，已忽略")
            return

        now = time.time()
        loaded = 0
        for item in raw_bans:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id") or "").strip()
            scope_key = str(item.get("scope") or "").strip()
            if not user_id.isdigit() or not scope_key:
                continue
            permanent = item.get("permanent") is True
            if permanent:
                expire_at = float("inf")
            else:
                try:
                    expire_at = float(item.get("expire_at"))
                except (TypeError, ValueError):
                    continue
                if expire_at <= now:
                    continue
            name = str(item.get("name") or user_id).strip() or user_id
            scope_label = str(
                item.get("scope_label") or ("全局" if scope_key == _GLOBAL_SCOPE else "聊天流")
            ).strip()
            self._bans[(user_id, scope_key)] = BanRecord(
                expire_at=expire_at,
                name=name,
                index=0,
                scope_label=scope_label,
                permanent=permanent,
            )
            loaded += 1

        self._rebuild_indexes()
        if loaded:
            self._log_info(f"[礼貌性沉默] 已从本地恢复 {loaded} 条拒答记录")

    @staticmethod
    def _extract_plain_text(message: dict[str, Any]) -> str:
        """从序列化消息中尽量取出纯文本。"""
        processed = message.get("processed_plain_text")
        if isinstance(processed, str) and processed.strip():
            return processed
        raw_message = message.get("raw_message")
        components = raw_message.get("components") if isinstance(raw_message, dict) else raw_message
        if not isinstance(components, list):
            return ""
        parts = [
            str(component.get("data") or "")
            for component in components
            if isinstance(component, dict) and str(component.get("type") or "").lower() == "text"
        ]
        return " ".join(parts)

    def _is_admin_command(self, message: dict[str, Any]) -> bool:
        """判断消息是否为插件自身的解封/名单指令。"""
        text = self._extract_plain_text(message).strip()
        return bool(text and _ADMIN_COMMAND_PATTERN.fullmatch(text))

    @staticmethod
    def _resolve_target_qq(target_text: str, state: SessionState | None) -> str:
        """把 QQ 号或昵称解析为 QQ 号。"""
        candidate = target_text.strip().strip("[]")
        if not candidate:
            return ""
        if candidate.isdigit():
            return candidate
        if state is not None:
            for seen in reversed(state.recent):
                if seen.name == candidate:
                    return seen.user_id
        match = re.search(r"\d{5,}", candidate)
        return match.group(0) if match else ""

    @staticmethod
    def _scope_text(state: SessionState | None) -> str:
        """把会话状态转换为可读的作用域名称。"""
        if state is None:
            return "未知会话"
        if not state.is_group:
            return "私聊"
        if state.group_name:
            return f"群聊 {state.group_name}（{state.group_id}）"
        if state.group_id:
            return f"群聊 {state.group_id}"
        return "群聊"

    @staticmethod
    def _resolve_user_name(state: SessionState | None, target_id: str) -> str:
        """从最近发言记录中查找目标用户的展示名。"""
        if state is not None:
            for seen in reversed(state.recent):
                if seen.user_id == target_id:
                    return seen.name
        return target_id


def create_plugin() -> PoliteSilencePlugin:
    """创建礼貌性沉默插件实例。"""
    return PoliteSilencePlugin()
