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
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

_PATCH_FLAG = "__toolmsg_flatten_trace_patched__"


class _TraceSettings:
    """抓包的运行期设置。"""

    enable: bool = False
    max_chars: int = 0
    console: bool = True
    keep_days: int = 30
    # 每天最多保留多少条（0 表示不限）
    keep_entries: int = 30


SETTINGS = _TraceSettings()

# 每天已有多少条，避免每次都去数一遍大文件
_ENTRY_COUNTS: dict[str, int] = {}


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


def _write_dump(record: dict[str, Any]) -> None:
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

    messages = record.get("messages") or []
    lines = [
        "=" * 72,
        "时间：%s" % record["time"],
        "请求的模型：%s" % (record.get("model") or "（未指定）"),
        "带的工具：%d 个 %s" % (len(record.get("tools") or []), record.get("tools") or ""),
        "消息条数：%d" % len(messages),
        "流式：%s" % ("是" if record.get("stream") else "否"),
        "",
        "---- 系统提示 ----",
        _clip(_content_text(record.get("system_prompt"))) or "（没有单独的 system 字段）",
        "",
    ]
    for index, message in enumerate(messages, start=1):
        if isinstance(message, dict):
            role = message.get("role", "?")
            content = message.get("content")
            note = ""
            if message.get("tool_calls"):
                note += "（含工具调用声明）"
            if message.get("tool_call_id"):
                note += "（工具结果，id=%s）" % str(message.get("tool_call_id"))[:12]
        else:
            role, content, note = "?", message, ""
        lines.append("---- 第 %d 条 / 角色：%s %s ----" % (index, role, note))
        lines.append(_clip(_content_text(content)) or "（空）")
        lines.append("")

    lines.append("---- 模型回答 ----")
    lines.append("回报的模型：%s" % (record.get("response_model") or "（未知）"))
    lines.append(_clip(record.get("response_text")) or "（空）")
    if record.get("tool_calls"):
        lines.append("模型发起的工具调用：%s" % ", ".join(record["tool_calls"]))
    lines.append("")

    try:
        with open(text_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        logger.warning("[抓包] 写文本失败：%s", exc)

    # 每次写完顺手做两件清理：当天条数超了裁掉最旧的，天数超了删掉最旧的那天
    try:
        _ENTRY_COUNTS[day] = _ENTRY_COUNTS.get(day, _count_lines(json_path)) 
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
    if count <= keep:
        return

    try:
        with open(json_path, encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        lines = lines[-keep:]
        with open(json_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        _ENTRY_COUNTS[day] = len(lines)
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


def _extract_response(result: Any) -> tuple[str, str, list[str]]:
    """从接口库的返回里取出：文本、真实模型名、工具调用名。"""
    text, model, calls = "", "", []
    try:
        model = str(getattr(result, "model", "") or "")
        choices = getattr(result, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                text = str(getattr(message, "content", "") or "")
                for call in getattr(message, "tool_calls", None) or []:
                    function = getattr(call, "function", None)
                    name = getattr(function, "name", None) if function else None
                    if name:
                        calls.append(str(name))
    except Exception:
        pass
    return text, model, calls


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
            with open(path, encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
        except OSError:
            continue
        days.append({"date": name[:-6], "size": size, "count": count})
    return days


def _read_records(day: str) -> list[dict[str, Any]]:
    """读取某天的全部记录（原始字典）。"""
    path = os.path.join(output_dir(), "%s.jsonl" % day)
    records: list[dict[str, Any]] = []
    if not os.path.isfile(path):
        return records
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("[抓包] 读取失败：%s", exc)
    return records


def list_entries(day: str) -> list[dict[str, Any]]:
    """某天每条请求的摘要（不含完整正文，界面列表用）。

    最新的排在最前面；`index` 仍然是文件里的原始序号，
    界面拿它去取单条完整记录。
    """
    records = _read_records(day)
    summaries: list[dict[str, Any]] = []
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        messages = record.get("messages") or []
        summaries.append({
            "index": index,
            "time": record.get("time", ""),
            "model": record.get("model") or "",
            "response_model": record.get("response_model") or "",
            "tools": record.get("tools") or [],
            "message_count": len(messages),
            "has_tool_message": any(
                isinstance(m, dict) and m.get("role") == "tool" for m in messages
            ),
            "reply_length": len(str(record.get("response_text") or "")),
        })
    return summaries


def get_entry(day: str, index: int) -> dict[str, Any] | None:
    """取某一天的某一条完整记录。"""
    records = _read_records(day)
    if index < 0 or index >= len(records):
        return None
    return records[index]


def clear_day(day: str) -> bool:
    """删掉某天的抓包文件（jsonl + txt）。"""
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
    return removed


def install(
    enabled: bool,
    max_chars: int = 0,
    console: bool = True,
    keep_days: int = 30,
    keep_entries: int = 30,
) -> bool:
    """按设置决定是否安装抓包挂钩；返回是否装上了。"""
    SETTINGS.enable = bool(enabled)
    SETTINGS.max_chars = int(max_chars or 0)
    SETTINGS.console = bool(console)
    SETTINGS.keep_days = max(1, int(keep_days or 30))
    SETTINGS.keep_entries = max(0, int(keep_entries or 0))
    if not SETTINGS.enable:
        SETTINGS.enable = False
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
                messages = kwargs.get("messages") or []
                system_prompt = ""
                for message in messages:
                    if isinstance(message, dict) and message.get("role") == "system":
                        system_prompt = _content_text(message.get("content"))
                        break
                snapshot = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "model": kwargs.get("model"),
                    "stream": bool(kwargs.get("stream")),
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "tools": _tool_names(kwargs.get("tools")),
                    "tool_choice": kwargs.get("tool_choice"),
                }
            except Exception as exc:
                logger.debug("[抓包] 读取请求失败：%s", exc)

        result = await original(self, *args, **kwargs)

        if snapshot:
            try:
                if snapshot.get("stream"):
                    text, model, calls = "", "", []
                else:
                    text, model, calls = _extract_response(result)
                snapshot.update({
                    "response_model": model,
                    "response_text": text,
                    "tool_calls": calls,
                })
                _write_dump(snapshot)
                if SETTINGS.console:
                    logger.info(
                        "[抓包] 已记录：工具%d个 / 消息%d条 / 提示%d字 → 回报=%s 回答%d字",
                        len(snapshot.get("tools") or []),
                        len(snapshot.get("messages") or []),
                        len(str(snapshot.get("system_prompt") or "")),
                        model or "未知",
                        len(text),
                    )
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
