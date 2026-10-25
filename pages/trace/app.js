// 抓包查看页：调用插件自己的三个接口，把结果铺在页面上。
// 页面由 AstrBot 面板以 iframe 形式打开，接口通过注入的 bridge 对象调用。

const bridge = window.AstrBotPluginPage;

const elements = {
  daySelect: document.getElementById("day-select"),
  modelSelect: document.getElementById("model-select"),
  refresh: document.getElementById("refresh"),
  clear: document.getElementById("clear"),
  entryList: document.getElementById("entry-list"),
  listEmpty: document.getElementById("list-empty"),
  listPanel: document.querySelector(".list-panel"),
  detailPanel: document.querySelector(".detail-panel"),
  detail: document.getElementById("detail"),
  detailEmpty: document.getElementById("detail-empty"),
  detailMeta: document.getElementById("detail-meta"),
  detailSystem: document.getElementById("detail-system"),
  detailMessages: document.getElementById("detail-messages"),
  detailReply: document.getElementById("detail-reply"),
};

const state = {
  day: "",
  model: "",
  entries: [],
  activeIndex: -1,
};

function modelOf(entry) {
  return entry.response_model || entry.model || "";
}

const ROLE_LABELS = {
  system: "系统",
  user: "用户",
  assistant: "助手",
  tool: "工具结果",
};

function setText(node, text) {
  node.textContent = text == null ? "" : String(text);
}

// 消息正文可能是一整段文字，也可能是"一串碎片"（文字 + 图片 + 语音等）。
// 碎片直接当文字显示会变成 [object Object]，所以要逐块读出来。
function formatContentPart(part) {
  if (part == null) return "";
  if (typeof part === "string") return part;
  if (typeof part !== "object") return String(part);

  if (typeof part.text === "string") return part.text;

  const type = String(part.type || "").toLowerCase();
  if (type === "image_url" || part.image_url) return "〔图片〕";
  if (type === "input_audio" || type === "audio" || part.audio_url) return "〔语音〕";
  if (type === "file" || part.file) return "〔文件〕";

  try {
    return JSON.stringify(part, null, 2);
  } catch (error) {
    return String(part);
  }
}

function formatToolCalls(calls) {
  if (!Array.isArray(calls) || !calls.length) return "";
  const lines = calls.map((call) => {
    const fn = (call && call.function) || (call && call.function_call) || {};
    const name = fn.name || (call && call.name) || "（没写名字）";
    let args = fn.arguments;
    if (args === undefined) args = call && call.arguments;
    if (args && typeof args === "object") {
      try {
        args = JSON.stringify(args, null, 2);
      } catch (error) {
        args = String(args);
      }
    }
    const id = call && call.id ? `（编号 ${call.id}）` : "";
    return `→ 调用 ${name}${id}\n   参数：${args || "（无）"}`;
  });
  return "—— 工具调用声明 ——\n" + lines.join("\n");
}

function formatContent(content) {
  if (content == null) return "（空）";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const pieces = content.map(formatContentPart).filter((item) => item !== "");
    return pieces.length ? pieces.join("\n") : "（空）";
  }
  if (typeof content === "object") {
    try {
      return JSON.stringify(content, null, 2);
    } catch (error) {
      return String(content);
    }
  }
  return String(content);
}

function formatSize(bytes) {
  if (!bytes) return "0";
  if (bytes < 1024) return bytes + " 字节";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

async function callGet(name, params) {
  if (!bridge || typeof bridge.apiGet !== "function") {
    throw new Error("页面桥接对象不可用，请从 AstrBot 面板打开本页。");
  }
  return bridge.apiGet(name, params || {});
}

async function callPost(name, body) {
  if (!bridge || typeof bridge.apiPost !== "function") {
    throw new Error("页面桥接对象不可用，请从 AstrBot 面板打开本页。");
  }
  return bridge.apiPost(name, body || {});
}

function resultData(result) {
  if (!result) return {};
  if (result.data) return result.data;
  return result;
}

async function loadDays() {
  const data = resultData(await callGet("trace/days"));
  const days = data.days || [];
  elements.daySelect.innerHTML = "";
  if (!days.length) {
    elements.daySelect.innerHTML = '<option value="">（暂无记录）</option>';
    state.day = "";
    return;
  }
  for (const day of days) {
    const option = document.createElement("option");
    option.value = day.date;
    option.textContent = `${day.date}（${day.count} 条 / ${formatSize(day.size)}）`;
    elements.daySelect.appendChild(option);
  }
  if (!state.day || !days.some((d) => d.date === state.day)) {
    state.day = days[0].date;
  }
  elements.daySelect.value = state.day;
}

async function loadEntries() {
  if (!state.day) {
    state.entries = [];
    fillModelSelect();
    renderEntries();
    return;
  }
  const data = resultData(await callGet("trace/entries", { date: state.day }));
  state.entries = data.entries || [];
  fillModelSelect();
  renderEntries();
}

// 把这一天出现过的模型列成下拉选项，供筛选
function fillModelSelect() {
  const names = [];
  for (const entry of state.entries) {
    const name = modelOf(entry);
    if (name && !names.includes(name)) names.push(name);
  }
  names.sort();

  if (state.model && !names.includes(state.model)) {
    state.model = "";
  }

  elements.modelSelect.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部模型";
  elements.modelSelect.appendChild(all);

  for (const name of names) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    elements.modelSelect.appendChild(option);
  }
  elements.modelSelect.value = state.model;
}

function renderEntries() {
  elements.entryList.innerHTML = "";
  if (!state.day) {
    elements.listEmpty.hidden = false;
    setText(
      elements.listEmpty,
      "还没有抓包记录。先让机器人正常对话一次（抓包开关要开着），再回来点刷新。",
    );
    return;
  }
  const shown = state.entries.filter(
    (entry) => !state.model || modelOf(entry) === state.model,
  );
  elements.listEmpty.hidden = shown.length > 0;
  if (!shown.length) {
    setText(
      elements.listEmpty,
      state.model
        ? `这一天没有 ${state.model} 的记录，换个模型或选「全部模型」。`
        : "这一天还没有记录。",
    );
  }

  shown.forEach((entry) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "entry" + (entry.index === state.activeIndex ? " active" : "");

    const line1 = document.createElement("div");
    line1.className = "line1";
    const time = document.createElement("span");
    setText(time, (entry.time || "").slice(11));
    const reply = document.createElement("span");
    setText(reply, `回答 ${entry.reply_length} 字`);
    line1.appendChild(time);
    line1.appendChild(reply);

    const line2 = document.createElement("div");
    line2.className = "line2";
    const toolTag = document.createElement("span");
    toolTag.className = "tag";
    setText(toolTag, `工具 ${entry.tools.length}`);
    line2.appendChild(toolTag);

    const msgTag = document.createElement("span");
    msgTag.className = "tag";
    setText(msgTag, `消息 ${entry.message_count}`);
    line2.appendChild(msgTag);

    if (entry.has_tool_message) {
      const warnTag = document.createElement("span");
      warnTag.className = "tag warn";
      setText(warnTag, "含原生工具消息");
      line2.appendChild(warnTag);
    }

    const modelSpan = document.createElement("span");
    setText(modelSpan, entry.response_model || entry.model || "");
    line2.appendChild(modelSpan);

    button.appendChild(line1);
    button.appendChild(line2);
    button.addEventListener("click", () => showEntry(entry.index));
    elements.entryList.appendChild(button);
  });
}

async function showEntry(index) {
  state.activeIndex = index;
  renderEntries();
  const data = resultData(await callGet("trace/entry", { date: state.day, index }));
  const entry = data.entry;
  if (!entry) return;

  elements.detailEmpty.hidden = true;
  elements.detail.hidden = false;
  // 右栏是独立滚动的：换一条记录就回到顶部，不然会停在上一份内容的中间
  if (elements.detailPanel) elements.detailPanel.scrollTop = 0;

  const tools = entry.tools || [];
  const metaLines = [
    `时间：${entry.time || ""}`,
    `请求的模型：${entry.model || "（未指定）"}`,
    `回报的模型：${entry.response_model || "（未知）"}`,
    `带的工具：${tools.length ? tools.join("、") : "无"}`,
  ];
  setText(elements.detailMeta, metaLines.join("\n"));

  setText(elements.detailSystem, formatContent(entry.system_prompt) || "（没有单独的 system 字段）");

  elements.detailMessages.innerHTML = "";
  (entry.messages || []).forEach((message, index2) => {
    const box = document.createElement("div");
    box.className = "message";

    const role = document.createElement("div");
    role.className = "role";
    const roleName = message && message.role ? ROLE_LABELS[message.role] || message.role : "?";
    const calls = message && Array.isArray(message.tool_calls) ? message.tool_calls : [];
    let note = `第 ${index2 + 1} 条 · ${roleName}`;
    if (message && message.tool_call_id) note += ` · 工具结果（编号 ${message.tool_call_id}）`;
    if (calls.length) note += ` · 含 ${calls.length} 条工具调用声明`;
    setText(role, note);

    const body = document.createElement("div");
    body.className = "body";
    const blocks = [];
    const text = formatContent(message ? message.content : null);
    if (text && text !== "（空）") blocks.push(text);
    if (calls.length) blocks.push(formatToolCalls(calls));
    if (text === "（空）" && !calls.length) blocks.push("（空）");
    if (message && message.tool_call_id && !blocks.length) blocks.push("（这条只有工具结果，没有正文）");
    setText(body, blocks.join("\n\n"));

    box.appendChild(role);
    box.appendChild(body);
    elements.detailMessages.appendChild(box);
  });

  setText(elements.detailReply, formatContent(entry.response_text) || "（空）");
}

async function refreshAll() {
  try {
    await loadDays();
    await loadEntries();
  } catch (error) {
    const raw = error && error.message ? error.message : String(error);
    let hint = raw;
    if (raw.includes("未找到该路由")) {
      hint =
        "接口还没注册上。页面是从磁盘直接读的，所以能打开；但接口要插件重新加载后才会注册。" +
        "请回插件列表点一次「重载」，再回到这里刷新。";
    }
    elements.listEmpty.hidden = false;
    setText(elements.listEmpty, "读取失败：" + hint);
  }
}

elements.refresh.addEventListener("click", refreshAll);

elements.daySelect.addEventListener("change", async () => {
  state.day = elements.daySelect.value;
  state.model = "";
  state.activeIndex = -1;
  disarmClear();
  elements.detail.hidden = true;
  elements.detailEmpty.hidden = false;
  await loadEntries();
});

elements.modelSelect.addEventListener("change", () => {
  state.model = elements.modelSelect.value;
  state.activeIndex = -1;
  elements.detail.hidden = true;
  elements.detailEmpty.hidden = false;
  renderEntries();
});

// 面板给插件页开的沙箱里没有弹窗权限，window.confirm 会被静默拦掉，
// 所以改成"点一次变成确认、再点一次才删"的两步式，不依赖弹窗。
let clearArmed = false;
let clearTimer = null;

function disarmClear() {
  clearArmed = false;
  elements.clear.textContent = "清空当天";
  if (clearTimer) {
    clearTimeout(clearTimer);
    clearTimer = null;
  }
}

elements.clear.addEventListener("click", async () => {
  if (!state.day) return;

  if (!clearArmed) {
    clearArmed = true;
    elements.clear.textContent = "真的删除？再点一次";
    clearTimer = setTimeout(disarmClear, 5000);
    return;
  }

  disarmClear();
  try {
    await callPost("trace/clear", { date: state.day });
  } catch (error) {
    elements.listEmpty.hidden = false;
    setText(elements.listEmpty, "删除失败：" + (error && error.message ? error.message : error));
    return;
  }
  state.activeIndex = -1;
  elements.detail.hidden = true;
  elements.detailEmpty.hidden = false;
  await refreshAll();
});

refreshAll();
