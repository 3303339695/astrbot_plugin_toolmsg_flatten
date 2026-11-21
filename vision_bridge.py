"""非多模态模型兼容视觉回溯。

## 要解决的问题

「Visual Recall - 视觉回溯」按序号取图那一步，交回来的是真图片。它自己的注释里
写得很明白：**当前会话的模型必须支持图片输入，否则只保留路径文本，模型看不到图像内容。**

主模型是纯文本的（比如 DeepSeek 的 pro 型号），那就是花了时间、花了调用次数，
取回来一个自己看不见的东西——还可能被那句"本会话有历史图片"勾着反复取。

## 这里怎么补

在取图工具返回的那一刻拦下来：

1. 主模型能看图 → 原样放行，什么都不动
2. 主模型看不了图 → 把图片交给「图片转述模型」看一眼，换回一段文字，
   原图不往主模型那边送
3. 转述模型没配、或者调用失败 → 原样放行，让本体自己的兜底逻辑去处理

换回来的文字会跟着原来的标签一起交回去，模型看到的仍然是"第几张、什么时候、
是图还是动图"，只是把图像换成了文字描述。

## 不动别人的东西

只包这一个取图工具，一行本体源码不改，视觉回溯插件自己的文件也不改。
转述用的是本体配置里那个「图片转述模型」，跟它处理用户发图时用的是同一个。
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from astrbot.api import logger

# 没配提示词时的兜底。要求它只描述看到的，不推测——省得模型拿着猜测当事实。
DEFAULT_PROMPT = (
    "请把这张图里能看到的内容如实描述出来：画面主体、场景、人物、颜色、"
    "以及图中出现的文字（原样抄下来，标明大概位置）。"
    "只描述你确实看到的，不要推测没看到的东西。"
)

# 转述结果的长度兜底，防止描述写成长篇塞爆上下文
CAPTION_MAX_CHARS = 500

_STATE = {"installed": False, "tool": None, "orig_call": None}


def _main_sees_images(context) -> bool:
    """当前会话的主模型能不能直接看图。

    跟本体的判断口径保持一致：看提供商配置里声明的输入模态里有没有图片。
    空列表按"没配置"处理，当作能看（本体的迁移兼容逻辑就是这样）。
    """
    try:
        plugin_ctx = context.context.context
        umo = context.context.event.unified_msg_origin
        provider = plugin_ctx.get_using_provider(umo)
        if provider is None:
            return False
        config = getattr(provider, "provider_config", None) or {}
        modalities = config.get("modalities", None)
        if modalities == []:
            return True
        return isinstance(modalities, list) and "image" in modalities
    except Exception as exc:  # noqa: BLE001
        logger.debug("[视觉桥接] 判断主模型能不能看图时出错，按看不了处理：%s", exc)
        return False


def _caption_settings(context):
    """把转述模型和提示词取出来。"""
    plugin_ctx = context.context.context
    umo = context.context.event.unified_msg_origin
    try:
        cfg = plugin_ctx.get_config(umo=umo) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    pset = cfg.get("provider_settings", {}) or {}
    provider_id = str(pset.get("default_image_caption_provider_id") or "").strip()
    prompt = str(pset.get("image_caption_prompt") or "").strip() or DEFAULT_PROMPT
    return provider_id, prompt


async def _describe(provider, prompt: str, data_b64: str, mime: str) -> str:
    """把一张 base64 图片交给转述模型，换回一段文字。"""
    raw = base64.b64decode(data_b64)
    suffix = ".png" if "png" in (mime or "").lower() else ".jpg"

    tmp = tempfile.NamedTemporaryFile(
        prefix="vision_bridge_", suffix=suffix, delete=False
    )
    try:
        tmp.write(raw)
        tmp.close()
        resp = await provider.text_chat(prompt=prompt, image_urls=[tmp.name])
        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            return "（转述模型没有返回内容）"
        if len(text) > CAPTION_MAX_CHARS:
            text = text[:CAPTION_MAX_CHARS].rstrip() + "……（后略）"
        return text
    finally:
        try:
            Path(tmp.name).unlink()
        except Exception:  # noqa: BLE001
            pass


async def _to_text(context, result):
    """把结果里的图片换成文字描述。换不了就返回原来的东西。"""
    content = getattr(result, "content", None)
    if not isinstance(content, list) or not content:
        return result

    try:
        from mcp import types as mcp_types
    except Exception as exc:  # noqa: BLE001
        logger.debug("[视觉桥接] 拿不到图片内容类型，原样放行：%s", exc)
        return result

    provider_id, prompt = _caption_settings(context)
    if not provider_id:
        logger.info("[视觉桥接] 没配图片转述模型，原样放行")
        return result

    plugin_ctx = context.context.context
    provider = plugin_ctx.get_provider_by_id(provider_id)
    if provider is None:
        logger.warning("[视觉桥接] 转述模型 %s 找不到，原样放行", provider_id)
        return result

    replaced = 0
    new_content = []
    for item in content:
        if getattr(item, "type", "") != "image":
            new_content.append(item)
            continue

        data = getattr(item, "data", "") or ""
        mime = getattr(item, "mimeType", "image/jpeg") or "image/jpeg"
        if not data:
            new_content.append(item)
            continue

        try:
            caption = await _describe(provider, prompt, data, mime)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[视觉桥接] 这一张转述失败，交给本体兜底：%s", exc)
            new_content.append(item)
            continue

        replaced += 1
        new_content.append(
            mcp_types.TextContent(
                type="text",
                text=f"[这张图的内容，由转述模型看过之后写下]\n{caption}",
            )
        )

    if not replaced:
        return result

    logger.info("[视觉桥接] 主模型看不了图，已把 %d 张换成文字描述", replaced)
    try:
        return mcp_types.CallToolResult(content=new_content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[视觉桥接] 拼装新结果失败，原样放行：%s", exc)
        return result


async def _guarded_call(self, context, **kwargs):
    """包住取图工具：主模型看不了图就把图片换成文字。"""
    original = _STATE["orig_call"]
    result = await original(self, context, **kwargs)

    try:
        if _main_sees_images(context):
            return result
        return await _to_text(context, result)
    except Exception as exc:  # noqa: BLE001
        # 桥接出错绝不能把取图整个搞挂，原样放行
        logger.warning("[视觉桥接] 出错，原样放行：%s", exc)
        return result


def _find_tool(context):
    """在已注册的工具里，按类名找视觉回溯那个取图工具。"""
    manager = context.provider_manager.llm_tools
    for tool in list(getattr(manager, "func_list", []) or []):
        if type(tool).__name__ == "RecallImageTool":
            return tool
    return None


def install(context, enabled: bool) -> str:
    """装上或者还回去。返回一句状态，给启动日志用。"""
    if not enabled:
        return _restore()

    try:
        tool = _find_tool(context)
    except Exception as exc:  # noqa: BLE001
        return f"没装上（{exc}）"

    if tool is None:
        return "视觉回溯没装或没开，不用接"

    cls = type(tool)
    if not _STATE["installed"]:
        _STATE["orig_call"] = cls.call
    cls.call = _guarded_call

    _STATE["installed"] = True
    _STATE["tool"] = tool
    return "已接管取图工具"


def _restore() -> str:
    tool = _STATE.get("tool")
    if not _STATE["installed"] or tool is None:
        return "关闭"
    try:
        cls = type(tool)
        if _STATE.get("orig_call") is not None:
            cls.call = _STATE["orig_call"]
    except Exception as exc:  # noqa: BLE001
        return f"关闭（没还干净：{exc}）"
    _STATE["installed"] = False
    return "关闭（已还回视觉回溯）"


def uninstall() -> str:
    return _restore()
