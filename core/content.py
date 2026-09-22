import re
from collections.abc import Iterable

from astrbot.api.message_components import At, AtAll, BaseMessageComponent, Plain, Reply

AT_HEAD_RE = re.compile(
    r"^\s*(?:"
    r"\[at[:：]\s*(\d+)\]"
    r"|\[at[:：]\s*([^\]]+)\]"
    r"|@(\d{5,12})"
    r"|@([\u4e00-\u9fa5\w-]{2,20})"
    r")\s*",
    re.IGNORECASE,
)


class MentionText(Plain):
    """字符串艾特前缀，在分段时与正文绑定。"""


def visible_text(text: str) -> str:
    return (
        (text or "")
        .translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff")))
        .strip()
    )


def has_payload(chain: Iterable[BaseMessageComponent]) -> bool:
    for component in chain:
        if isinstance(component, (At, AtAll, Reply, MentionText)):
            continue
        if not isinstance(component, Plain) or visible_text(component.text):
            return True
    return False


def remove_mentions(chain: list[BaseMessageComponent]) -> None:
    chain[:] = [
        component
        for component in chain
        if not isinstance(component, (At, AtAll, MentionText))
    ]
    for component in chain:
        if isinstance(component, Plain):
            component.text = AT_HEAD_RE.sub("", component.text, count=1)


def materialize_mentions(chain: list[BaseMessageComponent]) -> None:
    index = 0
    while index < len(chain):
        component = chain[index]
        if not isinstance(component, MentionText):
            index += 1
            continue
        text = component.text.rstrip() + " "
        while index + 1 < len(chain) and isinstance(chain[index + 1], Plain):
            text += chain.pop(index + 1).text
        chain[index] = Plain(text=text)
        index += 1
