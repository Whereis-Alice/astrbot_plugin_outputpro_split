from collections.abc import Iterable

from astrbot.api.message_components import At, AtAll, BaseMessageComponent, Plain, Reply


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
