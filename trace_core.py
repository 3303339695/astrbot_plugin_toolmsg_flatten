"""接口抓包（并进"工具调用改写"插件里的一份子功能）。

## 目的

把**每次真正发给模型的内容**完整落盘，回答这类问题：

- 模型到底收到了几个工具？
- 系统提示里到底塞了什么？
- 历史消息有多少条、每条说了什么？
- 有没有 `role:"tool"` 这种原生工具结构？
- 模型回报的模型名是什么？回答原文是什么？

## 挂在哪一层

不挂 AstrBot 的服务商方法——那会被其它包装顺序影响，可能抓到"改写前"的内容。
这里直接挂**官方接口库发请求的那个函数**，拿到的是最终请求体，
不管前面套了几层，看到的都是模型真正收到的。

## 只读不改

只读取参数、写文件，不改任何请求内容。
"""

from __future__ import annotations

import json
import os
import sys
import asyncio
import hashlib
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

_PATCH_FLAG = "__toolmsg_flatten_trace_patched__"

# 自己的插件目录名：判断"谁发起的"时要跳过，不然永远认成自己
_SELF_PLUGIN = "astrbot_plugin_toolmsg_flatten"


class _TraceSettings:
    """抓包的运行期设置。"""

    enable: bool = False
    max_chars: int = 0
    console: bool = True
    keep_days: int = 30
    # 每天最多保留多少条（0 表示不限）
    keep_entries: int = 30
    # 请求体检：只报告，不动手
    check: bool = True


SETTINGS = _TraceSettings()

# 每天已有多少条，避免每次都去数一遍大文件
_ENTRY_COUNTS: dict[str, int] = {}
_LOCK = threading.RLock()
_SUBSCRIBERS: set[tuple[Any, Any]] = set()


def subscribe_changes() -> tuple[Any, Any]:
    subscriber = (asyncio.get_running_loop(), asyncio.Queue(maxsize=1))
    with _LOCK:
        _SUBSCRIBERS.add(subscriber)
    return subscriber


def unsubscribe_changes(subscriber: tuple[Any, Any]) -> None:
    with _LOCK:
        _SUBSCRIBERS.discard(subscriber)


def _notify_changes() -> None:
    def notify(queue: Any) -> None:
        if queue.empty():
            queue.put_nowait(True)

    with _LOCK:
        for loop, queue in tuple(_SUBSCRIBERS):
            try:
                loop.call_soon_threadsafe(notify, queue)
            except Exception:
                _SUBSCRIBERS.discard((loop, queue))


def _effective_status(record: dict[str, Any]) -> str:
    status = record.get("status") or "complete"
    if status == "pending":
        try:
            if time.time() - datetime.fromisoformat(record["time"]).timestamp() >= 600:
                return "timeout"
        except (ValueError, TypeError, KeyError, OverflowError):
            pass
    return status


def next_timeout(day: str) -> float | None:
    if not day:
        day = datetime.now().strftime("%Y-%m-%d")
    deadlines = []
    for record in _read_records(day):
        if record.get("status") == "pending":
            try:
                deadlines.append(datetime.fromisoformat(record["time"]).timestamp() + 600)
            except (ValueError, TypeError, KeyError, OverflowError):
                pass
    future = [deadline - time.time() for deadline in deadlines if deadline > time.time()]
    return max(0.01, min(future)) if future else None


def output_dir() -> str:
    """抓包文件放在 plugin_data\\api_trace 下面，按天分文件。"""
    path = os.path.join(get_astrbot_plugin_data_path(), "api_trace")
    os.makedirs(path, exist_ok=True)
    return path


def _content_text(value: Any) -> str:
    """把消息正文转成可读文字。

    正文可能是一整段字符串，也可能是"一串碎片"（文字 + 图片 + 语音等）。
    碎片直接转字符串会变成一堆字典字面量，所以逐块读出来。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for part in value:
            if part is None:
                continue
            if isinstance(part, str):
                pieces.append(part)
                continue
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    pieces.append(part["text"])
                    continue
                kind = str(part.get("type") or "").lower()
                if kind == "image_url" or part.get("image_url"):
                    pieces.append("〔图片〕")
                    continue
                if kind in ("input_audio", "audio") or part.get("audio_url"):
                    pieces.append("〔语音〕")
                    continue
                if kind == "file" or part.get("file"):
                    pieces.append("〔文件〕")
                    continue
                try:
                    pieces.append(json.dumps(part, ensure_ascii=False))
                except (TypeError, ValueError):
                    pieces.append(str(part))
                continue
            pieces.append(str(part))
        return "\n".join(piece for piece in pieces if piece)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _clip(value: Any) -> str:
    body = "" if value is None else str(value)
    if SETTINGS.max_chars > 0 and len(body) > SETTINGS.max_chars:
        return body[: SETTINGS.max_chars] + "\n……（已截断，原文 %d 字）" % len(body)
    return body


def _raw_json_text(value: Any) -> str:
    """保存工具参数的 JSON 原文；长度限制只在页面展示时处理。"""
    try:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.debug("[抓包] 保存工具参数原文失败：%s", exc)
        try:
            return str(value)
        except Exception:
            return ""


def _tool_names(tools: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(tools, list):
        return names
    for item in tools:
        try:
            function = (item or {}).get("function") or {}
            name = function.get("name") or (item or {}).get("name")
            if name:
                names.append(str(name))
        except Exception:
            continue
    return names


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _normalize_response_tool_call(call: Any, order: int) -> dict[str, Any]:
    """把响应里的单个工具调用存成独立、稳定、可展示的字段。"""
    try:
        function = _field(call, "function") or _field(call, "function_call") or call
        return {
            "id": str(_field(call, "id") or ""),
            "name": str(_field(function, "name") or ""),
            "arguments": _raw_json_text(_field(function, "arguments")),
            "order": order,
        }
    except Exception as exc:
        logger.debug("[抓包] 规范化响应工具调用失败：%s", exc)
        return {"id": "", "name": "", "arguments": "", "order": order}


def _context_tool_result_count(record: dict[str, Any]) -> int:
    """统计请求上下文里的工具结果，不读取模型本发的响应工具调用。"""
    try:
        results = record.get("tool_results")
        if isinstance(results, list):
            return len(results)
        messages = record.get("messages") or []
        return sum(
            1
            for message in messages
            if isinstance(message, dict) and message.get("role") == "tool"
        )
    except Exception as exc:
        logger.debug("[抓包] 统计上下文工具结果失败：%s", exc)
        return 0


# ---------------------------------------------------------------- 用量解析
#
# 各家回报用量的写法不一样，这里只做一件事：从"可能带着用量的任意东西"里
# 抠出输入、输出、缓存命中三样，取不到的就不写进结果（界面显示破折号），
# 全程不抛异常——抓包是旁路，不能因为读不懂用量把正常对话搞挂。


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """把模型对象 / 字典 / 普通对象统一成字典；弄不了就返回 None。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    for name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, name, None)
        if callable(method):
            try:
                data = method()
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    try:
        data = vars(value)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _snapshot_default(value: Any) -> Any:
    mapping = _as_mapping(value)
    return mapping if mapping is not None else str(value)


def _snapshot_messages(messages: Any) -> Any:
    """Freeze the request before the intercepted call can mutate it."""
    return json.loads(json.dumps(messages, ensure_ascii=False, default=_snapshot_default))


def _pick_int(mapping: dict[str, Any], *names: str) -> int | None:
    """按顺序找第一个能转成整数的字段。"""
    for name in names:
        if name not in mapping:
            continue
        try:
            return int(mapping[name])
        except (TypeError, ValueError):
            continue
    return None


def _raw_usage(obj: Any) -> dict[str, Any] | None:
    """从返回体或流的一帧里找出"用量"那一段。"""
    if obj is None:
        return None
    mapping = _as_mapping(obj)
    if mapping is not None:
        if mapping.get("usage") is not None:
            inner = _as_mapping(mapping.get("usage"))
            if inner is not None:
                return inner
        # 有的实现直接把用量摊在顶层
        if "prompt_tokens" in mapping or "input_tokens" in mapping:
            return mapping
        return None
    inner = _as_mapping(getattr(obj, "usage", None))
    if inner is not None:
        return inner
    if getattr(obj, "prompt_tokens", None) is not None:
        return _as_mapping(obj)
    return None


def extract_usage(obj: Any) -> dict[str, Any]:
    """把用量整理成统一的键；一个都取不到就返回空字典。

    缓存命中优先看 DeepSeek 口径的 `prompt_cache_hit_tokens`，
    没有再退到 OpenAI 口径的 `prompt_tokens_details.cached_tokens`。
    """
    try:
        raw = _raw_usage(obj)
    except Exception:
        return {}
    if not raw:
        return {}

    usage: dict[str, Any] = {}
    prompt = _pick_int(raw, "prompt_tokens", "input_tokens")
    completion = _pick_int(raw, "completion_tokens", "output_tokens")
    total = _pick_int(raw, "total_tokens")
    hit = _pick_int(raw, "prompt_cache_hit_tokens")
    miss = _pick_int(raw, "prompt_cache_miss_tokens")

    cached = hit
    if cached is None:
        details = _as_mapping(raw.get("prompt_tokens_details"))
        if details is not None:
            cached = _pick_int(details, "cached_tokens")
    if cached is None:
        cached = _pick_int(raw, "cache_read_input_tokens")

    if prompt is not None:
        usage["prompt_tokens"] = prompt
    if completion is not None:
        usage["completion_tokens"] = completion
    if total is not None:
        usage["total_tokens"] = total
    elif prompt is not None and completion is not None:
        usage["total_tokens"] = prompt + completion
    if hit is not None:
        usage["prompt_cache_hit_tokens"] = hit
    if miss is not None:
        usage["prompt_cache_miss_tokens"] = miss
    if cached is not None:
        usage["cached_tokens"] = cached
        if prompt:
            usage["hit_rate"] = round(cached / prompt, 6)
    return usage


def _usage_text(usage: dict[str, Any] | None) -> str:
    """控制台那一行用的极短用量说明。"""
    if not usage:
        return ""
    bits: list[str] = []
    if usage.get("prompt_tokens") is not None:
        bits.append("输入%d" % usage["prompt_tokens"])
    if usage.get("completion_tokens") is not None:
        bits.append("输出%d" % usage["completion_tokens"])
    rate = usage.get("hit_rate")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        bits.append("命中%.0f%%" % (float(rate) * 100))
    return (" 用量=" + "/".join(bits)) if bits else ""


def _usage_brief(usage: Any) -> dict[str, Any]:
    """从记录里的用量取列表要的那几个数；取不到的键干脆不放。"""
    if not isinstance(usage, dict):
        return {}
    brief: dict[str, Any] = {}
    prompt = _pick_int(usage, "prompt_tokens")
    completion = _pick_int(usage, "completion_tokens")
    cached = _pick_int(usage, "cached_tokens")
    if cached is None:
        cached = _pick_int(usage, "prompt_cache_hit_tokens")
    if prompt is not None:
        brief["prompt_tokens"] = prompt
    if completion is not None:
        brief["completion_tokens"] = completion
    if cached is not None:
        brief["cached_tokens"] = cached

    rate = usage.get("hit_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        rate = None
    if rate is None and prompt and cached is not None:
        rate = cached / prompt
    if rate is not None:
        brief["hit_rate"] = float(rate)
    return brief


def _usage_line(usage: Any) -> str:
    """.txt 里那一行中文用量明细。"""
    brief = _usage_brief(usage)
    if not brief:
        return ""
    prompt = brief.get("prompt_tokens")
    cached = brief.get("cached_tokens")
    completion = brief.get("completion_tokens")
    bits: list[str] = []
    if prompt is not None:
        bits.append("输入 %d" % prompt)
    if cached is not None:
        bits.append("缓存读取 %d" % cached)
    if prompt is not None and cached is not None:
        bits.append("未缓存 %d" % max(0, prompt - cached))
    if completion is not None:
        bits.append("输出 %d" % completion)
    if prompt is not None and completion is not None:
        bits.append("合计 %d" % (prompt + completion))
    if "hit_rate" in brief:
        bits.append("命中 %.1f%%" % (brief["hit_rate"] * 100))
    return " / ".join(bits)


def _write_dump(record: dict[str, Any]) -> None:
    with _LOCK:
        _write_dump_locked(record)


def _write_dump_locked(record: dict[str, Any]) -> None:
    """一次请求写两份：机器读的 JSON 行、人读的中文排版。"""
    directory = output_dir()
    day = record["time"][:10]
    json_path = os.path.join(directory, "%s.jsonl" % day)
    text_path = os.path.join(directory, "%s.txt" % day)

    try:
        with open(json_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[抓包] 写 JSON 失败：%s", exc)
        return

    _notify_changes()
    if record.get("status") == "pending":
        try:
            _ENTRY_COUNTS[day] = len(_read_records(day))
            _trim_day(day)
            prune_old_days()
        except Exception as exc:
            logger.debug("[抓包] 清理失败：%s", exc)
        return

    messages = record.get("messages") or []
    lines = [
        "=" * 72,
        "时间：%s" % record["time"],
        "请求的模型：%s" % (record.get("model") or "（未指定）"),
        "带的工具：%d 个 %s" % (len(record.get("tools") or []), record.get("tools") or ""),
        "消息条数：%d" % len(messages),
        "流式：%s" % ("是" if record.get("stream") else "否"),
        "用量：%s" % (_usage_line(record.get("usage")) or "（接口没回报）"),
        "",
        "---- 系统提示 ----",
        _content_text(record.get("system_prompt")) or "（没有单独的 system 字段）",
        "",
    ]
    try:
        result_count = _context_tool_result_count(record)
        if result_count:
            lines.insert(5, "上下文里带 %d 条工具结果" % result_count)
    except Exception as exc:
        logger.debug("[抓包] 整理上下文工具结果提示失败：%s", exc)

    for index, message in enumerate(messages, start=1):
        if isinstance(message, dict):
            role = message.get("role", "?")
            content = message.get("content")
            note = " ".join(str(message.get(key) or "") for key in ("name", "tool_call_id")).strip()
        else:
            role, content, note = "?", message, ""
        lines.append("---- 第 %d 条 / 角色：%s %s ----" % (index, role, note))
        body = _content_text(content)
        if body:
            lines.append(body)
        for order, call in enumerate(message.get("tool_calls") or [] if isinstance(message, dict) else []):
            item = _normalize_response_tool_call(call, order)
            lines.append("工具：%s（id=%s）" % (item["name"] or "（未知）", item["id"]))
            lines.append("参数原文：%s" % item["arguments"])
        if not body and not (isinstance(message, dict) and message.get("tool_calls")):
            lines.append("（空）")
        lines.append("")

    lines.append("---- 模型回答 ----")
    lines.append("回报的模型：%s" % (record.get("response_model") or "（未知）"))
    try:
        response_text = _content_text(record.get("response_text"))
        response_calls = record.get("response_tool_calls")
        if not isinstance(response_calls, list):
            response_calls = record.get("tool_calls") or []
        if response_text:
            lines.append(response_text)
        elif response_calls:
            lines.append("本发没有文本，只输出了工具调用")
        else:
            lines.append("（空）")
        for index, call in enumerate(response_calls):
            if isinstance(call, str):
                lines.append("工具调用 #%d：%s" % (index + 1, call))
                continue
            name = str(call.get("name") or "（未知）")
            call_id = str(call.get("id") or "（无）")
            arguments = _raw_json_text(call.get("arguments"))
            lines.append("工具：%s（id=%s）" % (name, call_id))
            lines.append("参数原文：%s" % (arguments or "（无）"))
    except Exception as exc:
        logger.warning("[抓包] 整理模型回复文本失败：%s", exc)
        lines.append("（模型回复整理失败）")
    lines.append("")

    try:
        with open(text_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        logger.warning("[抓包] 写文本失败：%s", exc)

    # 每次写完顺手做两件清理：当天条数超了裁掉最旧的，天数超了删掉最旧的那天
    try:
        _ENTRY_COUNTS[day] = len(_read_records(day))
        _trim_day(day)
    except Exception as exc:
        logger.debug("[抓包] 裁剪当天记录失败：%s", exc)
    try:
        prune_old_days()
    except Exception as exc:
        logger.debug("[抓包] 清理旧文件失败：%s", exc)


def _count_lines(path: str) -> int:
    """数一个文件里有多少条非空行。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


MAX_DAY_MB = 32


def _trim_day(day: str) -> None:
    """当天记录超过上限时，只保留最新的若干条。"""
    keep = int(SETTINGS.keep_entries or 0)
    if keep <= 0:
        return

    directory = output_dir()
    json_path = os.path.join(directory, "%s.jsonl" % day)
    text_path = os.path.join(directory, "%s.txt" % day)
    if not os.path.isfile(json_path):
        return

    count = _ENTRY_COUNTS.get(day, 0)
    try:
        size_mb = os.path.getsize(json_path) / 1048576.0
    except OSError:
        size_mb = 0.0
    # 条数没超但体积超了也要裁，防止某一天悄悄堆大
    if count <= keep and size_mb <= MAX_DAY_MB:
        return

    try:
        records = _read_records(day)
        retained_ids = {id(record) for record in records[-keep:]}
        retained = [record for record in records
                    if id(record) in retained_ids or record.get("status") == "pending"]
        with open(json_path, "w", encoding="utf-8") as fh:
            for record in retained:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _ENTRY_COUNTS[day] = len(retained)
    except OSError as exc:
        logger.warning("[抓包] 裁剪 JSON 失败：%s", exc)
        return

    # 可读文本也一起裁：每条记录以一条 72 个等号的分隔线开头
    try:
        if os.path.isfile(text_path):
            with open(text_path, encoding="utf-8") as fh:
                raw = fh.read()
            blocks = raw.split("=" * 72)
            header = blocks[0]
            kept = blocks[1:][-keep:]
            rebuilt = header + "".join("=" * 72 + block for block in kept)
            with open(text_path, "w", encoding="utf-8") as fh:
                fh.write(rebuilt)
    except OSError as exc:
        logger.debug("[抓包] 裁剪文本失败：%s", exc)

    logger.info("[抓包] %s 的记录超过 %d 条，只保留最新的 %d 条", day, keep, keep)


def _extract_response(result: Any) -> tuple[Any, str, list[dict[str, Any]]]:
    """从接口库的返回里取出文本、模型名和工具调用。"""
    text, model, calls = "", "", []
    try:
        model = str(_field(result, "model") or "")
        choices = _field(result, "choices") or []
        if choices:
            message = _field(choices[0], "message")
            if message is not None:
                text = _field(message, "content")
                for order, call in enumerate(_field(message, "tool_calls") or []):
                    try:
                        calls.append(_normalize_response_tool_call(call, order))
                    except Exception as exc:
                        logger.debug("[抓包] 读取响应工具调用失败：%s", exc)
    except Exception as exc:
        logger.debug("[抓包] 读取响应失败：%s", exc)
    return text, model, calls


def _finish_record(
    snapshot: dict[str, Any],
    text: Any,
    model: str,
    calls: list[dict[str, Any]],
    usage: dict[str, Any] | None,
) -> None:
    """把回答和用量补齐，然后落盘 + 打一行控制台。"""
    try:
        normalized_calls = [
            _normalize_response_tool_call(call, index)
            for index, call in enumerate(calls or [])
        ]
        snapshot.update({
            "status": "complete",
            "duration_ms": max(0, round((time.time() - datetime.fromisoformat(snapshot["time"]).timestamp()) * 1000)),
            "response_model": model,
            "response_text": text,
            "response_tool_calls": normalized_calls,
            # 旧记录/旧页面使用这个键；它仍然只保存模型本发的调用。
            "tool_calls": normalized_calls,
            "response_raw": {
                "content": text,
                "tool_calls": normalized_calls,
            },
        })
        if usage:
            snapshot["usage"] = usage
        _write_dump(snapshot)
        if SETTINGS.console:
            logger.info(
                "[抓包] 已记录：工具%d个 / 消息%d条 / 提示%d字 → 回报=%s 回答%d字%s",
                len(snapshot.get("tools") or []),
                len(snapshot.get("messages") or []),
                len(str(snapshot.get("system_prompt") or "")),
                model or "未知",
                len(_content_text(text)),
                _usage_text(usage),
            )
    except Exception as exc:
        logger.warning("[抓包] 整理模型响应失败：%s", exc)


def _fail_record(snapshot: dict[str, Any], error: BaseException) -> None:
    try:
        snapshot.update({
            "status": "failed",
            "error": "%s: %s" % (type(error).__name__, str(error)),
            "duration_ms": max(0, round((time.time() - datetime.fromisoformat(snapshot["time"]).timestamp()) * 1000)),
        })
        _write_dump(snapshot)
    except Exception as exc:
        logger.warning("[抓包] 记录失败状态出错：%s", exc)


class _UsageStream:
    """给流套一层"旁听"的壳：原样转给调用方，顺手记下末帧的用量。

    为什么要套：开了流以后 `create` 交回来的是个异步流，真正的用量在最后一帧，
    而且只有请求方要了用量才有。不套壳就只能二选一——要么把流读掉（调用方就没
    东西可读了），要么永远拿不到用量。

    套壳的风险全部按"能不加就不加"处理：
    - 只接 `__aiter__` / `__anext__` / 进出上下文 / `close`，其余属性一律原样转交；
    - 中途抛错、提前关流、流读完了，都会把这条记录落盘，不会丢；
    - 包不上就把原对象原样还回去（调用方自己看 `_attach_stream_usage`）。
    """

    def __init__(self, stream: Any, snapshot: dict[str, Any]) -> None:
        self._stream = stream
        self._snapshot = snapshot
        self._usage: dict[str, Any] = {}
        self._calls: dict[int, dict[str, Any]] = {}
        self._content: str | None = None
        self._model = ""
        self._done = False

    def __aiter__(self) -> "_UsageStream":
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._stream.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            # 取消、超时、连接断都算：先把这条记录落下来，再把异常原样抛出去
            self._done = True
            _fail_record(self._snapshot, exc)
            raise
        self._absorb(chunk)
        return chunk

    def _absorb(self, chunk: Any) -> None:
        """每一帧都看一眼；通常只有最后一帧带用量，所以后到的覆盖先到的。"""
        try:
            usage = extract_usage(chunk)
        except Exception:
            usage = None
        if usage:
            self._usage = usage
        try:
            self._model = str(_field(chunk, "model") or self._model)
            choices = _field(chunk, "choices") or []
            if not choices:
                return
            delta = _field(choices[0], "delta")
            content = _field(delta, "content")
            if content is not None:
                self._content = (self._content or "") + _content_text(content)
            for fragment in _field(delta, "tool_calls") or []:
                index = int(_field(fragment, "index", 0))
                item = self._calls.setdefault(index, {
                    "id": "", "type": "", "function": {"name": "", "arguments": ""},
                })
                function = _field(fragment, "function") or {}
                for target, key, value in (
                    (item, "id", _field(fragment, "id")),
                    (item, "type", _field(fragment, "type")),
                    (item["function"], "name", _field(function, "name")),
                    (item["function"], "arguments", _field(function, "arguments")),
                ):
                    if value is not None:
                        target[key] = str(target.get(key) or "") + str(value)
        except Exception as exc:
            logger.debug("[抓包] 读取流式工具调用失败：%s", exc)

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            calls = [self._calls[index] for index in sorted(self._calls)]
            _finish_record(self._snapshot, self._content, self._model, calls, self._usage)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[抓包] 落盘失败：%s", exc)

    def close(self) -> None:
        closer = getattr(self._stream, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        self._finish()

    async def aclose(self) -> None:
        closer = getattr(self._stream, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except Exception:
                pass
        else:
            sync = getattr(self._stream, "close", None)
            if callable(sync):
                try:
                    sync()
                except Exception:
                    pass
        self._finish()

    async def __aenter__(self) -> "_UsageStream":
        enter = getattr(self._stream, "__aenter__", None)
        if callable(enter):
            await enter()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        leaver = getattr(self._stream, "__aexit__", None)
        try:
            if callable(leaver):
                return await leaver(*exc_info)
            return False
        finally:
            if exc_info and exc_info[1] is not None and not self._done:
                self._done = True
                _fail_record(self._snapshot, exc_info[1])
            else:
                self._finish()

    def __getattr__(self, name: str) -> Any:
        # 内部字段不走转交，免得初始化没完就被问到、转成死循环
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)

    def __del__(self) -> None:
        # 调用方半路把流扔了也要留下记录；解释器收尾时写盘失败就算了
        try:
            self._finish()
        except Exception:
            pass


def _attach_stream_usage(result: Any, snapshot: dict[str, Any]) -> Any:
    """流式：套壳旁听；万一不是能迭代的流，就当场落盘（不含量），一条都不丢。"""
    try:
        if hasattr(result, "__anext__"):
            return _UsageStream(result, snapshot)
    except Exception:
        pass
    text, model, calls = _extract_response(result)
    _finish_record(snapshot, text, model, calls, extract_usage(result))
    return result


def prune_old_days() -> list[str]:
    """只保留最近若干天的抓包文件，删掉更早的。返回被删掉的日期。"""
    keep = max(1, int(SETTINGS.keep_days or 30))
    directory = output_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []

    days = sorted({name[:-6] for name in names if name.endswith(".jsonl")})
    if len(days) <= keep:
        return []

    removed: list[str] = []
    for day in days[:-keep]:
        if clear_day(day):
            removed.append(day)
    if removed:
        logger.info("[抓包] 超出保留天数，已删掉 %d 天的旧记录：%s", len(removed), ", ".join(removed))
    return removed


def list_days() -> list[dict[str, Any]]:
    """列出已有的抓包文件（按天）。"""
    directory = output_dir()
    days: list[dict[str, Any]] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return days
    for name in sorted(names, reverse=True):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(directory, name)
        try:
            size = os.path.getsize(path)
            count = len(_read_records(name[:-6]))
        except OSError:
            continue
        days.append({"date": name[:-6], "size": size, "count": count})
    return days


def _read_records(day: str) -> list[dict[str, Any]]:
    """读取某天的全部记录（原始字典）。"""
    with _LOCK:
        return _read_records_locked(day)


def _read_records_locked(day: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return []
    path = os.path.join(output_dir(), "%s.jsonl" % day)
    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    if not os.path.isfile(path):
        return records
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    record_id = record.get("id")
                    if record_id and record_id in by_id:
                        by_id[record_id].update(record)
                    else:
                        records.append(record)
                        if record_id:
                            by_id[record_id] = record
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("[抓包] 读取失败：%s", exc)
    return records


def _search_text(value: Any) -> str:
    """把待检索的内容压成可比较的形态：连续空白压成一个空格、大小写统一。

    这样跨行的文字也能命中：请求里写「foo」再换行写「bar」，检索词写「foo bar」
    也算同一处；检索词里的空白同样归一，两边口径一致。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    return re.sub(r"\s+", " ", value).lower()


def _search_count(record: dict[str, Any], query: str) -> int:
    """统计展示字段中的非重叠匹配，避免重复计算冗余快照。"""
    try:
        needle = _search_text(query).strip()
        if not needle:
            return 0
        fields = [record.get("model"), record.get("response_model")]
        messages = record.get("messages") or []
        system = _content_text(record.get("system_prompt"))
        message_systems = [_content_text(m.get("content")) for m in messages
                           if isinstance(m, dict) and m.get("role") == "system"]
        if system and system not in message_systems:
            fields.append(system)
        for message in messages:
            if not isinstance(message, dict):
                fields.append(_content_text(message))
                continue
            fields.append(_content_text(message.get("content")))
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    function = call.get("function") or call.get("function_call") or call
                    if isinstance(function, dict):
                        fields.append(function.get("arguments"))
        raw = record.get("response_raw")
        raw = raw if isinstance(raw, dict) else {}
        fields.append(record.get("response_text") if record.get("response_text") is not None else raw.get("content"))
        calls = record.get("response_tool_calls")
        if calls is None:
            calls = raw.get("tool_calls", record.get("tool_calls") or [])
        for call in calls or []:
            if isinstance(call, dict):
                function = call.get("function") or call.get("function_call") or call
                if isinstance(function, dict):
                    fields.append(function.get("arguments"))
        return sum(_search_text(value).count(needle) for value in fields if value is not None)
    except Exception as exc:
        logger.warning("[抓包] 检索记录失败：%s", exc)
        return 0


def open_output_dir() -> dict[str, Any]:
    """打开抓包目录；无法打开时返回可复制的绝对路径。"""
    path = os.path.abspath(os.path.join("data", "plugin_data", "api_trace"))
    try:
        path = os.path.abspath(os.path.join(get_astrbot_plugin_data_path(), "api_trace"))
        if os.name != "nt":
            return {"opened": False, "path": path}
        os.makedirs(path, exist_ok=True)
        subprocess.Popen(["explorer.exe", path])
        return {"opened": True, "path": path}
    except Exception as exc:
        logger.warning("[抓包] 打开目录失败：%s", exc)
        return {"opened": False, "path": path}


def list_entries(day: str, query: str = "") -> list[dict[str, Any]]:
    """某天每条请求的摘要（不含完整正文，界面列表用）。

    最新的排在最前面；`index` 仍然是文件里的原始序号，
    界面拿它去取单条完整记录。

    用量只挑列表用得着的四个数，取不到的键不放——老记录（没有 `usage`）
    照样能读，界面上显示破折号。
    """
    records = _read_records(day)
    summaries: list[dict[str, Any]] = []
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if query:
            count = _search_count(record, query)
            if not count:
                continue
        messages = record.get("messages") or []
        summary = {
            "index": index,
            "id": record.get("id") or "legacy:%d" % index,
            "status": _effective_status(record),
            "revision": hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16],
            "time": record.get("time", ""),
            "model": record.get("model") or "",
            "response_model": record.get("response_model") or "",
            "tools": record.get("tools") or [],
            "message_count": len(messages),
            "has_tool_message": any(
                isinstance(m, dict) and m.get("role") == "tool" for m in messages
            ),
            "reply_length": len(str(record.get("response_text") or "")),
        }
        summary.update(_usage_brief(record.get("usage")))
        if query:
            summary["match_count"] = count
        summaries.append(summary)
        if query and len(summaries) > 200:
            break
    return summaries


def summarize(day: str) -> dict[str, Any]:
    """当天的用量汇总：条数、总输入、总输出、总缓存读取、整体命中率。

    一条带用量的都没有时，只给条数——给 0 会让人误以为"真的没花"。
    """
    records = _read_records(day)
    count = len(records)
    prompt_total = 0
    completion_total = 0
    cached_total = 0
    used = 0
    has_cached = False

    for record in records:
        brief = _usage_brief(record.get("usage"))
        if not brief:
            continue
        used += 1
        prompt_total += int(brief.get("prompt_tokens") or 0)
        completion_total += int(brief.get("completion_tokens") or 0)
        if "cached_tokens" in brief:
            cached_total += int(brief["cached_tokens"])
            has_cached = True

    if not used:
        return {"date": day, "count": count, "with_usage": 0}

    summary: dict[str, Any] = {
        "date": day,
        "count": count,
        "with_usage": used,
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "total_tokens": prompt_total + completion_total,
    }
    if has_cached:
        summary["cached_tokens"] = cached_total
        if prompt_total > 0:
            summary["hit_rate"] = round(cached_total / prompt_total, 6)
    return summary


def get_entry(day: str, index: int, record_id: str = "") -> dict[str, Any] | None:
    """取某一天的某一条完整记录。"""
    records = _read_records(day)
    if record_id and not record_id.startswith("legacy:"):
        record = next((item for item in records if item.get("id") == record_id), None)
        if record is None:
            return None
    elif index < 0 or index >= len(records):
        return None
    else:
        record = records[index]
    return {**record, "status": _effective_status(record)}


def clear_day(day: str) -> bool:
    """删掉某天的抓包文件（jsonl + txt）。"""
    with _LOCK:
        return _clear_day_locked(day)


def _clear_day_locked(day: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return False
    directory = output_dir()
    removed = False
    for suffix in (".jsonl", ".txt"):
        path = os.path.join(directory, "%s%s" % (day, suffix))
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed = True
        except OSError as exc:
            logger.warning("[抓包] 删除失败：%s", exc)
    if removed:
        _ENTRY_COUNTS.pop(day, None)
        _notify_changes()
    return removed


def uninstall() -> bool:
    """把发请求那个方法还回去（开关关掉时用）。

    为什么要有：开关关掉只代表「这次不装」，可上次装上去的壳还挂在接口库的类上，
    而且那个壳的闭包里抓的是**上一次加载时**的设置——旧壳里的开关还是开的，
    于是关掉开关照样抓包。只有真的把方法换回原件才叫关掉。
    """
    try:
        from openai.resources.chat.completions.completions import AsyncCompletions
    except Exception as exc:  # noqa: BLE001
        logger.debug("[抓包] 找不到接口库，没什么可摘的：%s", exc)
        return True

    current = AsyncCompletions.__dict__.get("create")
    if current is None:
        return True
    if not getattr(current, _PATCH_FLAG, False):
        return True
    original = getattr(current, "__wrapped__", None)
    if original is None:
        logger.warning("[抓包] 找不到被包住的原件，挂钩摘不掉，只能重启")
        return False
    try:
        AsyncCompletions.create = original
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[抓包] 摘下挂钩失败：%s", exc)
        return False


def _requester() -> dict[str, Any]:
    """看这一发 API 请求是谁发起的：某个插件，还是主对话本身。

    顺着调用栈往上翻，第一个落在**别的插件**目录里的帧，就是发起方。

    必须跳过自己：抓包这层包装器本身就住在插件目录里，不跳过的话每次都会
    认成"本插件发的"——把真正的发起方整个盖住。

    翻不到别的插件，就是主对话（或者框架内部别的调用）。
    """
    marker = os.sep + "data" + os.sep + "plugins" + os.sep
    try:
        frame = sys._getframe(1)
    except ValueError:
        return {"kind": "未知"}

    depth = 0
    while frame is not None and depth < 60:
        filename = frame.f_code.co_filename or ""
        pos = filename.find(marker)
        if pos >= 0:
            rest = filename[pos + len(marker) :]
            plugin = rest.split(os.sep)[0] or ""
            # 自己的包装器、以及本插件的其他文件，一律跳过
            if plugin and plugin != _SELF_PLUGIN:
                return {
                    "kind": "插件",
                    "plugin": plugin,
                    "file": os.path.basename(filename),
                    "line": frame.f_lineno,
                }
        frame = frame.f_back
        depth += 1
    return {"kind": "主对话"}


def install(
    enabled: bool,
    max_chars: int = 0,
    console: bool = True,
    keep_days: int = 30,
    keep_entries: int = 30,
    check: bool = True,
) -> bool:
    """按设置决定是否安装抓包挂钩；返回是否装上了。"""
    SETTINGS.enable = bool(enabled)
    SETTINGS.check = bool(check)
    SETTINGS.max_chars = int(max_chars or 0)
    SETTINGS.console = bool(console)
    SETTINGS.keep_days = max(1, int(keep_days or 30))
    SETTINGS.keep_entries = max(0, int(keep_entries or 0))
    if not SETTINGS.enable:
        undone = uninstall()
        logger.info(
            "[抓包] 开关是关的：%s",
            "已把挂钩摘掉" if undone else "挂钩没摘干净，要重启本体",
        )
        return False

    # 装了就先把已有的记录按新规矩收拾一遍，免得老文件一直超标
    try:
        for info in list_days():
            _ENTRY_COUNTS[info["date"]] = info["count"]
            _trim_day(info["date"])
    except Exception as exc:
        logger.debug("[抓包] 启动时裁剪失败：%s", exc)
    prune_old_days()

    try:
        from openai.resources.chat.completions.completions import AsyncCompletions
    except Exception as exc:
        logger.warning("[抓包] 找不到接口库，抓包不可用：%s", exc)
        return False

    original = AsyncCompletions.__dict__.get("create")
    if original is None:
        return False
    if getattr(original, _PATCH_FLAG, False):
        original = getattr(original, "__wrapped__", original)

    async def create(self, *args, **kwargs):
        snapshot: dict[str, Any] = {}
        if SETTINGS.enable:
            try:
                request_raw = _snapshot_messages(kwargs)
                messages = request_raw.get("messages") or []
                system_prompt = ""
                for message in messages:
                    if isinstance(message, dict) and message.get("role") == "system":
                        system_prompt = _content_text(message.get("content"))
                        break
                snapshot = {
                    "id": uuid.uuid4().hex,
                    "status": "pending",
                    "time": datetime.now().isoformat(sep=" ", timespec="milliseconds"),
                    "model": request_raw.get("model"),
                    "stream": bool(kwargs.get("stream")),
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "request_raw": request_raw,
                    "tools": _tool_names(request_raw.get("tools")),
                    "tool_choice": request_raw.get("tool_choice"),
                }
                try:
                    snapshot["requester"] = _requester()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[抓包] 判断请求来源失败：%s", exc)
                # 顺手体检：结构上的毛病在这一刻最清楚
                if SETTINGS.check:
                    try:
                        from . import request_check

                        health = request_check.check(system_prompt, messages)
                        snapshot["health"] = health
                        if health.get("level") != "ok":
                            logger.warning(
                                "[请求体检] %s", request_check.summary_text(health)
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[请求体检] 跳过：%s", exc)
                # 谁往这次请求里加了什么（只追插件层）
                try:
                    from . import prompt_source

                    src = prompt_source.take()
                    if src:
                        snapshot["sources"] = src
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[抓包] 取来源记录失败：%s", exc)
                _write_dump(snapshot)
            except Exception as exc:
                logger.debug("[抓包] 读取请求失败：%s", exc)

        try:
            result = await original(self, *args, **kwargs)
        except BaseException as exc:
            if snapshot:
                _fail_record(snapshot, exc)
            raise

        if snapshot:
            try:
                if snapshot.get("stream"):
                    # 流式：用量在最后一帧，得等调用方读完才知道，交给壳去收尾
                    result = _attach_stream_usage(result, snapshot)
                else:
                    text, model, calls = _extract_response(result)
                    _finish_record(snapshot, text, model, calls, extract_usage(result))
            except Exception as exc:
                logger.warning("[抓包] 落盘失败：%s", exc)

        return result

    setattr(create, _PATCH_FLAG, True)
    create.__wrapped__ = original

    try:
        AsyncCompletions.create = create
        return True
    except Exception as exc:
        logger.warning("[抓包] 安装失败：%s", exc)
        return False
