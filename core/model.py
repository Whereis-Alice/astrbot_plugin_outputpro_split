from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from pydantic import BaseModel, Field

REPLY_EVENT_SEQUENCE_EXTRA = "_outputpro_split_reply_event_sequence"


class GroupState(BaseModel):
    gid: str
    """群号"""
    bot_msgs: deque = Field(default_factory=lambda: deque(maxlen=5))
    """Bot消息缓存"""
    msg_queue: deque[str] = Field(default_factory=lambda: deque(maxlen=20))
    """最近的用户消息 ID，仅用于诊断和兼容回退判断"""
    reply_message_seq: int = 0
    """群内用户消息序号，用于跨并发回复计算插嘴数量"""
    reply_handled_msg_ids: deque[str] = Field(
        default_factory=lambda: deque(maxlen=20),
    )
    """已经插入引用的原消息 ID，避免同一事件重复引用"""
    name_to_qq: OrderedDict[str, str] = Field(default_factory=lambda: OrderedDict())
    """昵称 -> QQ"""

    def record_reply_message(self, msg_id: str) -> int:
        """记录一条用户消息并返回其序号。"""
        self.reply_message_seq += 1
        self.msg_queue.append(msg_id)
        return self.reply_message_seq

    def mark_reply_handled(self, msg_id: str) -> bool:
        """标记消息已引用；返回 False 表示此前已经处理过。"""
        if msg_id in self.reply_handled_msg_ids:
            return False
        self.reply_handled_msg_ids.append(msg_id)
        return True


class StateManager:
    """内存状态管理"""

    _groups: dict[str, GroupState] = {}

    @classmethod
    def get_group(cls, gid: str) -> GroupState:
        if gid not in cls._groups:
            cls._groups[gid] = GroupState(gid=gid)
        return cls._groups[gid]


@dataclass
class OutContext:
    """输出消息上下文"""

    event: AstrMessageEvent
    chain: list[BaseMessageComponent]
    is_llm: bool
    plain: str
    gid: str
    uid: str
    bid: str
    group: GroupState
    timestamp: int



class StepName(str, Enum):
    SUMMARY = "summary"
    ERROR = "error"
    BLOCK = "block"
    AT = "at"
    CLEAN = "clean"
    REPLACE = "replace"
    TTS = "tts"
    T2I = "t2i"
    REPLY = "reply"
    FORWARD = "forward"
    RECALL = "recall"
    SPLIT = "split"


@dataclass(slots=True)
class StepResult:
    ok: bool = True
    """步骤是否成功"""
    abort: bool = False
    """是否需要中断处理"""
    msg: str | None = None
    """附加消息"""
    data: Any | None = None
    """携带的上下文信息"""
