"""工具回传改写（入口）。

## 只改一件事：结果的身份

上游（谷歌那边）的校验严起来之后，工具回传那条如果没有函数名就会被整条打回：

    *.contents[N].parts[0].function_response.name: Name cannot be empty

严起来之前屁事没有。所以本插件在**发送请求前**把"工具回传"那一条改成普通消息
（默认系统侧），只改**身份**：

- 数据库里怎么存，还是怎么存（插件不碰存档）；
- 助手的工具申请原样保留（模型要看得见自己申请了什么）；
- 工具清单照常交给服务商（模型该调就调）；
- **不替 AstrBot 改提示词**：重复调用的提醒用原版模板和阈值；
  轮次上限注入的也是原版那句收尾提示（直接从执行器类上取）。

另外保留两块"看得到"的东西：抓包页面（排查看请求）和报错补全（发送失败的
异常如果没带说明，补上类型名）。

## 维护须知（踩出来的，别删）

### 一、补丁必须"当场打"

插件加载得早，那时各家服务商的类往往还没被导入，`Provider.__subclasses__()`
是空的，一个补丁都打不上。表现：热重载装上去当天正常，冷启动重启后带工具
回传的历史又开始被拒。

所以补丁走两路：启动时把已经出现的类包掉；每条消息进执行器时，把这次要用的
服务商类当场包上。两路共用 `_PATCHED_CLASSES` 去重。

### 二、热重载要能换掉旧壳

热重载会生成新的模块对象，旧壳闭包里引用的是旧模块的设置。所以重打补丁时先
顺着 `__wrapped__` 剥回原件，再用本次加载的壳重新包。

### 三、改写只在"发送"这一层

存档由 AstrBot 自己负责，动它就是动用户的聊天记录。这里只在请求即将发出时
换掉那一条——这也是唯一能看到"最终要发出去的东西"的地方。
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

try:  # 面板接口；个别环境（精简安装）可能没有这一块
    from astrbot.api.web import json_response, request
except Exception:  # noqa: BLE001
    # 没有它顶多是"抓包页面"用不了，插件本体照常干活——
    # 绝不能因为这一块缺失，把整个插件判成加载失败。
    json_response = None
    request = None

from . import trace_core
from .flatten_core import (
    DEFAULT_RESULT_MAX_CHARS,
    DEFAULT_ROUND_MAX_CHARS,
    has_tool_format,
    normalize_role,
    rewrite_tool_results,
)

PLUGIN_NAME = "astrbot_plugin_toolmsg_flatten"

# 一次提问里，工具最多来回几轮（默认值，配置可改）
DEFAULT_MAX_TOOL_ROUNDS = 4

_WRAPPED_FLAG = "__toolmsg_flatten_wrapped__"
_SEND_FLAG = "__toolmsg_flatten_send_patched__"
_RUNNER_FLAG = "__toolmsg_flatten_runner_wrapped__"


class _Settings:
    """运行期设置，供包装后的函数读取。"""

    enable: bool = True
    keywords: list[str] = []
    record_role: str = "system"
    result_max_chars: int = DEFAULT_RESULT_MAX_CHARS
    round_max_chars: int = DEFAULT_ROUND_MAX_CHARS
    # 一次提问里，工具最多来回几轮；到了就把工具收走，逼它开口
    max_rounds: int = DEFAULT_MAX_TOOL_ROUNDS


SETTINGS = _Settings()


def _limit_prompt() -> str:
    """直接借用 AstrBot 自己那句"别再调了"的提示，不自己编。

    AstrBot 到步数上限时会摘工具并注入这句，用的原文就在执行器类上。
    这里原样取来用：模型对它的反应是验证过的，而且以后本体改措辞也跟着改。
    """
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )

        return str(ToolLoopAgentRunner.MAX_STEPS_REACHED_PROMPT)
    except Exception:  # noqa: BLE001
        return (
            "Maximum tool call limit reached. Stop calling tools, and based on the "
            "information you have gathered, summarize your task and findings, "
            "and reply to the user directly."
        )

# 本次加载已经包过的类（记"真正定义这个方法的那个类"）
_PATCHED_CLASSES: set[type] = set()


def _should_handle(provider: Any) -> bool:
    """这个服务商要不要管（按配置里的关键字挑，留空就全管）。"""
    if not SETTINGS.enable:
        return False
    if not SETTINGS.keywords:
        return True
    parts: list[str] = []
    try:
        config = getattr(provider, "provider_config", None) or {}
        parts.append(str(config.get("id", "") or ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        meta = provider.meta()
        parts.append(str(getattr(meta, "id", "") or ""))
        parts.append(str(getattr(meta, "model", "") or ""))
    except Exception:  # noqa: BLE001
        pass
    identity = " ".join(part for part in parts if part).lower()
    return any(keyword in identity for keyword in SETTINGS.keywords)


def _as_message_dict(msg: Any) -> dict | None:
    """把一条消息统一成字典，不管它原本是字典还是对象。

    执行器手里的上下文是"消息对象"，而配置了模态的服务商会先转成字典。
    改写逻辑只认字典，所以这里统一一下，免得换个服务商就改不动。
    """
    if isinstance(msg, dict):
        return msg
    dump = getattr(msg, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None
    return None


def _role_of(msg: Any) -> str:
    """取一条消息的角色（对象、字典两种形态都认）。"""
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _value_of(msg: Any, key: str) -> Any:
    """取一条消息上的字段（对象、字典两种形态都认）。"""
    if isinstance(msg, dict):
        return msg.get(key)
    return getattr(msg, key, None)


def _count_turn_tool_rounds(contexts: list[Any]) -> int:
    """数"这一句提问"里，工具已经来回了几轮。

    从尾部往前数"助手申请调用工具"的段，碰到用户发话就停。
    新的一句提问进来，计数自然归零，不会把下一轮也一起掐掉。
    """
    rounds = 0
    for msg in reversed(contexts or []):
        role = _role_of(msg)
        if role == "user":
            break
        if role == "assistant" and _value_of(msg, "tool_calls"):
            rounds += 1
    return rounds


def _decide_tool_gate(rounds: int, contexts: list[Any]) -> dict | None:
    """到了轮次上限，就把这次请求的工具清单收走。

    没有工具可调，它只能拿手头的信息开口——这是唯一能真正掐住
    "查起来没完没了"的办法（模型答应"不查了"是不算数的）。

    只影响这一次请求：存储不动，下一句提问进来计数归零，工具照常给。
    """
    limit = max(1, int(SETTINGS.max_rounds or DEFAULT_MAX_TOOL_ROUNDS))
    if rounds < limit:
        return None
    logger.warning(
        "[工具回传改写] 这一问已经来回 %d 轮工具（上限 %d），这次把工具收走，逼它直接回答",
        rounds,
        limit,
    )
    return {
        "role": normalize_role(SETTINGS.record_role),
        "content": _limit_prompt(),
    }


def _rewrite_for_send(contexts: list[Any], tool_results: Any) -> tuple[list[Any], bool]:
    """把要发出去的消息过一遍：只换"工具回传"，其余原样。

    返回（新的消息列表，有没有改动过）。
    """
    extra: list[Any] = []
    if tool_results:
        items = tool_results if isinstance(tool_results, list) else [tool_results]
        for item in items:
            to_messages = getattr(item, "to_openai_messages", None)
            if callable(to_messages):
                try:
                    extra.extend(to_messages())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[工具回传改写] 解析本轮工具结果失败：%s", exc)
            elif isinstance(item, dict):
                extra.append(item)

    original = list(contexts) + extra
    normalized = [(_as_message_dict(msg) or msg) for msg in original]
    if not has_tool_format(normalized):
        # 没有"工具回传"就一个字都不动（这是绝大多数请求）
        return original, False

    rewritten = rewrite_tool_results(
        normalized,
        record_role=SETTINGS.record_role,
        result_max_chars=SETTINGS.result_max_chars,
        round_max_chars=SETTINGS.round_max_chars,
    )
    logger.debug(
        "[工具回传改写] 这次发出去之前，把 %d 条工具回传换成了普通消息",
        sum(1 for m in normalized if isinstance(m, dict) and m.get("role") == "tool"),
    )
    return rewritten, True


def _wrap_text_chat(original):
    """包住非流式请求：发出去之前改写工具回传。"""
    signature = inspect.signature(original)

    async def wrapper(self, *args, **kwargs):
        if not _should_handle(self):
            return await original(self, *args, **kwargs)
        try:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            contexts = list(bound.arguments.get("contexts") or [])

            # 一、工具查了没完没了就把工具收走（这是本次请求的临时决定，存储不动）
            rounds = _count_turn_tool_rounds(contexts)
            gate = _decide_tool_gate(rounds, contexts)
            if gate is not None:
                bound.arguments["func_tool"] = None
                contexts = list(contexts) + [gate]
                bound.arguments["contexts"] = contexts

            # 二、把"工具回传"换成普通消息
            rewritten, changed = _rewrite_for_send(
                contexts, bound.arguments.get("tool_calls_result")
            )
            touched = changed or gate is not None
            if not touched:
                return await original(self, *args, **kwargs)

            bound.arguments["contexts"] = rewritten
            if changed:
                bound.arguments["tool_calls_result"] = None
            return await original(self, *bound.args[1:], **bound.kwargs)
        except Exception as exc:  # noqa: BLE001
            # 改写出错绝不能把用户的对话打挂：原样放行，把原因留在日志里
            logger.error(
                "[工具回传改写] 出错，这一次按原样放行：%s", exc, exc_info=True
            )
            return await original(self, *args, **kwargs)

    setattr(wrapper, _WRAPPED_FLAG, True)
    wrapper.__wrapped__ = original
    return wrapper


def _wrap_text_chat_stream(original):
    """包住流式请求：同样只改写工具回传。"""
    signature = inspect.signature(original)

    async def wrapper(self, *args, **kwargs):
        if not _should_handle(self):
            async for item in original(self, *args, **kwargs):
                yield item
            return

        try:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            contexts = list(bound.arguments.get("contexts") or [])

            rounds = _count_turn_tool_rounds(contexts)
            gate = _decide_tool_gate(rounds, contexts)
            if gate is not None:
                bound.arguments["func_tool"] = None
                contexts = list(contexts) + [gate]
                bound.arguments["contexts"] = contexts

            rewritten, changed = _rewrite_for_send(
                contexts, bound.arguments.get("tool_calls_result")
            )
            if changed or gate is not None:
                bound.arguments["contexts"] = rewritten
                if changed:
                    bound.arguments["tool_calls_result"] = None
                source = original(self, *bound.args[1:], **bound.kwargs)
            else:
                source = original(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("[工具回传改写] 流出错，按原样放行：%s", exc, exc_info=True)
            source = original(self, *args, **kwargs)

        async for item in source:
            yield item

    setattr(wrapper, _WRAPPED_FLAG, True)
    wrapper.__wrapped__ = original
    return wrapper


def _method_owner(klass: type, method_name: str) -> type | None:
    """顺着继承链找"这个方法究竟写在哪一层类上"。

    有些服务商是动态派生子类，自己没重写请求方法；只查 `klass.__dict__`
    会以为没有，于是永远包不上——而实际被调用的偏偏是父类那一个。
    """
    for base in getattr(klass, "__mro__", (klass,)):
        if method_name in base.__dict__:
            return base
    return None


def _patch_class(klass: type) -> int:
    """给一个服务商（含它继承来的实现）打上补丁，返回包住了几个方法。"""
    patched = 0
    for method_name, wrapper in (
        ("text_chat", _wrap_text_chat),
        ("text_chat_stream", _wrap_text_chat_stream),
    ):
        owner = _method_owner(klass, method_name)
        if owner is None or owner in _PATCHED_CLASSES:
            continue

        own = owner.__dict__[method_name]
        target = own
        if getattr(own, _WRAPPED_FLAG, False):
            original = getattr(own, "__wrapped__", None)
            if original is None:
                continue
            target = original

        try:
            setattr(owner, method_name, wrapper(target))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具回传改写] 包装 %s.%s 失败：%s", owner.__name__, method_name, exc)
            continue

        _PATCHED_CLASSES.add(owner)
        patched += 1
    return patched


def _install_provider_patch() -> int:
    """启动时先把已经出现的服务商类包掉（可能一个都没有，正常）。"""
    try:
        from astrbot.core.provider.provider import Provider
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具回传改写] 找不到服务商基类：%s", exc)
        return 0

    patched = 0
    queue = [Provider]
    seen: set[type] = set()
    while queue:
        klass = queue.pop()
        if klass in seen:
            continue
        seen.add(klass)
        patched += _patch_class(klass)
        queue.extend(klass.__subclasses__())
    return patched


def _install_runner_patch() -> bool:
    """挂住执行器：每来一条消息，把这次要用的服务商类当场包上。

    为什么不靠启动时枚举：插件加载得早，服务商类往往还没被导入。
    执行器每条消息都会走一次 reset，而且手里就攥着这次的服务商对象。
    """
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具回传改写] 找不到执行器：%s", exc)
        return False

    own = ToolLoopAgentRunner.__dict__.get("reset")
    if own is None:
        return False
    target = getattr(own, "__wrapped__", None) if getattr(own, _RUNNER_FLAG, False) else own
    if target is None:
        target = own

    async def wrapper(self, *args, **kwargs):
        try:
            provider = kwargs.get("provider")
            if provider is None and args:
                provider = args[0]
            if provider is not None and _patch_class(type(provider)):
                logger.info(
                    "[工具回传改写] 已当场包装服务商 %s", type(provider).__name__
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具回传改写] 当场包装失败：%s", exc)
        return await target(self, *args, **kwargs)

    setattr(wrapper, _RUNNER_FLAG, True)
    wrapper.__wrapped__ = target

    try:
        ToolLoopAgentRunner.reset = wrapper
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具回传改写] 包装执行器失败：%s", exc)
        return False


def _install_send_patch() -> bool:
    """让"发不出去"这种错至少说出它是什么错（有的底层异常一句说明都没有）。"""
    try:
        from astrbot.core.star.context import Context as StarContext
    except Exception:  # noqa: BLE001
        return False

    own = StarContext.__dict__.get("send_message")
    if own is None:
        return False
    target = getattr(own, "__wrapped__", None) if getattr(own, _SEND_FLAG, False) else own
    if target is None:
        target = own

    async def send_message(self, session, message_chain):
        try:
            return await target(self, session, message_chain)
        except Exception as exc:  # noqa: BLE001
            if not str(exc).strip():
                raise RuntimeError(type(exc).__name__) from exc
            raise

    setattr(send_message, _SEND_FLAG, True)
    send_message.__wrapped__ = target
    try:
        StarContext.send_message = send_message
        return True
    except Exception:  # noqa: BLE001
        return False


@register(
    PLUGIN_NAME,
    "咱",
    "发送前把“工具回传”整段原样换身份（只改身份），避开上游要求函数名的拒收",
    "4.2.0",
)
class ToolMessageFlattenPlugin(Star):
    """只在发请求那一刻动手：换掉工具回传，别的都不碰。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        config = config or {}

        SETTINGS.enable = bool(config.get("enable", True))
        SETTINGS.record_role = normalize_role(config.get("record_role") or "system")

        def _chars(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(config.get(key) or default)
            except (TypeError, ValueError):
                value = default
            return max(low, min(value, high))

        SETTINGS.result_max_chars = _chars(
            "result_max_chars", DEFAULT_RESULT_MAX_CHARS, 200, 8000
        )
        SETTINGS.round_max_chars = _chars(
            "round_max_chars", DEFAULT_ROUND_MAX_CHARS, 400, 20000
        )
        SETTINGS.max_rounds = _chars("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS, 1, 12)

        raw = config.get("apply_to_providers")
        keywords = str(raw).split(",") if raw is not None else ["gemini"]
        SETTINGS.keywords = [item.strip().lower() for item in keywords if item.strip()]

        patched = _install_provider_patch()
        runner_ok = _install_runner_patch()
        send_ok = _install_send_patch()
        trace_ok = trace_core.install(
            enabled=bool(config.get("trace", False)),
            max_chars=int(config.get("trace_max_chars", 0) or 0),
            console=bool(config.get("trace_console", True)),
            keep_days=int(config.get("trace_keep_days", 30) or 30),
            keep_entries=int(config.get("trace_keep_entries", 30) or 0),
        )
        self._register_trace_apis(context)

        logger.info(
            "[工具回传改写] 已加载：启用=%s，适用服务商=%s，工具最多来回 %d 轮，"
            "启动时已包装 %d 个方法，执行器挂钩=%s，报错补全=%s，抓包=%s",
            SETTINGS.enable,
            SETTINGS.keywords,
            SETTINGS.max_rounds,
            patched,
            "成功" if runner_ok else "失败",
            "已开启" if send_ok else "失败",
            ("开启 → " + trace_core.output_dir()) if trace_ok else "关闭",
        )
        if not runner_ok:
            logger.error(
                "[工具回传改写] 执行器挂钩失败：冷启动时可能一个服务商都包不上。"
                "请确认 AstrBot 版本是否还在用 ToolLoopAgentRunner。"
            )

    # ------------------------------------------------------------ 抓包界面接口

    def _register_trace_apis(self, context: Context) -> None:
        """给插件的网页界面注册四个查询接口。"""
        if json_response is None or request is None:
            logger.info(
                "[工具回传改写] 这个环境没有面板接口模块，抓包页面暂不可用；"
                "改写功能不受影响。"
            )
            return
        try:
            context.register_web_api(
                "/%s/trace/days" % PLUGIN_NAME, self.trace_days, ["GET"], "列出有抓包记录的日子"
            )
            context.register_web_api(
                "/%s/trace/entries" % PLUGIN_NAME, self.trace_entries, ["GET"], "列出某一天每条请求的摘要"
            )
            context.register_web_api(
                "/%s/trace/entry" % PLUGIN_NAME, self.trace_entry, ["GET"], "读取某一条完整请求"
            )
            context.register_web_api(
                "/%s/trace/clear" % PLUGIN_NAME, self.trace_clear, ["POST"], "删除某一天的抓包记录"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具回传改写] 注册抓包界面接口失败：%s", exc)

    async def trace_days(self):
        return json_response({"days": trace_core.list_days()})

    async def trace_entries(self):
        day = str(request.query.get("date", "") or "").strip()
        if not day:
            return json_response({"error": "缺少日期"}, status_code=400)
        return json_response({"entries": trace_core.list_entries(day)})

    async def trace_entry(self):
        day = str(request.query.get("date", "") or "").strip()
        try:
            index = int(request.query.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        if not day or index < 0:
            return json_response({"error": "参数不对"}, status_code=400)
        record = trace_core.get_entry(day, index)
        if record is None:
            return json_response({"error": "没有这条记录"}, status_code=404)
        return json_response({"entry": record})

    async def trace_clear(self):
        try:
            payload = await request.json(default={})
        except Exception:  # noqa: BLE001
            payload = {}
        day = str((payload or {}).get("date", "") or "").strip()
        if not day:
            return json_response({"error": "缺少日期"}, status_code=400)
        removed = trace_core.clear_day(day)
        return json_response({"cleared": removed, "date": day})
