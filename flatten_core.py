"""工具回传的"发送前换身份"（纯逻辑，不依赖 AstrBot，可单独测试）。

## 这个插件是怎么来的

上游（谷歌那边）的校验一旦要求"函数返回必须带名字"，而我们发出去的这条工具回传
偏偏没有名字（转换环节一直没变，是它开始严了），整条请求就被打回：

    *.contents[N].parts[0].function_response.name: Name cannot be empty

它严起来之前屁事没有；严起来之后，只要请求里带着工具回传就必然被拒。

## 只干一件事：换身份，不动内容

把 `role="tool"` 那条整段原样搬进一条普通消息（默认系统侧）——外面套个壳，
里面就是原版给的那段文字，一个标点都不加、不删、不重排：

- 原版贴在末尾的提醒（`[SYSTEM NOTICE] …`）留在里面；
- 里面太长就省略多余的部分，但末尾那段提醒保留；
- 助手的工具申请、双方发言、消息顺序、工具清单，一律原样；
- 存储一个字节都不碰（那是 AstrBot 的事）。
"""

from __future__ import annotations

import json
from typing import Any

# 单条工具回传最多带多少字（超了省略多余部分）
DEFAULT_RESULT_MAX_CHARS = 800
# 一次工具往返（并行好几路）合计最多带多少字
DEFAULT_ROUND_MAX_CHARS = 2000

# AstrBot 自己贴在工具结果末尾的那类提醒，长这样开头
NATIVE_NOTICE_MARK = "[SYSTEM NOTICE]"

# 截断时补的记号
TRUNCATED_MARK = "……（太长，后面省略）"


def clip_text(value: Any, limit: int) -> str:
    """按上限裁一段文字，**不动它的换行和空格**（原样最要紧）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + TRUNCATED_MARK
    return text


def split_native_notice(content: Any) -> tuple[str, str]:
    """把"提醒"从工具回传里剥出来，返回（正文，提醒）。

    为什么必须剥：原版的重复调用提醒是**拼在结果末尾**的；
    按字数省略时被切掉的恰恰是尾巴——提醒就没了。
    所以先剥出来，截断正文，再把提醒原样接回去。
    """
    text = content if isinstance(content, str) else clip_text(content, 0)
    pos = text.find(NATIVE_NOTICE_MARK)
    if pos < 0:
        return text, ""
    return text[:pos].rstrip(), text[pos:].strip()


def normalize_role(role: str) -> str:
    """只认 system / user 两种；填 assistant 或乱填，一律按 system 处理。

    为什么不许 assistant：工具回传挂到助手名下，弱模型会把那段原始内容
    当成自己的说话方式照抄出来。
    """
    return "user" if str(role or "").strip().lower() == "user" else "system"


def _native_repeat_notice(tool_name: str, streak: int) -> str:
    """按 AstrBot 原版的阈值和原文，生成"重复调用"提醒。

    直接取执行器类上的常量和模板：措辞不重写，阈值不另定。
    拿不到（比如本体改了类名）就返回空，宁可少一句也不自己编。
    """
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner as Runner,
        )

        if streak >= Runner.REPEATED_TOOL_NOTICE_L3_THRESHOLD:
            template = Runner.REPEATED_TOOL_NOTICE_L3_TEMPLATE
        elif streak >= Runner.REPEATED_TOOL_NOTICE_L2_THRESHOLD:
            template = Runner.REPEATED_TOOL_NOTICE_L2_TEMPLATE
        elif streak >= Runner.REPEATED_TOOL_NOTICE_L1_THRESHOLD:
            template = Runner.REPEATED_TOOL_NOTICE_L1_TEMPLATE
        else:
            return ""
        return str(template.format(tool_name=tool_name, streak=streak))
    except Exception:  # noqa: BLE001
        return ""


def _tool_names_of_turn(messages: list[Any], index: int) -> dict[str, tuple[str, str]]:
    """从前面最近那条"助手申请"里取编号 →（工具名，参数）的对照表。

    回传那条只带编号，工具名藏在助手那份申请里，补提醒时要用到。
    """
    for back in range(index - 1, -1, -1):
        message = messages[back]
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            break
        if role == "assistant" and message.get("tool_calls"):
            table: dict[str, tuple[str, str]] = {}
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                table[str(call.get("id"))] = (
                    str(function.get("name") or ""),
                    json.dumps(function.get("arguments"), ensure_ascii=False)
                    if not isinstance(function.get("arguments"), str)
                    else str(function.get("arguments")),
                )
            return table
    return {}


def rewrite_tool_results(
    messages: list[Any],
    record_role: str = "system",
    result_max_chars: int = DEFAULT_RESULT_MAX_CHARS,
    round_max_chars: int = DEFAULT_ROUND_MAX_CHARS,
) -> list[dict]:
    """只换"工具回传"的身份：原样包进一条普通消息，其余消息一字不动。

    1. `role == "tool"` → 内容原样，外面套一条普通消息（默认系统侧）；
    2. 连着好几条回传（并行调用）→ 合进同一条，中间用换行隔开；
    3. 里面超字数 → 省略多余部分，末尾提醒保留；
    4. 别的消息一律不动。
    """
    output: list[dict] = []
    index = 0
    total = len(messages)

    # 这一问里"同一个工具 + 同一套参数"连续调了几次（原版的判定口径）
    last_key: tuple[str, str] | None = None
    streak = 0

    while index < total:
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "tool":
            if isinstance(message, dict) and message.get("role") == "user":
                last_key, streak = None, 0  # 新的一句提问，重新计数
            output.append(message)
            index += 1
            continue

        pieces: list[str] = []
        used = 0
        skipped = 0
        cursor = index
        while (
            cursor < total
            and isinstance(messages[cursor], dict)
            and messages[cursor].get("role") == "tool"
        ):
            item = messages[cursor]
            call_id = str(item.get("tool_call_id") or "")
            name = _tool_names_of_turn(messages, cursor).get(call_id, ("", ""))[0]

            key = (name, str(item.get("content"))[:200])
            streak = streak + 1 if key == last_key else 1
            last_key = key

            body, native = split_native_notice(item.get("content"))
            text = clip_text(body, result_max_chars)
            if native:
                text = text + "\n" + native
            elif name:
                extra = _native_repeat_notice(name, streak)
                if extra:
                    text = text + "\n" + extra

            if pieces and used + len(text) > round_max_chars:
                skipped += 1
            else:
                pieces.append(text)
                used += len(text)
            cursor += 1

        content = "\n".join(pieces)
        if skipped:
            content = content + "\n" + TRUNCATED_MARK + ("（另有 %d 条没带进来）" % skipped)
        output.append({"role": normalize_role(record_role), "content": content.strip()})
        index = cursor

    return output


def has_tool_format(messages: list[Any]) -> bool:
    """还有没有"工具回传"（只有这种会让上游拒收）。"""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            return True
    return False
