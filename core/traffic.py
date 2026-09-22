from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import AstrBotConfig

from .model import REPLY_EVENT_SEQUENCE_EXTRA, StateManager


class GroupTrafficFilter(filter.CustomFilter):
    """在唤醒检查时记录群内消息，不唤醒 Bot 或激活消息处理器。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if not event.get_group_id() or str(event.get_sender_id()) == str(
            event.get_self_id()
        ):
            return False
        group = StateManager.get_event_group(event)
        if not isinstance(event.get_extra(REPLY_EVENT_SEQUENCE_EXTRA), int):
            message_id = getattr(event.message_obj, "message_id", None)
            sequence = group.record_reply_message(
                "" if message_id is None else str(message_id)
            )
            event.set_extra(REPLY_EVENT_SEQUENCE_EXTRA, sequence)
        name = event.get_sender_name()
        if name:
            group.name_to_qq[name] = str(event.get_sender_id())
            group.name_to_qq.move_to_end(name)
            if len(group.name_to_qq) > 100:
                group.name_to_qq.popitem(last=False)
        return False
