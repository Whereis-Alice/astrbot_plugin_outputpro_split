import asyncio
from types import SimpleNamespace

from astrbot.core.message.components import Plain, Reply

from core.model import (
    REPLY_EVENT_SEQUENCE_EXTRA,
    GroupState,
    OutContext,
)
from core.step.reply import ReplyStep


class FakeEvent:
    def __init__(self, message_id: str):
        self.message_obj = SimpleNamespace(message_id=message_id)
        self._extras = {}

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_id(self) -> str:
        return "42"

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key: str, value) -> None:
        self._extras[key] = value


def make_context(event: FakeEvent, group: GroupState, text: str) -> OutContext:
    return OutContext(
        event=event,
        chain=[Plain(text=text)],
        is_llm=True,
        plain=text,
        gid=group.gid,
        uid="42",
        bid="99",
        group=group,
        timestamp=0,
    )


def record(group: GroupState, event: FakeEvent) -> None:
    sequence = group.record_reply_message(event.message_obj.message_id)
    event.set_extra(REPLY_EVENT_SEQUENCE_EXTRA, sequence)


def make_step(threshold: int = 1) -> ReplyStep:
    config = SimpleNamespace(
        reply=SimpleNamespace(threshold=threshold, include_at=False),
        pipeline=SimpleNamespace(debug_logging=False),
    )
    return ReplyStep(config)


def test_reply_keeps_older_anchor_when_newer_reply_finishes_first():
    async def scenario():
        group = GroupState(gid="group-1")
        first = FakeEvent("m1")
        second = FakeEvent("m2")
        inserted = FakeEvent("m3")
        for event in (first, second, inserted):
            record(group, event)

        step = make_step()
        second_ctx = make_context(second, group, "reply-2")
        first_ctx = make_context(first, group, "reply-1")

        second_result = await step.handle(second_ctx)
        first_result = await step.handle(first_ctx)

        assert second_result.msg == "已插入Reply组件, 引用消息m2"
        assert first_result.msg == "已插入Reply组件, 引用消息m1"
        assert isinstance(second_ctx.chain[0], Reply)
        assert isinstance(first_ctx.chain[0], Reply)
        assert second_ctx.chain[0].id == "m2"
        assert first_ctx.chain[0].id == "m1"
        assert list(group.msg_queue) == ["m1", "m2", "m3"]

    asyncio.run(scenario())


def test_reply_threshold_counts_messages_after_anchor_only():
    async def scenario():
        group = GroupState(gid="group-1")
        anchor = FakeEvent("m1")
        later = FakeEvent("m2")
        record(group, anchor)
        record(group, later)

        step = make_step(threshold=2)
        context = make_context(anchor, group, "reply")
        result = await step.handle(context)

        assert result.msg is None
        assert isinstance(context.chain[0], Plain)

        record(group, FakeEvent("m3"))
        record(group, FakeEvent("m4"))
        result = await step.handle(context)

        assert result.msg == "已插入Reply组件, 引用消息m1"
        assert isinstance(context.chain[0], Reply)

    asyncio.run(scenario())


def test_reply_does_not_insert_duplicate_for_same_event():
    async def scenario():
        group = GroupState(gid="group-1")
        anchor = FakeEvent("m1")
        record(group, anchor)
        record(group, FakeEvent("m2"))

        step = make_step()
        first_context = make_context(anchor, group, "reply")
        second_context = make_context(anchor, group, "reply")

        first_result = await step.handle(first_context)
        second_result = await step.handle(second_context)

        assert first_result.msg == "已插入Reply组件, 引用消息m1"
        assert second_result.msg is None
        assert isinstance(first_context.chain[0], Reply)
        assert isinstance(second_context.chain[0], Plain)

    asyncio.run(scenario())
