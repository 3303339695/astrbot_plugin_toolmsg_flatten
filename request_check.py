"""请求体检：发出去之前，先看看这发请求有没有毛病。

## 为什么要有这一块

抓包页能让你**看见**每次请求长什么样，但看见不等于看明白。一次请求动辄上百条
消息、好几万字，肉眼很难发现"人格被注入了两遍""有工具结果配不上号""单条消息
八千字"这种问题——它们不会报错，只会让模型变傻、让账单变贵。

这里在请求组装完毕、即将发出的那一刻过一遍，把可疑的地方挑出来。

## 检查的是结构，不是内容

全部是机械判断：重复、配对、长度、占比、顺序。不判断"这句话好不好"，
那要靠模型，就不是体检了。

## 只报告，不动手

发现问题只记下来，不改请求。要不要改、怎么改，看你自己判断。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 单条消息超过这么多字就提醒
LONG_MESSAGE_CHARS = 6000

# 工具结果占上下文的比例超过这个就提醒
TOOL_RATIO_WARN = 0.6

# 重复段落的最小长度：太短的重复没意义（"好的""嗯"到处都是）
DUP_MIN_CHARS = 60

# 判重时把空白压掉再比，避免换行差异漏判
_WS = re.compile(r"\s+")


def _text_of(value: Any) -> str:
    """把消息内容压成一段纯文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return " ".join(parts)
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def _fingerprint(text: str) -> str:
    """判重用的指纹：压掉所有空白。"""
    return _WS.sub("", text or "")


def _tool_call_ids(message: dict) -> list[str]:
    """取一条助手消息里所有工具调用的编号。"""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    out = []
    for call in calls:
        if isinstance(call, dict):
            out.append(str(call.get("id") or ""))
    return [c for c in out if c]


def check(system_prompt: str, messages: list[Any]) -> dict:
    """把一次请求过一遍，返回体检结果。

    Args:
        system_prompt: 已经抽出来的系统提示（可以为空，会从 messages 里找）。
        messages: 即将发出去的消息列表。

    Returns:
        {"level": "ok"|"warn"|"bad", "issues": [...], "stats": {...}}
    """
    issues: list[dict] = []
    rows = [m for m in (messages or []) if isinstance(m, dict)]

    if not system_prompt:
        for message in rows:
            if message.get("role") == "system":
                system_prompt = _text_of(message.get("content"))
                break

    total_chars = 0
    tool_chars = 0
    system_count = 0
    seen: dict[str, int] = {}
    declared_calls: dict[str, int] = {}
    answered_calls: dict[str, int] = {}

    for index, message in enumerate(rows):
        role = str(message.get("role") or "")
        text = _text_of(message.get("content"))
        size = len(text)
        total_chars += size

        if role == "system":
            system_count += 1
        if role == "tool":
            tool_chars += size
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                answered_calls[call_id] = index
            else:
                issues.append({
                    "kind": "工具结果没有编号",
                    "detail": "这条工具结果没带调用编号，接不回它对应哪一次调用",
                    "at": index,
                })
        if role == "assistant":
            for call_id in _tool_call_ids(message):
                declared_calls[call_id] = index
            if not text and not _tool_call_ids(message):
                issues.append({
                    "kind": "空消息",
                    "detail": "这条助手消息既没内容也没工具申请",
                    "at": index,
                })

        if size > LONG_MESSAGE_CHARS:
            issues.append({
                "kind": "单条超长",
                "detail": "这一条有 %d 字" % size,
                "at": index,
            })

        # 判重只比够长的正文，太短的不算
        if size >= DUP_MIN_CHARS:
            finger = _fingerprint(text)
            if finger in seen:
                issues.append({
                    "kind": "内容重复",
                    "detail": "跟第 %d 条一模一样（%d 字）" % (seen[finger], size),
                    "at": index,
                })
            else:
                seen[finger] = index

    # 配对检查：申请了但没结果，有结果但没申请
    for call_id, index in declared_calls.items():
        if call_id not in answered_calls:
            issues.append({
                "kind": "工具调用没有结果",
                "detail": "申请了调用 %s，但后面没有对应的返回" % call_id[:12],
                "at": index,
            })
    for call_id, index in answered_calls.items():
        if call_id not in declared_calls:
            issues.append({
                "kind": "工具结果来路不明",
                "detail": "编号 %s 的结果，前面找不到对应的调用申请" % call_id[:12],
                "at": index,
            })

    if system_count > 1:
        issues.append({
            "kind": "多条系统提示",
            "detail": "这次请求里有 %d 条系统提示，多数上游只认第一条" % system_count,
            "at": -1,
        })

    ratio = (tool_chars / total_chars) if total_chars else 0.0
    if ratio >= TOOL_RATIO_WARN:
        issues.append({
            "kind": "工具结果占比过高",
            "detail": "工具结果占了全部内容的 %.0f%%（%d / %d 字）"
                      % (ratio * 100, tool_chars, total_chars),
            "at": -1,
        })

    # 定级
    serious = {"工具结果没有编号", "工具调用没有结果", "工具结果来路不明", "空消息"}
    level = "ok"
    if issues:
        level = "warn"
    if any(item["kind"] in serious for item in issues):
        level = "bad"

    return {
        "level": level,
        "issues": issues[:40],
        "stats": {
            "messages": len(rows),
            "chars": total_chars,
            "tool_chars": tool_chars,
            "tool_ratio": round(ratio, 3),
            "system_count": system_count,
            "tool_calls": len(declared_calls),
            "tool_results": len(answered_calls),
        },
    }


def summary_text(result: dict) -> str:
    """压成一句话，给日志用。"""
    level = result.get("level")
    issues = result.get("issues") or []
    stats = result.get("stats") or {}
    label = {"ok": "正常", "warn": "有疑点", "bad": "有问题"}.get(level, level)
    bits = ["%s（%d 条消息、%d 字、工具占 %.0f%%）"
            % (label, stats.get("messages", 0), stats.get("chars", 0),
               (stats.get("tool_ratio") or 0) * 100)]
    for item in issues[:3]:
        bits.append("· " + item.get("kind", ""))
    if len(issues) > 3:
        bits.append("…另有 %d 项" % (len(issues) - 3))
    return "；".join(bits)


__all__ = ["check", "summary_text", "LONG_MESSAGE_CHARS", "TOOL_RATIO_WARN"]
