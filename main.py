"""工具行为改写（入口）。

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

## 另外两件事（v4.4.0 起）

- **不裁剪**：`no_cut.py` 把 AstrBot 的轮次裁剪、超限减半、以及接口层
  "超长就删最早消息"的重试，统统换成"一条不删"。开关 `disable_truncation`。
- **示例隔离**：`examples_core.py` 在请求进执行器时把人格对话示例
  （带 `_no_save` 的那种）整段摘出来放在请求对象上，发请求前再插回
  "开头 system 之后、摘要之前"，免得它们被当成旧历史裁掉或并进摘要。
  开关 `keep_examples`。这一步跟服务商名单无关（v4.6.2 起明确）：
  名单只管工具回传改写，示例插回任何服务商都做。

## 另一件事（v4.6.0 起）

- **未来任务**：本文件末尾「原 future_task.py」那一节接管「闹钟响之后」那一段
  （v4.6.1 起并入本文件，不再单独成模块，省得热重载时旧模块壳留着）：
  历史按真消息原样放回、最后一条是「这是一个定时任务」的系统消息（任务说明＋任务正文）、
  系统提示不再被替换、工具清单不动；多轮与否由模型写在**这一发要调用的工具参数**里的
  「多轮」字段决定（写假就是最后一发；不写就按纯工具调用兜底放行两轮），正文里的
  〔多轮：是〕只作老写法兜底；停下时如果尾巴是工具回传，补一条系统消息「任务完成」
  和一条假助手「收到」，把尾巴收干净；**最后一发的正文如果没被发送工具发过，就一定
  补发**（不再因为前面某一轮发过就丢掉最后一段）；跑完照核心原版写两条假消息作记录。
  开关 `future_task_v2`，多轮闸门开关 `future_task_multi_round`。

## 维护须知（踩出来的，别删）

### 一、补丁必须"当场打"

插件加载得早，那时各家服务商的类往往还没被导入，`Provider.__subclasses__()`
是空的，一个补丁都打不上。表现：热重载装上去当天正常，冷启动重启后带工具
回传的历史又开始被拒。

所以补丁走两路：启动时把已经出现的类包掉；每条消息进执行器时，把这次要用的
服务商类当场包上。两路共用 `_PATCHED_METHODS` 去重（按「类 + 方法」记）。

### 二、热重载要能换掉旧壳

热重载会生成新的模块对象。识别旧壳后将其接到当前设置，保留其他插件的包装链。

### 三、改写只在"发送"这一层

存档由 AstrBot 自己负责，动它就是动用户的聊天记录。这里只在请求即将发出时
换掉那一条——这也是唯一能看到"最终要发出去的东西"的地方。

### 四、服务商名单只管工具链路（v4.6.2 起）

配置里那条「只对这些服务商生效」（`apply_to_providers`）只决定工具链路做不做：
轮次闸门和工具回传改写。示例插回不看它——任何服务商都照插。

示例在执行器完成上下文处理后插回，不依赖服务商名单或工具回传包装器。

"""

from __future__ import annotations

import inspect
import asyncio
import contextvars
import json
import os
import re
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

try:
    from astrbot.api.web import stream_response
except Exception:
    stream_response = None

# 原 future_task.py 用到的核心模块（整个模块已并入本文件末尾那一节）。
# 照样单独圈一层：缺一块也只是"定时任务接管"用不了，插件本体照常干活——
# 和上面面板接口同一个道理，绝不能因为这一块缺失把整个插件判成加载失败。
# 原来这几行是写在 `from . import future_task` 的 try 里的，失败只记日志、
# 退回原版唤醒；现在模块不再单独导入，就把这道判定挪到 _FUTURE_CORE_OK 上，
# 由 install() 第一步检查，效果与原来一模一样。
_FUTURE_CORE_OK = True
try:
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.message_session import MessageSession
except Exception as exc:  # noqa: BLE001
    _FUTURE_CORE_OK = False
    logger.warning(
        "[工具行为改写] 定时任务接管要用的模块没导入全，这一块不可用（其余功能照常）：%s",
        exc,
    )

from . import (
    examples_core,
    history_agent,
    no_cut,
    prompt_source,
    search_digest,
    trace_core,
    vision_bridge,
)
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
_SANITIZE_FLAG = "__toolmsg_flatten_sanitize_wrapped__"


class _Settings:
    """运行期设置，供包装后的函数读取。"""

    enable: bool = True
    keywords: list[str] = []
    record_role: str = "system"
    result_max_chars: int = DEFAULT_RESULT_MAX_CHARS
    round_max_chars: int = DEFAULT_ROUND_MAX_CHARS
    # 一次提问里，工具最多来回几轮；到了就把工具收走，逼它开口
    max_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    # 彻底关掉 AstrBot 的上下文裁剪（只 monkeypatch）
    disable_truncation: bool = True
    # 人格对话示例单独存、发请求前再插回去
    keep_examples: bool = True


SETTINGS = _Settings()

# 视觉桥接要等视觉回溯把工具注册进来才找得到，而插件加载顺序不定。
# 这里留住插件上下文，加载时没找到的话，第一次请求时再补一次。
_PLUGIN_CTX = None
_VISION_WANTED = True
_VISION_TRIED = False


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

# 本次加载已经包过的方法（记「真正定义这个方法的那个类」+ 方法名）
#
# 去重必须按「类 + 方法」：一个类上有 text_chat 和 text_chat_stream 两个方法，
# 只记类的话第二个永远进不来——流式那条从前就是这么被跳过的。
_PATCHED_METHODS: set[tuple[type, str]] = set()


def _should_handle(provider: Any) -> bool:
    """这个服务商要不要做「工具回传改写」（按配置里的关键字挑，留空就全管）。

    只管工具链路（轮次闸门 + 回传改写）；示例插回不看这个名单。
    """
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


def _config_flag(config: dict, key: str, default: bool) -> bool:
    """读一个开关。配置面板给的是布尔；手改过配置文件时可能是字符串，也认。"""
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(value)


def _no_cut_text(status: dict) -> str:
    """把「不裁剪」的状态压成一句人话，给启动日志用。"""
    if not status.get("enabled"):
        restored = len(status.get("restored") or [])
        return "关闭（已还给本体 %d 处）" % restored if restored else "关闭（本体原样）"
    if not status.get("installed"):
        return "开启但没接上（看日志）"
    fresh = len(status.get("truncator") or []) + (
        1 if status.get("pop_record") else 0
    )
    return "已接管 %d 处" % fresh if fresh else "已接管（之前装过）"


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
        "[工具行为改写] 这一问已经来回 %d 轮工具（上限 %d），这次把工具收走，逼它直接回答",
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
                    logger.warning("[工具行为改写] 解析本轮工具结果失败：%s", exc)
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
        "[工具行为改写] 这次发出去之前，把 %d 条工具回传换成了普通消息",
        sum(1 for m in normalized if isinstance(m, dict) and m.get("role") == "tool"),
    )
    return rewritten, True


def _ensure_vision_bridge() -> None:
    """第一次请求时补装视觉桥接。

    插件加载顺序不定，视觉回溯可能比我们晚加载。加载时没找到就先记着，
    等到第一次真正发请求的时候再试一次——那时它的工具一定已经注册好了。
    """
    global _VISION_TRIED
    if _VISION_TRIED or _PLUGIN_CTX is None:
        return
    if not _VISION_WANTED:
        _VISION_TRIED = True
        return
    try:
        state = vision_bridge.install(_PLUGIN_CTX, True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具行为改写] 视觉桥接补装失败：%s", exc)
        _VISION_TRIED = True
        return
    if "没装上" in state:
        return  # 留个机会下次再试
    logger.info("[工具行为改写] 视觉桥接补装：%s", state)
    _VISION_TRIED = True


def _wrap_text_chat(original):
    """包住非流式请求，工具回传改写看服务商名单。"""
    signature = inspect.signature(original)

    async def wrapper(self, *args, **kwargs):
        _ensure_vision_bridge()
        rewrite_on = _should_handle(self)
        try:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            contexts = list(bound.arguments.get("contexts") or [])

            touched = False
            tools_changed = False
            send = contexts

            if rewrite_on:
                # 1) 工具查了没完没了就把工具收走（本次请求的临时决定，存储不动）。
                gate = _decide_tool_gate(_count_turn_tool_rounds(contexts), contexts)
                if gate is not None:
                    bound.arguments["func_tool"] = None
                    send = list(send) + [gate]
                    touched = True

                # 2) 把"工具回传"换成普通消息
                rewritten, tools_changed = _rewrite_for_send(
                    send, bound.arguments.get("tool_calls_result")
                )
                if tools_changed or gate is not None:
                    send = rewritten
                if tools_changed:
                    touched = True

            if not touched:
                return await original(self, *args, **kwargs)

            bound.arguments["contexts"] = send
            if tools_changed:
                bound.arguments["tool_calls_result"] = None
            return await original(self, *bound.args[1:], **bound.kwargs)
        except Exception as exc:  # noqa: BLE001
            # 改写出错绝不能把用户的对话打挂：原样放行，把原因留在日志里
            logger.error(
                "[工具行为改写] 出错，这一次按原样放行：%s", exc, exc_info=True
            )
            return await original(self, *args, **kwargs)

    setattr(wrapper, _WRAPPED_FLAG, True)
    wrapper.__wrapped__ = original
    return wrapper


def _wrap_text_chat_stream(original):
    """包住流式请求，工具回传改写看服务商名单。"""
    signature = inspect.signature(original)

    async def wrapper(self, *args, **kwargs):
        _ensure_vision_bridge()
        rewrite_on = _should_handle(self)
        try:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            contexts = list(bound.arguments.get("contexts") or [])

            touched = False
            tools_changed = False
            send = contexts

            if rewrite_on:
                gate = _decide_tool_gate(_count_turn_tool_rounds(contexts), contexts)
                if gate is not None:
                    bound.arguments["func_tool"] = None
                    send = list(send) + [gate]
                    touched = True

                rewritten, tools_changed = _rewrite_for_send(
                    send, bound.arguments.get("tool_calls_result")
                )
                if tools_changed or gate is not None:
                    send = rewritten
                if tools_changed:
                    touched = True

            if touched:
                bound.arguments["contexts"] = send
                if tools_changed:
                    bound.arguments["tool_calls_result"] = None
                source = original(self, *bound.args[1:], **bound.kwargs)
            else:
                source = original(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("[工具行为改写] 流出错，按原样放行：%s", exc, exc_info=True)
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


def _owned_wrappers(function, flag):
    """Find our wrappers without trusting markers copied by functools.wraps."""
    seen = set()
    while function is not None and id(function) not in seen:
        seen.add(id(function))
        env = getattr(function, "__globals__", {})
        if getattr(function, "__dict__", {}).get(flag) and env.get("examples_core") is not None:
            yield function, env
        function = getattr(function, "__wrapped__", None)


def _adopt_wrapper(env):
    """Reconnect a surviving wrapper to the current request-local implementation."""
    old = env.get("examples_core")
    if old is None or old is examples_core:
        return
    try:
        env["examples_core"] = examples_core
        env["SETTINGS"] = SETTINGS
        env["_patch_class"] = _patch_class
    except Exception as exc:  # noqa: BLE001
        logger.warning("[示例隔离] 旧包装器接入失败，请重启 AstrBot：%s", exc)
        raise


def _patch_class(klass: type) -> int:
    """给一个服务商（含它继承来的实现）打上补丁，返回包住了几个方法。"""
    patched = 0
    for method_name, wrapper in (
        ("text_chat", _wrap_text_chat),
        ("text_chat_stream", _wrap_text_chat_stream),
    ):
        owner = _method_owner(klass, method_name)
        if owner is None:
            continue
        own = owner.__dict__[method_name]
        existing = list(_owned_wrappers(own, _WRAPPED_FLAG))
        for _, env in existing:
            _adopt_wrapper(env)
        if existing:
            # An outer third-party wrapper must not be discarded.
            if existing[0][0] is not own or existing[0][1] is globals():
                _PATCHED_METHODS.add((owner, method_name))
                continue
            target = getattr(own, "__wrapped__", None)
            if target is None:
                continue
        else:
            target = own

        try:
            setattr(owner, method_name, wrapper(target))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具行为改写] 包装 %s.%s 失败：%s", owner.__name__, method_name, exc)
            continue

        _PATCHED_METHODS.add((owner, method_name))
        patched += 1
    return patched


def _install_sanitize_patch(runner_class) -> bool:
    """Restore this request's examples only after context processing."""
    own = runner_class.__dict__.get("_sanitize_contexts_for_provider")
    if own is None:
        logger.error("[示例隔离] 找不到发送前上下文处理方法，示例隔离已关闭")
        return False
    existing = list(_owned_wrappers(own, _SANITIZE_FLAG))
    if existing:
        try:
            for _, env in existing:
                _adopt_wrapper(env)
        except Exception:  # noqa: BLE001
            return False
        return True

    def wrapper(runner, contexts, *args, **kwargs):
        processed = own(runner, contexts, *args, **kwargs)
        return examples_core.insert_for_request(processed, getattr(runner, "req", None))

    setattr(wrapper, _SANITIZE_FLAG, True)
    wrapper.__wrapped__ = own
    runner_class._sanitize_contexts_for_provider = wrapper
    return True


def _install_provider_patch() -> int:
    """启动时先把已经出现的服务商类包掉（可能一个都没有，正常）。"""
    try:
        from astrbot.core.provider.provider import Provider
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具行为改写] 找不到服务商基类：%s", exc)
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
    """挂住执行器：每来一条消息，把这次要用的服务商类当场包上，顺手把人格示例摘出来。

    为什么不靠启动时枚举：插件加载得早，服务商类往往还没被导入。
    执行器每条消息都会走一次 reset，而且手里就攥着这次的服务商对象和这份请求。

    摘示例必须在这里做：AstrBot 是在进执行器之前把示例插到上下文最前面的，
    捏着这份请求的时机只有这里；等发请求时再摘，就被裁剪和压缩先动过手了。
    """
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具行为改写] 找不到执行器：%s", exc)
        return False

    own = ToolLoopAgentRunner.__dict__.get("reset")
    if own is None:
        return False
    if not _install_sanitize_patch(ToolLoopAgentRunner):
        examples_core.SETTINGS.keep_examples = False
        try:
            for _, env in _owned_wrappers(own, _RUNNER_FLAG):
                _adopt_wrapper(env)
        except Exception:  # noqa: BLE001
            logger.error("[示例隔离] 旧包装器无法安全停用，请重启 AstrBot")
        return False
    existing = list(_owned_wrappers(own, _RUNNER_FLAG))
    if existing:
        try:
            for _, env in existing:
                _adopt_wrapper(env)
        except Exception:  # noqa: BLE001
            return False
        return True
    target = own

    async def wrapper(self, *args, **kwargs):
        request = kwargs.get("request")
        if request is None and len(args) >= 2:
            request = args[1]
        if request is not None and examples_core.SETTINGS.keep_examples:
            try:
                taken = examples_core.capture(request)
                if taken:
                    logger.debug(
                        "[示例隔离] 摘出人格示例 %d 条（会话=%s）",
                        taken,
                        examples_core.session_key_of(request) or "（无标识）",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[示例隔离] 摘出人格示例失败，这一次按原样：%s", exc)

        try:
            provider = kwargs.get("provider")
            if provider is None and args:
                provider = args[0]
            if provider is not None and _patch_class(type(provider)):
                logger.info(
                    "[工具行为改写] 已当场包装服务商 %s", type(provider).__name__
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具行为改写] 当场包装失败：%s", exc)
        return await target(self, *args, **kwargs)

    setattr(wrapper, _RUNNER_FLAG, True)
    wrapper.__wrapped__ = target

    try:
        ToolLoopAgentRunner.reset = wrapper
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[工具行为改写] 包装执行器失败：%s", exc)
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
    "3303339695",
    "在请求发出前动手术：搜索只掏摘要、会话历史交给助手模型整理、看不了图的模型借图片转述、请求体检、提示词来源追踪、抓包排障",
    "5.2.0",
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

        # 两个新开关：一个管"别裁"，一个管"示例单独存"
        SETTINGS.disable_truncation = _config_flag(config, "disable_truncation", True)
        examples_core.SETTINGS.keep_examples = _config_flag(
            config, "keep_examples", True
        )

        patched = _install_provider_patch()
        runner_ok = _install_runner_patch()
        send_ok = _install_send_patch()
        no_cut_status = no_cut.install(SETTINGS.disable_truncation)
        digest_state = search_digest.install(
            _config_flag(config, "search_digest", True),
            _chars("search_digest_max_chars", 400, 80, 4000),
        )
        try:
            history_state = history_agent.install(
                context,
                _config_flag(config, "history_agent", True),
                str(config.get("history_agent_provider") or ""),
                _chars("history_agent_max_rounds", 4, 1, 12),
                _chars("history_agent_batch", 50, 1, 50),
                _chars("history_agent_keep", 3000, 50, 20000),
            )
        except Exception as exc:  # noqa: BLE001
            history_state = "没装上（%s）" % exc
            logger.warning("[工具行为改写] 会话历史接管没装上：%s", exc)

        # 视觉桥接：得等视觉回溯把工具注册进来才找得到，加载顺序不一定，
        # 所以先把入口记下，第一次请求时再补一次（见 _ensure_vision_bridge）。
        global _PLUGIN_CTX, _VISION_WANTED
        _PLUGIN_CTX = context
        _VISION_WANTED = _config_flag(config, "vision_bridge", True)
        vision_state = vision_bridge.install(context, _VISION_WANTED)

        try:
            source_state = prompt_source.install()
        except Exception as exc:  # noqa: BLE001
            source_state = "没装上（%s）" % exc
            logger.warning("[工具行为改写] 提示词来源追踪没装上：%s", exc)
        trace_ok = trace_core.install(
            enabled=bool(config.get("trace", False)),
            max_chars=int(config.get("trace_max_chars", 0) or 0),
            console=bool(config.get("trace_console", True)),
            keep_days=int(config.get("trace_keep_days", 30) or 30),
            keep_entries=int(config.get("trace_keep_entries", 30) or 0),
            check=_config_flag(config, "request_check", True),
        )
        future_state = "关闭"
        if _config_flag(config, "future_task_v2", True):
            try:
                if install(context):
                    future_state = "已接管"
                else:
                    future_state = "未启用"
            except Exception:  # noqa: BLE001
                logger.exception("[工具行为改写] 未来任务接管安装失败，保留原版唤醒")
                future_state = "未启用"
        else:
            # 开关关掉时不能只是"这次不装"：本体那个类在热重载里是同一个对象，
            # 上次装上去的方法不会自己消失。不主动还回去，关掉开关也照样接管。
            try:
                future_state = "已还给本体" if uninstall() else "没还干净（要重启）"
            except Exception:  # noqa: BLE001
                logger.exception("[工具行为改写] 卸载未来任务接管失败")
                future_state = "没还干净（要重启）"
        self._register_trace_apis(context)
        self._register_file_apis(context)

        logger.info(
            "[工具行为改写] 已加载：启用=%s，适用服务商=%s，工具最多来回 %d 轮，"
            "启动时已包装 %d 个方法，执行器挂钩=%s，报错补全=%s，抓包=%s；"
            "联网搜索摘要=%s，会话历史接管=%s，视觉桥接=%s，来源追踪=%s，不裁剪=%s，示例隔离=%s，未来任务=%s",
            SETTINGS.enable,
            SETTINGS.keywords,
            SETTINGS.max_rounds,
            patched,
            "成功" if runner_ok else "失败",
            "已开启" if send_ok else "失败",
            (
                ("开启 → " + trace_core.output_dir())
                if trace_ok
                else ("关闭（已摘钩）" if not bool(config.get("trace", False)) else "没装上")
            ),
            digest_state,
            history_state,
            vision_state,
            source_state,
            _no_cut_text(no_cut_status),
            "开启" if examples_core.SETTINGS.keep_examples else "关闭",
            future_state,
        )
        if not no_cut_status.get("installed") and no_cut_status.get("enabled"):
            logger.warning(
                "[上下文不裁剪] 没接上：上下文仍然会被裁。可能是 AstrBot 改了实现，"
                "请看上面那条具体是哪一处失败。"
            )
        if not runner_ok:
            logger.error(
                "[工具行为改写] 执行器挂钩失败：冷启动时可能一个服务商都包不上。"
                "请确认 AstrBot 版本是否还在用 ToolLoopAgentRunner。"
            )

    # ------------------------------------------------------------ 抓包界面接口

    def _register_trace_apis(self, context: Context) -> None:
        """给插件的网页界面注册查询接口。"""
        if json_response is None or request is None:
            logger.info(
                "[工具行为改写] 这个环境没有面板接口模块，抓包页面暂不可用；"
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
                "/%s/trace/open_dir" % PLUGIN_NAME, self.trace_open_dir, ["POST"], "打开抓包目录"
            )
            context.register_web_api(
                "/%s/trace/summary" % PLUGIN_NAME, self.trace_summary, ["GET"], "某一天的用量汇总"
            )
            context.register_web_api(
                "/%s/trace/entry" % PLUGIN_NAME, self.trace_entry, ["GET"], "读取某一条完整请求"
            )
            if stream_response is not None:
                context.register_web_api(
                    "/%s/trace/events" % PLUGIN_NAME, self.trace_events, ["GET"], "抓包变更事件"
                )
            context.register_web_api(
                "/%s/trace/clear" % PLUGIN_NAME, self.trace_clear, ["POST"], "删除某一天的抓包记录"
            )
            context.register_web_api(
                "/%s/trace/stop_all" % PLUGIN_NAME, self.trace_stop_all, ["POST"], "终止所有会话里正在运行的任务"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具行为改写] 注册抓包界面接口失败：%s", exc)

    async def trace_days(self):
        return json_response({"days": trace_core.list_days()})

    async def trace_events(self):
        day = str(request.query.get("date", "") or "").strip()
        subscriber = trace_core.subscribe_changes()

        async def events():
            try:
                yield "data: {}\n\n"
                while True:
                    delay = trace_core.next_timeout(day)
                    try:
                        await asyncio.wait_for(subscriber[1].get(), timeout=min(delay, 25) if delay else 25)
                        yield "data: {}\n\n"
                    except asyncio.TimeoutError:
                        yield "data: {}\n\n" if delay and delay <= 25 else ": keepalive\n\n"
            finally:
                trace_core.unsubscribe_changes(subscriber)

        return stream_response(events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def trace_entries(self):
        day = str(request.query.get("date", "") or "").strip()
        if not day:
            return json_response({"error": "缺少日期"}, status_code=400)
        # 列表顺带把当天的汇总带上，省得页面再请求一次
        try:
            query = str(request.query.get("q", "") or "")
            entries = trace_core.list_entries(day, query)
            return json_response({
                "entries": entries[:200] if query else entries,
                "truncated": bool(query and len(entries) > 200),
                "summary": trace_core.summarize(day),
            })
        except Exception as exc:
            logger.warning("[工具行为改写] 检索抓包失败：%s", exc)
            return json_response({"error": "读取抓包失败"}, status_code=500)

    async def trace_open_dir(self):
        try:
            return json_response(trace_core.open_output_dir())
        except Exception as exc:
            logger.warning("[工具行为改写] 打开抓包目录失败：%s", exc)
            return json_response({"opened": False, "path": str(self._trace_dir().resolve())})

    async def trace_summary(self):
        day = str(request.query.get("date", "") or "").strip()
        if not day:
            return json_response({"error": "缺少日期"}, status_code=400)
        return json_response({"summary": trace_core.summarize(day)})

    async def trace_entry(self):
        day = str(request.query.get("date", "") or "").strip()
        try:
            index = int(request.query.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        record_id = str(request.query.get("id", "") or "").strip()
        if not day or (index < 0 and not record_id):
            return json_response({"error": "参数不对"}, status_code=400)
        record = trace_core.get_entry(day, index, record_id)
        if record is None:
            return json_response({"error": "没有这条记录"}, status_code=404)
        # 原样带出，`usage` 是原封不动的一整段（没有就是没有）
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

    async def trace_stop_all(self):
        """终止全部：把 /stop 那套请求，对所有会话各来一遍。

        /stop 只认当前会话；这里把登记表里所有会话都翻出来，一个个发停止请求。
        用的是"只请求停 Agent、不中断事件传播"那一个，跟 /stop 的手感一致——
        这样收尾动作（保存历史之类）仍然会走完，不会留下半截记录。
        """
        try:
            from astrbot.core.utils.active_event_registry import (
                active_event_registry as registry,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具行为改写] 拿不到任务登记表：%s", exc)
            return json_response({"ok": False, "error": "这个版本拿不到任务登记表"})

        # 活跃事件和后台任务分两个口袋存，两个都要翻
        umos = set(getattr(registry, "_events", {}) or {})
        umos |= set(getattr(registry, "_background_tasks", {}) or {})

        sessions = len(umos)
        stopped = 0
        failed = 0
        for umo in list(umos):
            try:
                stopped += int(registry.request_agent_stop_all(umo))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("[工具行为改写] 终止 %s 失败：%s", umo, exc)

        logger.warning(
            "[工具行为改写] 抓包页点了终止：%d 个会话，%d 个任务收到停止请求，失败 %d",
            sessions,
            stopped,
            failed,
        )
        return json_response({
            "ok": True,
            "sessions": sessions,
            "stopped": stopped,
            "failed": failed,
        })

    # ------------------------------------------------------------ 抓包文件管理

    def _trace_dir(self):
        from pathlib import Path as _Path
        return _Path(__file__).resolve().parents[2] / "plugin_data" / "api_trace"

    def _register_file_apis(self, context: Context) -> None:
        if json_response is None or request is None:
            return
        try:
            context.register_web_api(
                "/%s/trace/files" % PLUGIN_NAME, self.trace_files, ["GET"], "列出抓包文件"
            )
            context.register_web_api(
                "/%s/trace/file" % PLUGIN_NAME, self.trace_file, ["GET"], "预览一个抓包文件"
            )
            context.register_web_api(
                "/%s/trace/file_delete" % PLUGIN_NAME, self.trace_file_delete, ["POST"], "删除一个抓包文件"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[工具行为改写] 注册抓包文件接口失败：%s", exc)

    async def trace_files(self):
        d = self._trace_dir()
        items = []
        if d.exists():
            for p in d.iterdir():
                if p.is_file():
                    st = p.stat()
                    items.append({"name": p.name, "size": st.st_size})
        items.sort(key=lambda x: x["name"], reverse=True)
        return json_response({"dir": str(d), "files": items})

    async def trace_file(self):
        name = str(request.query.get("name", "") or "")
        base = self._trace_dir().resolve()
        p = (base / name).resolve()
        if base != p.parent or not p.is_file():
            return json_response({"error": "文件不存在"}, status_code=400)
        limit = 200 * 1024
        size = p.stat().st_size
        with p.open("rb") as f:
            head = f.read(limit)
        return json_response({
            "name": p.name, "size": size, "truncated": size > limit,
            "text": head.decode("utf-8", "replace"),
        })

    async def trace_file_delete(self):
        try:
            body = await request.get_json(silent=True)
        except Exception:
            body = None
        name = str((body or {}).get("name", "") or "")
        base = self._trace_dir().resolve()
        p = (base / name).resolve()
        if base != p.parent or not p.is_file():
            return json_response({"ok": False, "error": "文件不存在"})
        p.unlink()
        return json_response({"ok": True, "name": p.name})


# ============================================================================
# 定时任务：薄接管（v4.11.2）
#
# 原则：说什么、怎么记，全交回本体。插件只做三件事——
#
#   1. 把"启动时刻"换成本地时间文本再交给本体（本体给的是世界时，模型多半会把它
#      当成本地时间再算一次，于是"现在"差了八小时）；
#   2. 跑完之后，把这次唤醒里她说过的正文送到目标会话；
#   3. 她要是自己用发送工具发过，纯正文就不再补发（避免一次收到两条）。
#
# 为什么不替换本体的唤醒方法：那边每一处都跟着本体版本走（错误态、插件白名单、
# 日志上下文、安全模式、步数、入库格式）。整段抄一遍就多一处脱节——上一版抄出来
# 五处。所以这里只包一层：改一个字段、原样跑、跑完补投递。
#
# 正文从哪来：本体把执行器攥在方法里面，外面看不到。但执行器每一轮都会走
# `step()`，插件在那个方法上挂个采集器：每轮结束看一眼"这一轮新说了什么"。
# 采集器同时挂在执行器对象和一张小本子上，唤醒方法跑完再取回来。
# ============================================================================

_RECORDER_ATTR = "_cron_text_recorder"
_STEP_FLAG = "__cron_step_probe__"
_PATCH_FLAG = "__future_task_v2__"
_RECORDERS: dict[str, Any] = {}


def _message_text(message) -> str:
    """把一条消息的正文抠成纯文字（字符串或文本块列表都能处理）。"""
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if str(part.get("type", "")).lower() == "text":
                    chunks.append(str(part.get("text") or ""))
                continue
            if str(getattr(part, "type", "") or "").lower() == "text":
                chunks.append(str(getattr(part, "text", "") or ""))
        return "".join(chunks)
    return ""



def _local_time_text(value: Any) -> str:
    """把本体给的时刻换成本机时间再显示；解析失败就原样返回。"""
    text = str(value or "").strip()
    if not text:
        return "（未知）"
    try:
        from datetime import datetime

        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return text


def _adjust_cron_extras(extras: Any) -> None:
    """只改一个字段：启动时刻。其余一个字不动，提示词也不碰。"""
    if not isinstance(extras, dict):
        return
    job = extras.get("cron_job")
    if not isinstance(job, dict):
        return
    original = job.get("run_started_at")
    local = _local_time_text(original)
    if local and local != original:
        job["run_started_at"] = local
        logger.info("[未来任务] 启动时刻换成本地时间：%s → %s", original, local)


class _TextRecorder:
    """记下这次唤醒里她说过的每一段正文，以及那一段有没有同时申请工具。"""

    def __init__(self, runner, event) -> None:
        self.runner = runner
        self.event = event
        messages = getattr(getattr(runner, "run_context", None), "messages", None) or []
        self._seen = {id(message) for message in messages}
        self.items: list[tuple[str, bool]] = []

    def sample(self) -> int:
        """看一眼这一轮新说的那段话；记过的不再记。"""
        messages = getattr(getattr(self.runner, "run_context", None), "messages", None) or []
        for message in reversed(messages):
            role = _role_of(message)
            if role == "tool":
                continue
            if role != "assistant":
                break
            if id(message) in self._seen:
                break
            self._seen.add(id(message))
            text = str(_message_text(message) or "").strip()
            if text:
                self.items.append((text, bool(_value_of(message, "tool_calls"))))
                return 1
            break
        return 0


def _current_cron_event():
    """拿"当前正在跑的那个事件"；不是定时唤醒就给空。"""
    try:
        from astrbot.core.platform.astr_message_event import current_message_event
    except Exception:  # noqa: BLE001
        return None
    try:
        event = current_message_event.get()
    except Exception:  # noqa: BLE001
        return None
    if event is None:
        return None
    try:
        if not event.get_extra("cron_job"):
            return None
    except Exception:  # noqa: BLE001
        return None
    return event


def _ensure_recorder(runner):
    """第一次采集时才建：先认这次是不是定时唤醒。"""
    recorder = getattr(runner, _RECORDER_ATTR, None)
    if recorder is not None:
        return recorder
    event = _current_cron_event()
    if event is None:
        return None
    recorder = _TextRecorder(runner, event)
    try:
        setattr(runner, _RECORDER_ATTR, recorder)
    except Exception:  # noqa: BLE001
        logger.exception("[未来任务] 采集器挂到执行器上失败")
    try:
        _RECORDERS[str(event.unified_msg_origin)] = recorder
    except Exception:  # noqa: BLE001
        logger.exception("[未来任务] 采集器登记失败")
    return recorder


async def _sample_after_step(runner) -> None:
    try:
        recorder = _ensure_recorder(runner)
        if recorder is not None:
            recorder.sample()
    except Exception:  # noqa: BLE001
        logger.exception("[未来任务] 采集这一轮正文失败")


def _install_step_patch() -> bool:
    """在执行器每一轮结束处挂个采集：不改任何行为，只看它新说了什么。"""
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    except Exception as exc:  # noqa: BLE001
        logger.warning("[未来任务] 找不到执行器，正文采集不可用：%s", exc)
        return False
    own = ToolLoopAgentRunner.__dict__.get("step")
    if own is None:
        logger.warning("[未来任务] 执行器没有 step，正文采集不可用")
        return False
    # 每次加载都重新包一遍：旧壳的闭包抓的是旧模块，重载后它会把正文记到旧本子上
    target = getattr(own, "__wrapped__", None) if getattr(own, _STEP_FLAG, False) else own
    if target is None:
        target = own

    async def step(self, *args, **kwargs):
        # 采集器必须在这一步开始之前就建好：它的"基线"要停在"她开口之前"。
        # 要是等跑完这一步再建，她一步就说完了的那种，那一段会被当成历史，
        # 一个字都发不出去（2026-11-15 二十三点那次就是这么丢的）。
        _ensure_recorder(self)
        async for item in target(self, *args, **kwargs):
            yield item
        await _sample_after_step(self)

    setattr(step, _STEP_FLAG, True)
    step.__wrapped__ = target
    try:
        ToolLoopAgentRunner.step = step
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[未来任务] 包执行器失败，正文采集不可用：%s", exc)
        return False


# --------------------------------------------------------------- 投递

async def _aiocqhttp_self_id(bot) -> str:  # type: ignore[misc]
    """取出机器人自己的号：优先看已连接的协议端，其次看接口客户端。"""
    clients = getattr(bot, "_wsr_event_clients", None) or set()
    for websocket in clients:
        headers = getattr(websocket, "headers", None)
        try:
            value = headers.get("X-Self-ID") if headers is not None else ""
        except Exception:  # noqa: BLE001
            value = ""
        if value:
            return str(value)
    api_clients = getattr(bot, "_wsr_api_clients", None)
    if isinstance(api_clients, dict) and api_clients:
        return str(next(iter(api_clients)))
    return ""


def _aiocqhttp_platform(ctx, session):
    try:
        for platform in ctx.platform_manager.platform_insts:
            if platform.meta().id == session.platform_name:
                return platform
    except Exception:  # noqa: BLE001
        return None
    return None


async def _send_via_aiocqhttp(ctx, session, chain) -> bool:
    """直接调协议端接口发一条，显式带上机器人自己的号。

    为什么要有这条路：反向连接下如果接口客户端不止一个，通用发送不带自己的号时
    认不出该用哪条连接，会抛"接口不可用"。
    """
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    platform = _aiocqhttp_platform(ctx, session)
    bot = getattr(platform, "bot", None) if platform is not None else None
    if bot is None:
        return False
    self_id = await _aiocqhttp_self_id(bot)
    if not self_id:
        return False
    is_group = session.message_type == MessageType.GROUP_MESSAGE
    session_id = session.session_id.split("_")[-1] if is_group else session.session_id
    messages = await AiocqhttpMessageEvent._parse_onebot_json(chain)
    if not messages:
        return False
    if is_group:
        await bot.call_action(
            "send_group_msg", self_id=self_id, group_id=session_id, message=messages
        )
    else:
        await bot.call_action(
            "send_private_msg", self_id=self_id, user_id=session_id, message=messages
        )
    return True


async def _deliver_chain_with_retry(ctx, session, chain, attempts: int = 5) -> bool:
    """把一条消息链发到目标会话；平台接口瞬时不可用时重试几次再认输。"""
    last_error = ""
    for index in range(1, attempts + 1):
        try:
            sent = await ctx.send_message(session, chain)
            if sent:
                return True
            # 不抛异常、但返回了假值：平台没找到，等于没发出去。
            # 这里必须看返回值，不能"调过就算成功"。
            last_error = "通用发送返回假值（没找到对应平台）"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        try:
            if await _send_via_aiocqhttp(ctx, session, chain):
                logger.info("[未来任务] 通用发送失败，已改用协议端直发成功")
                return True
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[未来任务] 第 %d/%d 次投递没成功：%s", index, attempts, last_error
        )
        if index < attempts:
            await asyncio.sleep(min(1.0 * index, 4.0))
    logger.error("[未来任务] 投递连续 %d 次失败，最后的错误：%s", attempts, last_error)
    return False


async def _deliver_with_retry(ctx, session, text: str, attempts: int = 5) -> bool:
    """把一段纯正文发到目标会话（顺手包成一条消息链）。"""
    return await _deliver_chain_with_retry(
        ctx, session, MessageChain().message(text), attempts
    )


def _pick_texts(recorder, sent_by_tool: bool) -> list[str]:
    """挑这次要发哪些正文。

    她要是自己用发送工具发过：纯正文（那一发没有同时申请工具）就不补发，
    免得一次收到两条；但"一边说话一边申请工具"的那一段仍然算她说的话，要发。

    她要是没用过发送工具：所有正文按顺序拼成一条发出去。
    """
    items = list(getattr(recorder, "items", []) or [])
    picked: list[str] = []
    for item in items:
        try:
            text, has_tools = item
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        if sent_by_tool and not has_tools:
            continue
        picked.append(str(text))
    return picked


async def _deliver_cron_texts(ctx, session, recorder, sent_by_tool: bool) -> bool:
    texts = _pick_texts(recorder, sent_by_tool)
    if not texts:
        return False
    body = "\n\n".join(texts).strip()
    if not body:
        return False
    logger.info(
        "[未来任务] 补发正文 %d 段、共 %d 字（她用发送工具发过=%s）",
        len(texts),
        len(body),
        sent_by_tool,
    )
    return await _deliver_with_retry(ctx, session, body)


def _event_sent_by_tool(recorder) -> bool:
    """这次唤醒里，她有没有自己用发送工具发到当前会话。"""
    event = getattr(recorder, "event", None)
    if event is None:
        return False
    if bool(getattr(event, "_has_send_oper", False)):
        return True
    try:
        texts = event.get_extra(
            "_send_message_to_user_current_session_plain_texts", []
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(texts, list) and texts)


async def _deliver_after_run(ctx, key: str, session_str, delivery_session_str: str) -> bool:
    """唤醒跑完之后的统一投递：一次唤醒只发一条。"""
    recorder = _RECORDERS.pop(key, None)
    if recorder is None:
        logger.info("[未来任务] 这次唤醒没采到正文（可能她一个字都没说）")
        return False
    if not delivery_session_str:
        return False
    try:
        session = (
            session_str
            if isinstance(session_str, MessageSession)
            else MessageSession.from_str(str(session_str))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[未来任务] 目标会话解析失败，补发跳过：%s", exc)
        return False
    return await _deliver_cron_texts(
        ctx, session, recorder, _event_sent_by_tool(recorder)
    )


# ------------------------------------------------------------------- 安装

def install(ctx) -> bool:
    """把唤醒方法包一层。重复调用/热重载都安全。"""
    if not _FUTURE_CORE_OK:
        logger.warning("[未来任务] 缺所需的 AstrBot 模块，保持原版唤醒")
        return False
    step_ok = _install_step_patch()
    if not step_ok:
        logger.warning("[未来任务] 正文采集没挂上：这次接管只能改时间，发不出正文")
    try:
        import astrbot.core.cron.manager as cron_manager_module

        manager_cls = cron_manager_module.CronJobManager
        existing = manager_cls.__dict__.get("_woke_main_agent")
        if existing is None:
            logger.warning("[未来任务] 找不到唤醒方法，保持原样")
            return False

        # 热重载时旧壳闭包里引用的是旧模块与旧上下文，必须剥掉重包
        if getattr(existing, _PATCH_FLAG, False):
            original = getattr(existing, "__wrapped__", None)
            if original is None:
                original = existing
        else:
            original = existing

        async def woke_main_agent(
            self,
            *,
            message: str,
            session_str: str,
            extras: dict,
            delivery_session_str: str = "",
        ) -> None:
            try:
                _adjust_cron_extras(extras)
            except Exception:  # noqa: BLE001
                logger.exception("[未来任务] 改启动时刻失败，按原样交给本体")
            key = str(session_str or "")
            try:
                await original(
                    self,
                    message=message,
                    session_str=session_str,
                    extras=extras,
                    delivery_session_str=delivery_session_str,
                )
            except Exception:
                # 跑挂了也尽量把已经说出来的发出去，再把错误原样抛回去让任务记失败
                try:
                    await _deliver_after_run(
                        ctx, key, session_str, delivery_session_str
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[未来任务] 出错后补发也失败")
                raise
            try:
                await _deliver_after_run(ctx, key, session_str, delivery_session_str)
            except Exception:  # noqa: BLE001
                logger.exception("[未来任务] 补发正文失败")

        setattr(woke_main_agent, _PATCH_FLAG, True)
        woke_main_agent.__wrapped__ = original
        manager_cls._woke_main_agent = woke_main_agent
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[未来任务] 安装失败，保持原版唤醒")
        return False


def uninstall() -> bool:
    """把唤醒方法还给本体。开关关掉时用。

    为什么必须有这一步：本体那个排班管家类在热重载里始终是同一个对象。上一次加载
    时把"唤醒方法"换成了插件版，之后就算开关关掉、插件重新加载，只要"这次不装"，
    那个旧的插件版也还挂在类上——于是关掉开关照样接管。只有把方法真的换回被包住的
    那个原件，才叫关掉。
    """
    try:
        import astrbot.core.cron.manager as cron_manager_module

        manager_cls = cron_manager_module.CronJobManager
        current = manager_cls.__dict__.get("_woke_main_agent")
        if current is None:
            logger.warning("[未来任务] 找不到唤醒方法，没得还")
            return False
        if not getattr(current, _PATCH_FLAG, False):
            logger.info("[未来任务] 唤醒方法本来就是本体的原件，不用还")
            return True
        original = getattr(current, "__wrapped__", None)
        if original is None:
            logger.warning("[未来任务] 找不到被包住的原件，还不了，只能重启本体")
            return False
        manager_cls._woke_main_agent = original
        logger.info("[未来任务] 已把唤醒方法还给本体，下次响铃走原版")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[未来任务] 卸载接管失败，只能重启本体才回到原版")
        return False
