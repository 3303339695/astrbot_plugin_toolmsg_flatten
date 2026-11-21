"""人格对话示例的「摘出来 / 插回去」。

## 为什么要隔离

AstrBot 把人格自带的对话示例（每条都带 `_no_save` 的那种，不落库）
直接塞进请求上下文的**最前面**（`astr_main_agent.py` 里那行切片赋值）。
它们于是和真实历史混在一起，会被轮次裁剪、超限减半、以及摘要压缩
当成「旧历史」处理：要么被裁掉，要么被并进摘要里，等于白写。

这里在请求进执行器时（`ToolLoopAgentRunner.reset` 包装里）把这一整段摘出来，
放在本次请求对象上；真正发给模型前，再插回
「开头 system 之后、摘要之前」这个位置。

## 两件事都要能重复做

一次提问里工具来回好几趟、请求发好几次，摘和插都会重复发生。
所以插之前先看一眼那个位置是不是已经有一份同样的示例，有就不插。

## 只认两样东西

- 消息对象（有 `.role` / `.content`，可能还有 `.text_content()`）；
- 字典（`{"role": ..., "content": ...}`）。

示例正文是普通字符串，插回去时按原样搬，不做任何格式改写。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

# 摘要消息的开头。AstrBot 的摘要压缩器写死的就是这一句，靠它认出「摘要那一对」。
SUMMARY_PREFIX = "Our previous history conversation summary"

class _Settings:
    """运行期设置，由 main.py 按配置写进来。"""

    keep_examples: bool = True


SETTINGS = _Settings()

# ------------------------------------------------------------------ 小工具


def is_temp(msg: Any) -> bool:
    """这条消息是不是「只给模型看、不落库」的那种（人格示例打的就是这个标）。"""
    if isinstance(msg, dict):
        return bool(msg.get("_no_save"))
    return bool(getattr(msg, "_no_save", False))


def role_of(msg: Any) -> str:
    """取一条消息的身份；对象、字典两种形态都认。"""
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _parts_text(value: Any) -> str:
    """把「一串碎片」形式的正文拼回文字（只取文字块，图片语音不掺和）。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    pieces: list[str] = []
    for part in value:
        if isinstance(part, str):
            pieces.append(part)
            continue
        text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces)


def text_of(msg: Any) -> str:
    """取一条消息的正文文字；对象、字典两种形态都认。"""
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        return _parts_text(msg.get("content"))

    # 有的版本给了个现成的取文字方法，有就用它
    reader = getattr(msg, "text_content", None)
    if callable(reader):
        try:
            text = reader()
            if text is not None:
                return str(text)
        except Exception:  # noqa: BLE001 - 读不出来就退回下面硬拆
            pass
    return _parts_text(getattr(msg, "content", None))


def session_key_of(request: Any) -> str:
    """取这次请求的会话标识，仅用于日志。"""
    try:
        return str(getattr(request, "session_id", "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 摘


def _capture(request: Any) -> int:
    """把人格示例从 request.contexts 里整段摘走，返回摘走了几条。

    写回上下文用重新赋值，不原地切片，旧列表一点引用都不留。
    """
    if not SETTINGS.keep_examples:
        return 0

    contexts = getattr(request, "contexts", None)
    previous = getattr(request, "_tmf_captured_contexts", None)
    if previous is contexts and getattr(request, "_tmf_examples_captured", False):
        return 0
    if not isinstance(contexts, list) or not contexts:
        return 0

    kept: list[Any] = []
    taken: list[Any] = []
    for item in contexts:
        if is_temp(item):
            taken.append(item)
        else:
            kept.append(item)

    try:
        request._tmf_examples = taken
        request._tmf_examples_captured = True
        request._tmf_captured_contexts = kept if taken else contexts
        if taken:
            request.contexts = kept
    except Exception as exc:  # noqa: BLE001
        logger.warning("[示例隔离] 无法保存请求示例，本次按原样：%s", exc)
        return 0
    return len(taken)


def capture(request: Any) -> int:
    return _capture(request)


# ------------------------------------------------------------------ 插


def _already_present(contexts: list[Any], items: list[Any]) -> bool:
    """那个位置是不是已经有一份同样的示例了。"""
    # Only a complete matching sequence counts. Other temporary messages, or
    # one reused example, must not suppress the rest of the examples.
    for position in range(len(contexts) - len(items) + 1):
        window = contexts[position : position + len(items)]
        if all(
            role_of(got) == role_of(want)
            and text_of(got).strip() == text_of(want).strip()
            for got, want in zip(window, items)
        ):
            return True
    return False


def _insert_position(contexts: list[Any]) -> int:
    """该插在第几个。

    开头连续的 system 之后；如果紧跟着一条摘要（user，正文以那句开头），
    就插在摘要前面；没有摘要就插在 system 之后、历史之前。
    """
    position = 0
    total = len(contexts)
    while position < total and role_of(contexts[position]) == "system":
        position += 1

    for index in range(position, total):
        if role_of(contexts[index]) == "user" and text_of(contexts[index]).lstrip().startswith(
            SUMMARY_PREFIX
        ):
            return index
    return position


def _as_object(data: dict[str, Any]) -> Any:
    """把一条字典消息变成消息对象，和上下文里其他消息保持同一种形态。"""
    try:
        from astrbot.core.agent.message import Message
    except Exception:  # noqa: BLE001 - 拿不到类就退回字典形态
        return None
    try:
        message = Message.model_validate(data)
    except Exception:  # noqa: BLE001
        return None
    try:
        message._no_save = True
    except Exception:  # noqa: BLE001
        pass
    return message


def _build(item: Any, use_dict: bool) -> Any:
    """按这次上下文里的形态，造一条可以插回去的消息。

    `_no_save` 是插件内部用的记号，绝不能出现在要发出去的字典里
    （上游看到不认识的字段有可能直接拒收）。
    """
    if isinstance(item, dict):
        clean = {key: value for key, value in item.items() if key != "_no_save"}
        if use_dict:
            return clean
        return _as_object(clean) or clean

    if use_dict:
        dumper = getattr(item, "model_dump", None)
        if callable(dumper):
            try:
                data = dumper()
                if isinstance(data, dict) and data.get("role"):
                    return data
            except Exception:  # noqa: BLE001
                pass
        return {"role": role_of(item), "content": text_of(item)}

    # 两边都是对象：原样插回，私有标记本来就不会被序列化
    return item


def insert_for_request(contexts: list[Any], request: Any) -> list[Any]:
    """把这一轮摘下来的示例插回去。

    返回新的列表；没东西可插、或者那个位置已经有一份了，
    就把原列表原样返回（调用方靠「还是不是同一个对象」判断有没有动过）。
    """
    items = getattr(request, "_tmf_examples", None)
    if not items:
        return contexts
    try:
        if _already_present(contexts, items):
            return contexts
        use_dict = any(isinstance(item, dict) for item in contexts) or (
            not contexts and isinstance(items[0], dict)
        )
        built = [_build(item, use_dict) for item in items]
        position = _insert_position(contexts)
        return list(contexts[:position]) + built + list(contexts[position:])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[示例隔离] 插回示例失败，这次请求就当没有示例：%s", exc)
        return contexts


def insert(contexts: list[Any], session_id: str) -> list[Any]:
    """Compatibility for provider wrappers from previous plugin generations."""
    return contexts
