"""提示词来源追踪：这次请求里，哪个插件往哪儿塞了什么。

## 能追到哪一层

只追**插件**。本体自己拼的那部分——人格设定、历史消息、时间提醒——没有钩子可挂，
追不到，只能整体标成「本体」。

## 怎么追的

本体在跑插件钩子时是"挨个调用"的：每个插件、每次调用，前后各拍一张快照，
比一比就知道这个插件改了什么、改在哪儿。

钩子函数本身是异步生成器，所以这里也包成一个异步生成器，原样把它的产出转出去，
一个字节都不改，只在前后多做一件事：记账。

## 记完放哪

放进一个短期队列。抓包那一刻（请求即将发出）从队列里取最近一条，
跟请求一起落盘。并发多个会话时按时间取最近的，极端情况下可能张冠李戴——
但绝大多数时候够用。

## 已知的天花板

历史消息入库之后，来源就没了。所以这里说的是"**本次请求**里谁加了什么"，
不是说"这条历史消息是谁当初加的"。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from astrbot.api import logger

# 最近一次请求的来源记录。同一次请求会调多次钩子，按请求对象归并。
_LAST = {"req": None, "umo": "", "sources": []}

# 一次最多记多少段，防止被某个插件刷屏
MAX_SPANS = 40

_STATE = {
    "installed": False,
    "module": None,
    "orig_handler": None,
    "patched": [],
}


def _system_of(req: Any) -> str:
    try:
        value = getattr(req, "system_prompt", None)
        return value if isinstance(value, str) else ""
    except Exception:  # noqa: BLE001
        return ""


def _part_text(part: Any) -> str:
    """从"额外内容块"里取文本。可能是个对象，也可能是个字典。"""
    if isinstance(part, dict):
        return str(part.get("text") or "")
    text = getattr(part, "text", None)
    if isinstance(text, str):
        return text
    try:
        return str(part) if part is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _parts_of(req: Any) -> list[str]:
    """取这次请求里所有"额外内容块"的文本。

    很多插件（比如小本本）不是往系统提示塞东西，而是往这里追加一段提醒，
    最后会被并进用户消息里。只盯系统提示的话，这类注入完全看不见。
    """
    try:
        raw = getattr(req, "extra_user_content_parts", None) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for part in list(raw):
        text = _part_text(part).strip()
        if text:
            out.append(text)
    return out


def _snapshot_of(req: Any) -> dict:
    """拍一张快照：系统提示全文 + 额外内容块文本 + 上下文条数。"""
    contexts = 0
    try:
        contexts = len(getattr(req, "contexts", None) or [])
    except Exception:  # noqa: BLE001
        contexts = 0
    return {
        "system": _system_of(req),
        "parts": _parts_of(req),
        "contexts": contexts,
    }


def _contexts_size(req: Any) -> int:
    try:
        contexts = getattr(req, "contexts", None)
        return len(contexts) if isinstance(contexts, list) else 0
    except Exception:  # noqa: BLE001
        return 0


def _changed_span(before: str, after: str) -> tuple[int, int] | None:
    """找出 after 里相对 before 变了的那一段（起止下标）。"""
    if before == after:
        return None
    limit = min(len(before), len(after))
    head = 0
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while (
        tail < limit - head
        and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]
    ):
        tail += 1
    return head, len(after) - tail


def _plugin_of(handler: Any) -> tuple[str, str]:
    """从绑定方法反查它是哪个插件的哪个钩子。"""
    try:
        owner = getattr(handler, "__self__", None)
        if owner is None:
            return "", ""
        cls = type(owner)
        name = str(getattr(owner, "name", "") or cls.__name__)
        hook = str(getattr(handler, "__name__", "") or "")
        return name, hook
    except Exception:  # noqa: BLE001
        return "", ""


def _plugin_from_path(module_path: str) -> str:
    """从 handler 的模块路径里抠出插件名。

    路径长这样：data.plugins.astrbot_plugin_xxx.main，取倒数第二段。
    """
    parts = [p for p in str(module_path or "").split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else ""


def _record(req: Any, umo: str, entry: dict) -> None:
    """按请求对象归并：同一次请求里的多次钩子调用，攒成一条。"""
    if req is not _LAST["req"]:
        _LAST["req"] = req
        _LAST["umo"] = umo
        _LAST["sources"] = []
    sources = _LAST["sources"]
    if len(sources) < MAX_SPANS:
        sources.append(entry)


def take(umo_hint: str | None = None) -> dict | None:
    """取最近一次请求的来源记录（抓包那一刻调用）。"""
    sources = _LAST.get("sources") or []
    if not sources:
        return None
    return {
        "umo": _LAST.get("umo") or "",
        "sources": list(sources),
    }


def _diff_and_record(req, event, plugin_name: str, hook_name: str, before: dict, after: dict) -> None:
    """比一比前后快照，有变化就记一笔。"""
    span = _changed_span(before["system"], after["system"])
    added_ctx = max(0, after["contexts"] - before["contexts"])
    old_parts = set(before["parts"])
    added_parts = [text for text in after["parts"] if text not in old_parts]

    if span is None and not added_ctx and not added_parts:
        return

    entry = {
        "plugin": plugin_name or "未知插件",
        "hook": hook_name,
        "contexts_added": added_ctx,
    }
    if span is not None:
        start, end = span
        piece = after["system"][start:end]
        if len(after["system"]) > len(before["system"]):
            mode = "追加"
        elif len(after["system"]) < len(before["system"]):
            mode = "删除"
        else:
            mode = "替换"
        entry.update(
            {
                "start": start,
                "end": end,
                "chars": len(piece) if piece else abs(
                    len(after["system"]) - len(before["system"])
                ),
                "mode": "系统提示" + mode,
                "sample": piece[:120].replace("\n", " "),
            }
        )
    if added_parts:
        entry["parts"] = added_parts[:6]
        entry["parts_chars"] = sum(len(text) for text in added_parts)
        if not entry.get("mode"):
            entry["mode"] = "用户消息追加"
            entry["chars"] = entry["parts_chars"]
        else:
            entry["mode"] = entry["mode"] + " + 用户消息追加"

    umo = ""
    try:
        umo = str(event.unified_msg_origin or "")
    except Exception:  # noqa: BLE001
        umo = ""
    _record(req, umo, entry)


def _wrap_hook(fn: Any, plugin_name: str, hook_name: str) -> Any:
    """把单个钩子函数包成"前后各拍一张快照"的版本。"""

    async def inner(event, *args, **kwargs):
        req = kwargs.get("req")
        if req is None:
            for value in args:
                if value is not None and hasattr(value, "system_prompt"):
                    req = value
                    break
        if req is None:
            try:
                req = event.get_extra("provider_request")
            except Exception:  # noqa: BLE001
                req = None
        if req is None:
            await fn(event, *args, **kwargs)
            return

        before = _snapshot_of(req)
        await fn(event, *args, **kwargs)
        after = _snapshot_of(req)
        _diff_and_record(req, event, plugin_name, hook_name, before, after)

    inner._prompt_source_wrapped = True  # type: ignore[attr-defined]
    inner.__wrapped__ = fn  # type: ignore[attr-defined]
    return inner


def _ensure_hooks_wrapped(verbose: bool = False) -> int:
    """把当前所有"请求时钩子"都包上。返回这次新包了几个。

    注意：插件初始化那一刻调用是扫不到的——本体还在逐个加载插件，
    每个插件的"已激活"标记都还没翻上去，过滤器会把它们全挡掉。
    真正有效的时机是第一次请求，那时候全都在了。
    """
    try:
        from astrbot.core.star.star_handler import EventType
        from astrbot.core.star.star_handler import (
            star_handlers_registry as registry,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[来源追踪] 拿不到钩子登记表：%s", exc)
        return 0

    try:
        # 不按"已激活"过滤：那个标记在插件初始化阶段还没翻上来，
        # 按它筛会一个都拿不到。未激活插件的钩子本来也不会被调用，包上无害。
        handlers = registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent,
            only_activated=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[来源追踪] 列不出请求时钩子：%s", exc)
        return 0

    if not handlers:
        total = len(getattr(registry, "_handlers", []) or [])
        logger.warning(
            "[来源追踪] 一个请求时钩子都没找到（登记表里一共 %d 条钩子）。"
            "如果这个数字是零，说明本体还没注册任何钩子；不是零就说明事件类型对不上。",
            total,
        )
        return 0

    count = 0
    for handler in list(handlers or []):
        fn = getattr(handler, "handler", None)
        if fn is None or getattr(fn, "_prompt_source_wrapped", False):
            continue
        plugin_name = _plugin_from_path(getattr(handler, "handler_module_path", ""))
        hook_name = str(getattr(handler, "handler_name", "") or "")
        try:
            handler.handler = _wrap_hook(fn, plugin_name, hook_name)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[来源追踪] 包 %s 失败：%s", plugin_name, exc)

    if count and verbose:
        logger.info("[来源追踪] 补包了 %d 个请求时钩子", count)
    return count


def _unwrap_hooks() -> int:
    """把先前套在钩子函数上的壳剥掉。

    接管调度之后就不需要给每个钩子套壳了——里外都记会记两遍。
    """
    try:
        from astrbot.core.star.star_handler import EventType
        from astrbot.core.star.star_handler import (
            star_handlers_registry as registry,
        )

        handlers = registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent, only_activated=False
        )
    except Exception:  # noqa: BLE001
        return 0

    count = 0
    for handler in list(handlers or []):
        fn = getattr(handler, "handler", None)
        if fn is None or not getattr(fn, "_prompt_source_wrapped", False):
            continue
        original = getattr(fn, "__wrapped__", None)
        if original is None:
            continue
        try:
            handler.handler = original
            count += 1
        except Exception:  # noqa: BLE001
            continue
    if count:
        logger.info("[来源追踪] 剥掉了 %d 个旧的钩子壳（改由调度层统一记）", count)
    return count


def install() -> str:
    """把"请求时钩子"的调度接过来：每次调用前都补包，然后转发。"""
    if _STATE["installed"]:
        return "已在追踪"

    try:
        from astrbot.core.pipeline import context_utils as cu
    except Exception as exc:  # noqa: BLE001
        return f"没装上（{exc}）"

    original = getattr(cu, "call_event_hook", None)
    if original is None:
        return "没装上（找不到钩子调度）"

    async def wrapper(event, hook_type, *args, **kwargs):
        """请求时钩子这一路，不交给本体调度了，自己挨个调、两头记。

        为什么这么干：先前是在"找到钩子、给它套壳"这条路上折腾，
        只要有一个环节对不上就全盘皆输。现在改成整体接管——拿得到钩子列表，
        就一定能逐个调用、逐个记；拿不到就老实退回原实现。
        """
        try:
            from astrbot.core.star.star_handler import EventType

            is_llm_request = hook_type == EventType.OnLLMRequestEvent
        except Exception:  # noqa: BLE001
            is_llm_request = False

        if not is_llm_request:
            return await original(event, hook_type, *args, **kwargs)

        # 请求对象：参数里有就用参数里的，没有就去事件上拿
        req = None
        for value in args:
            if value is not None and hasattr(value, "system_prompt"):
                req = value
                break
        if req is None:
            try:
                req = event.get_extra("provider_request")
            except Exception:  # noqa: BLE001
                req = None
        if req is None:
            return await original(event, hook_type, *args, **kwargs)

        try:
            from astrbot.core.star.star_handler import (
                star_handlers_registry as registry,
            )

            handlers = registry.get_handlers_by_event_type(
                hook_type,
                only_activated=False,
                plugins_name=getattr(event, "plugins_name", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[来源追踪] 拿不到钩子列表，退回本体调度：%s", exc)
            return await original(event, hook_type, *args, **kwargs)

        if not handlers:
            total = len(getattr(registry, "_handlers", []) or [])
            logger.warning(
                "[来源追踪] 请求时钩子列表是空的（登记表里一共 %d 条钩子）", total
            )
            return await original(event, hook_type, *args, **kwargs)

        import traceback as _tb

        for handler in list(handlers):
            fn = getattr(handler, "handler", None)
            if fn is None:
                continue
            plugin_name = _plugin_from_path(
                getattr(handler, "handler_module_path", "")
            )
            hook_name = str(getattr(handler, "handler_name", "") or "")
            before = _snapshot_of(req)
            try:
                await fn(event, *args, **kwargs)
            except BaseException:  # noqa: BLE001
                logger.error(_tb.format_exc())
            after = _snapshot_of(req)
            try:
                _diff_and_record(req, event, plugin_name, hook_name, before, after)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[来源追踪] 记录 %s 失败：%s", plugin_name, exc)
            try:
                if event.is_stopped():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    setattr(wrapper, "_prompt_source_dispatch", True)

    # 本体里各模块是 `from ... import call_event_hook` 拿的引用，
    # 光改定义处不够，得把所有持有旧引用的模块都换掉
    patched: list[tuple[Any, str]] = []
    import sys as _sys

    for name, module in list(_sys.modules.items()):
        if module is None or not hasattr(module, "call_event_hook"):
            continue
        try:
            if getattr(module, "call_event_hook") is not original:
                continue
            setattr(module, "call_event_hook", wrapper)
            patched.append((module, name))
        except Exception:  # noqa: BLE001
            continue

    # 剥掉旧版套在钩子上的壳：现在由调度层统一记，里外都记会重复
    _unwrap_hooks()

    _STATE.update(
        installed=True,
        module=cu,
        orig_handler=original,
        patched=patched,
    )
    return f"已在追踪（接管调度，换引用 {len(patched)} 处）"


def uninstall() -> str:
    if not _STATE["installed"]:
        return "关闭"
    try:
        original = _STATE.get("orig_handler")
        for module, _name in _STATE.get("patched") or []:
            try:
                setattr(module, "call_event_hook", original)
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        return f"关闭（没还干净：{exc}）"
    _STATE.update(installed=False, module=None, orig_handler=None, patched=[])
    return "关闭（已还回本体）"


def summary_text(record: dict | None) -> str:
    """压成一句话，给日志用。"""
    if not record:
        return "没有记录"
    sources = record.get("sources") or []
    bits = []
    for item in sources[:4]:
        name = item.get("plugin") or "未知"
        if item.get("chars"):
            bits.append("%s 改了 %d 字" % (name, item["chars"]))
        elif item.get("contexts_added"):
            bits.append("%s 加了 %d 条消息" % (name, item["contexts_added"]))
    return "；".join(bits) or "没有插件动过"


__all__ = ["install", "uninstall", "take", "summary_text", "MAX_SPANS"]
