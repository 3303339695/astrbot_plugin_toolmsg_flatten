"""把 AstrBot 自带的上下文裁剪整段废掉（只 monkeypatch，不改本体源码）。

## 裁在哪两处

1. **对话层**：`astrbot/core/agent/context/truncator.py` 里的三个方法——
   按轮次截断、丢弃最早的若干轮、超限减半。统一入口是
   `agent/context/manager.py` 的 `ContextManager.process`：
   先按轮次裁一次，压缩后还超再减半。
2. **接口层**：这一处藏得深。请求报「上下文超长」时，
   `Provider.pop_record` 会把最早的非 system 消息删掉（最多两条）再重试。

这里把它们统统换成「一条都不删」，原样把消息列表递回去。
真正的压缩（摘要那一套）照旧——那份压缩器只保留开头 system，
旧轮次整段换成一问一答的摘要，跟这里不冲突。

## 三条规矩

- 只 monkeypatch，绝不改上游源码；
- 幂等：重复加载不重复打补丁（替换上去的函数上留了记号）；
- 容错：上游改名字、少了属性，就跳过这一处并记一条日志，
  绝不能让整个插件起不来。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

# 打在替换上去的函数上的记号，用来认「这个已经换过了」
_FLAG = "__toolmsg_flatten_no_cut__"

# 原件存在哪儿：存在**换上去的那个函数自己身上**，而不是存在模块变量里。
# 为什么：插件热重载会生成新模块，模块里的变量全没了；但类上的函数还是旧那个，
# 只要原件跟着函数走，新模块一进来就能顺着它把方法还回去。
_ORIGINAL_ATTR = "__toolmsg_no_cut_original__"
# 接口层那条是多个类共用一个替身，原件没法挂在它身上，就挂在类上
_POP_ORIGINAL_ATTR = "__toolmsg_no_cut_pop_record_original__"

# 要变成恒等函数的三个裁剪方法
_TRUNCATE_METHODS = (
    "truncate_by_turns",
    "truncate_by_dropping_oldest_turns",
    "truncate_by_halving",
)


class _Settings:
    """运行期设置。"""

    disable_truncation: bool = True


SETTINGS = _Settings()


# ------------------------------------------------------------------ 小工具


def _marked(obj: Any) -> bool:
    """这个函数是不是已经被咱换掉了。"""
    return bool(getattr(obj, _FLAG, False))


def _iter_classes(base: type) -> list[type]:
    """把基类和它所有的子子孙孙列出来。"""
    pending = [base]
    result: list[type] = []
    seen: set[type] = set()
    while pending:
        klass = pending.pop(0)
        if klass in seen:
            continue
        seen.add(klass)
        result.append(klass)
        try:
            pending.extend(klass.__subclasses__())
        except Exception:  # noqa: BLE001
            continue
    return result


# ------------------------------------------------------------------ 第一处：裁剪器


def _make_identity(method_name: str, original: Any) -> Any:
    """造一个「什么都不删」的替身。"""

    def identity(self, messages, *args, **kwargs):
        return messages

    # 只搬名字和说明，不搬 __wrapped__ 之类的东西：
    # 让「被包过」这种痕迹留得越少越好，别人 unwrap 时不会绕进这里。
    identity.__name__ = method_name
    identity.__qualname__ = method_name
    identity.__module__ = getattr(original, "__module__", identity.__module__)
    identity.__doc__ = "（已被工具回传改写插件接管：什么都不删，原样返回）"
    setattr(identity, _FLAG, True)
    # 原件挂在函数自己身上，关开关时靠它还回去（不能用 __wrapped__，
    # 免得别的插件 unwrap 时绕进我们这一层）
    setattr(identity, _ORIGINAL_ATTR, original)
    return identity


def _patch_truncator() -> list[str]:
    """把三个裁剪方法换成恒等函数，返回这次换上去的方法名。"""
    applied: list[str] = []
    try:
        from astrbot.core.agent.context.truncator import ContextTruncator
    except Exception as exc:  # noqa: BLE001
        logger.warning("[上下文不裁剪] 找不到裁剪器，这一处跳过：%s", exc)
        return applied

    for method_name in _TRUNCATE_METHODS:
        try:
            current = getattr(ContextTruncator, method_name, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[上下文不裁剪] 读不到 %s，跳过：%s", method_name, exc)
            continue
        if current is None:
            logger.warning(
                "[上下文不裁剪] 裁剪器上已经没有 %s 了，跳过（AstrBot 可能改了实现）",
                method_name,
            )
            continue
        if _marked(current):
            continue
        try:
            setattr(ContextTruncator, method_name, _make_identity(method_name, current))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[上下文不裁剪] 替换 %s 失败，跳过：%s", method_name, exc)
            continue
        applied.append(method_name)
    return applied


def _truncator_patched() -> bool:
    """三个方法是不是都已经被换掉了。"""
    try:
        from astrbot.core.agent.context.truncator import ContextTruncator
    except Exception:  # noqa: BLE001
        return False
    for method_name in _TRUNCATE_METHODS:
        if not _marked(getattr(ContextTruncator, method_name, None)):
            return False
    return True


# ------------------------------------------------------------------ 第二处：接口层


async def _noop_pop_record(self, context):
    """上游想删掉最早的消息再重试，这里一条都不删。"""
    logger.warning(
        "[上下文不裁剪] 上游准备删掉最早的消息来腾地方，这里拦下了（一条没删，照原样重试）"
    )
    return None


setattr(_noop_pop_record, _FLAG, True)


def _pop_record_owners() -> list[type]:
    """把「自己定义了 pop_record」的类列出来（基类加各个服务商）。"""
    try:
        from astrbot.core.provider.provider import Provider
    except Exception:  # noqa: BLE001
        return []
    owners: list[type] = []
    for klass in _iter_classes(Provider):
        try:
            if klass.__dict__.get("pop_record") is not None:
                owners.append(klass)
        except Exception:  # noqa: BLE001
            continue
    return owners


def _patch_pop_record() -> bool:
    """把接口层「超长就删消息」那个方法换成不删；返回这次有没有新换上。"""
    owners = _pop_record_owners()
    if not owners:
        logger.warning(
            "[上下文不裁剪] 服务商上没找到 pop_record，接口层这一处按「本来就没有」处理"
        )
        return False

    applied = False
    for klass in owners:
        own = klass.__dict__.get("pop_record")
        if _marked(own):
            continue
        try:
            setattr(klass, _POP_ORIGINAL_ATTR, own)
            setattr(klass, "pop_record", _noop_pop_record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[上下文不裁剪] 替换 %s.pop_record 失败，跳过：%s",
                getattr(klass, "__name__", "?"),
                exc,
            )
            continue
        applied = True
    return applied


def uninstall() -> dict[str, Any]:
    """把三处裁剪还给 AstrBot。开关关掉时用。

    为什么要有：开关只判「这次装不装」是不够的。类上的方法在整个进程里是同一个
    对象，热重载不会重建它——不主动还回去，关掉开关照样不裁剪。
    """
    restored: list[str] = []

    try:
        from astrbot.core.agent.context.truncator import ContextTruncator
    except Exception:  # noqa: BLE001
        ContextTruncator = None  # type: ignore[assignment]
    if ContextTruncator is not None:
        for method_name in _TRUNCATE_METHODS:
            try:
                current = getattr(ContextTruncator, method_name, None)
            except Exception:  # noqa: BLE001
                continue
            original = getattr(current, _ORIGINAL_ATTR, None)
            if original is None:
                continue
            try:
                setattr(ContextTruncator, method_name, original)
                restored.append(method_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[上下文不裁剪] 还原 %s 失败，只能重启：%s", method_name, exc
                )

    for klass in _pop_record_owners():
        current = klass.__dict__.get("pop_record")
        original = klass.__dict__.get(_POP_ORIGINAL_ATTR)
        if original is None or not _marked(current):
            continue
        try:
            setattr(klass, "pop_record", original)
            restored.append(getattr(klass, "__name__", "?") + ".pop_record")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[上下文不裁剪] 还原 %s.pop_record 失败，只能重启：%s",
                getattr(klass, "__name__", "?"),
                exc,
            )

    return {"restored": restored}


def _pop_record_off() -> bool:
    """接口层那处：要么本来就没有，要么已经全部拦下。"""
    owners = _pop_record_owners()
    if not owners:
        return True
    return all(_marked(klass.__dict__.get("pop_record")) for klass in owners)


def _cut_fully_off() -> bool:
    """裁剪到底关没关干净：三处恒等 + 接口层拦住。"""
    return _truncator_patched() and _pop_record_off()


# ------------------------------------------------------------------ 入口


def install(disable_truncation: bool = True) -> dict[str, Any]:
    """按设置装上（或说明为什么没装）。返回一份状态，给启动日志用。"""
    SETTINGS.disable_truncation = bool(disable_truncation)
    status: dict[str, Any] = {
        "enabled": SETTINGS.disable_truncation,
        "truncator": [],
        "pop_record": False,
        "installed": False,
        "restored": [],
    }

    if not SETTINGS.disable_truncation:
        info = uninstall()
        status["restored"] = info["restored"]
        logger.info(
            "[上下文不裁剪] 开关是关的：已把裁剪还给本体（还原 %d 处）",
            len(info["restored"]),
        )
        return status

    status["truncator"] = _patch_truncator()
    status["pop_record"] = _patch_pop_record()
    status["installed"] = _cut_fully_off()

    if status["truncator"] or status["pop_record"]:
        logger.info(
            "[上下文不裁剪] 已接管：轮次裁剪=%s，超长重试删消息=%s（摘要压缩保持原样）",
            "、".join(status["truncator"]) or "（之前已接管）",
            "已拦下" if status["pop_record"] else "（之前已拦下）",
        )
    elif status["installed"]:
        # 热重载会重新走一遍安装：不重复打补丁，也不重复刷日志
        logger.debug("[上下文不裁剪] 之前已经接管过，重复加载不重复打补丁")
    else:
        logger.warning(
            "[上下文不裁剪] 一处都没接上：AstrBot 可能改了实现，上下文仍然会被裁"
        )
    return status
