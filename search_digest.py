"""联网搜索掏段摘要：只取接口自己给的那一小段。

## 为什么要这一块

DeepSeek 的搜索接口每条结果其实有两个口袋：

- **短摘要**：一百到三百字，就是"这页在讲什么"的那一段；
- **长摘要**：几百到几千字——大页面直接是整篇正文的节选。

AstrBot 自带的那个搜索工具先伸手掏**长的**那个，短的被丢掉了。
一次搜八条，一万二千字就进去了；词元费比搜索本身贵好几倍。

## 这跟"截断"不是一回事

截断是把长文拦腰砍断，留下半句话；这里是**换一个口袋拿**，
拿到的本来就是一段写好的摘要，句子完整、意思完整。

## 怎么装：运行时换方法，不动本体源码

插件加载时把 `DeepSeekWebSearchTool.call` 换成"只掏短口袋"的版本。
不改本体任何一个文件，升级不丢；工具的名字、参数说明、返回格式
全都照旧，模型那边完全无感。

## 边界

只管"搜索结果"这一个工具。别的工具一个字都不碰——
这是掏摘要，不是通用截断。
"""

from __future__ import annotations

import aiohttp

from astrbot.api import logger

# 短摘要兜底上限：正常情况下接口给的是一两百字，这里只防极端情况
DEFAULT_MAX_CHARS = 400

# 这次装的是哪个类、原来的方法是什么
_HELPERS: dict = {}
_STATE: dict = {"installed": False, "original": None, "tool": None}

# 多把钥匙轮着用（跟本体一个思路：账号级限速换钥匙没用，所以只做分摊）
_KEY_INDEX = 0


def _digest_text(value, limit: int) -> str:
    """把一段摘要收拾干净：去首尾空白，太长才兜底裁一刀。"""
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "……（后面还有，已省略）"
    return text


def _next_key(keys: list) -> str:
    """按顺序轮流取钥匙。"""
    global _KEY_INDEX
    if not keys:
        raise ValueError("DeepSeek API key is not configured in AstrBot.")
    key = keys[_KEY_INDEX % len(keys)]
    _KEY_INDEX += 1
    return str(key)


def _provider_settings_of(context) -> dict:
    """从执行上下文里把这一轮的服务商设置拿出来。"""
    agent_ctx = context.context
    event = agent_ctx.event
    cfg = agent_ctx.context.get_config(umo=event.unified_msg_origin)
    return cfg.get("provider_settings", {}) or {}


async def _digest_call(self, context, **kwargs):
    """只掏短摘要的那版搜索。参数校验和返回格式跟原版保持一致。"""
    settings = _provider_settings_of(context)
    keys = settings.get("websearch_deepseek_key", []) or []
    if not keys:
        return "Error: DeepSeek API key is not configured in AstrBot."

    query = kwargs.get("query")
    if not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 128:
        return "Error: query must be a non-blank string of 1-128 Unicode characters."

    count = kwargs.get("count", 10)
    if type(count) is not int or not 1 <= count <= 50:
        return "Error: count must be an integer between 1 and 50."

    need_content = kwargs.get("need_content", False)
    if not isinstance(need_content, bool):
        return "Error: need_content must be a boolean."

    ori_question = kwargs.get("ori_question")
    if ori_question is not None and (
        not isinstance(ori_question, str) or not 1 <= len(ori_question) <= 128
    ):
        return "Error: ori_question must be a string of 1-128 Unicode characters."

    payload = {"query": query, "count": count, "need_content": need_content}
    if ori_question is not None:
        payload["ori_question"] = ori_question

    key = _next_key(keys)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(
            "https://api.deepseek.com/v1/tools/search",
            json=payload,
            headers=headers,
        ) as response:
            if response.status != 200:
                reason = await response.text()
                raise RuntimeError(
                    f"DeepSeek web search failed: {reason}, status: {response.status}"
                )
            data = await response.json()

    limit = int(SETTINGS.get("max_chars") or DEFAULT_MAX_CHARS)
    parts = _HELPERS
    results = []
    for item in data.get("results", []) or []:
        # 只掏短的那一段；短口袋空了才退回去用长的
        snippet = _digest_text(item.get("snippet"), limit) or _digest_text(
            item.get("summary"), limit
        )
        if need_content and item.get("content"):
            snippet = "\n\n".join(filter(None, [snippet, item["content"]]))
        results.append(
            parts["SearchResult"](
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=snippet,
                favicon=item.get("site_icon"),
            )
        )
    if not results:
        return "Error: DeepSeek web search does not return any results."
    logger.debug(
        "[联网搜索摘要] 这次只掏了 %d 条短摘要，合计 %d 字",
        len(results),
        sum(len(r.snippet or "") for r in results),
    )
    return parts["payload"](results)


def _load_helpers() -> dict:
    """把本体里现成的那几个零件拿过来；拿不到就整块不装。"""
    from astrbot.core.tools import web_search_tools as wst

    return {
        "module": wst,
        "tool": wst.DeepSeekWebSearchTool,
        "SearchResult": wst.SearchResult,
        "payload": wst._search_result_payload,
    }


SETTINGS = {"max_chars": DEFAULT_MAX_CHARS}


def install(enabled: bool, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """装上或者还回去。返回一句状态，给启动日志用。"""
    SETTINGS["max_chars"] = max(80, int(max_chars or DEFAULT_MAX_CHARS))

    if not enabled:
        return "关闭（" + _restore() + "）"

    if _STATE["installed"] and _STATE["tool"] is not None:
        return "已接管（沿用之前那次）"

    try:
        parts = _load_helpers()
    except Exception as exc:  # noqa: BLE001
        return f"没装上（{exc}）"

    tool = parts["tool"]
    original = tool.call
    if getattr(original, "_digest_wrapped", False):
        # 热重载：旧壳还挂在类上，但它的全局量指向已经作废的旧模块。
        # 先把壳剥掉拿到真身，再用这次新模块的全局重新包一层，
        # 否则改了设置重载之后，读的还是上一轮的旧值。
        original = getattr(original, "_digest_original", original)

    async def wrapper(self, context, **kwargs):
        try:
            return await _digest_call(self, context, **kwargs)
        except Exception as exc:  # noqa: BLE001
            # 出错绝不能让搜索整个挂掉：退回原版，最多是这一回多带点字
            logger.warning("[联网搜索摘要] 出错，这一次退回原版：%s", exc)
            return await original(self, context, **kwargs)

    wrapper._digest_wrapped = True  # type: ignore[attr-defined]
    wrapper._digest_original = original  # type: ignore[attr-defined]
    tool.call = wrapper

    _HELPERS.clear()
    _HELPERS.update(parts)
    _STATE.update(installed=True, original=original, tool=tool)
    return "已接管"


def _restore() -> str:
    """把方法还回本体（关开关或卸载时用）。"""
    tool = _STATE.get("tool")
    original = _STATE.get("original")
    if tool is None or original is None:
        return "本来就没装"
    try:
        if getattr(tool.call, "_digest_wrapped", False):
            tool.call = original
        _STATE.update(installed=False, original=None, tool=None)
        return "已还回本体"
    except Exception as exc:  # noqa: BLE001
        return f"没还干净（{exc}）"


def uninstall() -> str:
    """卸载钩子。"""
    return _restore()
