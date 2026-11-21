// 接口抓包页：左边列当天抓到的请求，右边看选中那条的完整内容。
// 页面由 AstrBot 面板以 iframe 打开，接口通过注入的 bridge 对象调用。
//
// 检索是"混合"的：
//   1. 回车 / 点「检索」→ 后端按词过滤，左边只留命中的记录，每条带命中处数；
//   2. 当前详情若仍在命中结果里，右侧立刻滚到并高亮第一个命中；
//   3. 顶部「上一个 / 下一个」只在右侧详情里跳，计数是"第 N / 共 M 个匹配"；
//   4. 清空检索词，列表恢复全部、右侧高亮清除。

const bridge = window.AstrBotPluginPage;

const elements = {
  daySelect: document.getElementById("day-select"),
  modelSelect: document.getElementById("model-select"),
  searchForm: document.getElementById("search-form"),
  searchInput: document.getElementById("search-input"),
  searchGo: document.getElementById("search-go"),
  searchPrev: document.getElementById("search-prev"),
  searchNext: document.getElementById("search-next"),
  searchStatus: document.getElementById("search-status"),
  refresh: document.getElementById("refresh"),
  stopAll: document.getElementById("stop-all"),
  openDir: document.getElementById("open-dir"),
  clear: document.getElementById("clear"),
  notice: document.getElementById("notice"),
  noticeText: document.getElementById("notice-text"),
  noticeClose: document.getElementById("notice-close"),
  listPanel: document.querySelector(".list-panel"),
  listBody: document.querySelector(".list-body"),
  entryList: document.getElementById("entry-list"),
  listEmpty: document.getElementById("list-empty"),
  listCount: document.getElementById("list-count"),
  daySummary: document.getElementById("day-summary"),
  detailPanel: document.querySelector(".detail-panel"),
  detailScroll: document.querySelector(".detail-scroll"),
  detailHead: document.getElementById("detail-head"),
  detailTime: document.getElementById("detail-time"),
  detailState: document.getElementById("detail-state"),
  detailDuration: document.getElementById("detail-duration"),
  detailUsage: document.getElementById("detail-usage"),
  detailMeta: document.getElementById("detail-meta"),
  detailHealth: document.getElementById("detail-health"),
  detailSources: document.getElementById("detail-sources"),
  detailBadges: document.getElementById("detail-badges"),
  detailEmpty: document.getElementById("detail-empty"),
  detail: document.getElementById("detail"),
  detailSystem: document.getElementById("detail-system"),
  messageTitle: document.getElementById("message-title"),
  detailMessages: document.getElementById("detail-messages"),
  requestParameters: document.getElementById("request-parameters"),
  detailParameters: document.getElementById("detail-parameters"),
  detailReply: document.getElementById("detail-reply"),
  filesToggle: document.getElementById("files-toggle"),
  filesPopover: document.getElementById("files-popover"),
  filesClose: document.getElementById("files-close"),
  filesSummary: document.getElementById("files-summary"),
  filesDir: document.getElementById("files-dir"),
  filesRefresh: document.getElementById("files-refresh"),
  filesList: document.getElementById("files-list"),
  filesPreview: document.getElementById("files-preview"),
  filesList: document.getElementById("files-list"),
  filesPreview: document.getElementById("files-preview"),
};

const state = {
  day: "",
  model: "",
  query: "", // 已经生效的检索词（回车 / 点检索之后）
  entries: [], // 当天全部记录
  results: null, // 检索结果（带 match_count）；null = 没有过滤
  truncated: false,
  summary: null,
  activeIndex: -1,
  activeId: "",
  activeEntry: null,
};

const ROLE_LABELS = {
  system: "系统",
  user: "用户",
  assistant: "助手",
  tool: "工具",
};

// 检索高亮不碰这些界面文字，只认内容
const SEARCH_SKIP = [
  "[hidden]",
  ".empty",
  ".section-title",
  ".tool-heading",
  ".role",
  ".request-parameters > summary",
  ".folded-value > summary",
  ".state",
  ".detail-usage",
  ".detail-duration",
  ".meta-key",
  ".notice",
].join(", ");

let entriesGeneration = 0;
let detailToken = 0;
let searchToken = 0;
let matches = [];
let matchIndex = -1;
let noticeTimer = null;

let subscription = null;
let sseGeneration = 0;
let retryTimer = null;
let durationTimer = null;
let syncing = false;
let syncAgain = false;

let files = [];
let filesDir = "";
let filesLoaded = false;
let filesArmed = "";
let filesTimer = null;

// ------------------------------------------------------------ 小工具

function setText(node, text) {
  if (node) node.textContent = text == null ? "" : String(text);
}

function errorText(error) {
  if (!error) return "未知错误";
  const raw = error.message || String(error);
  if (raw.includes("未找到该路由")) {
    return "接口还没注册上。页面是从磁盘直接读的，所以能打开；接口要插件重新加载后才会注册。请回插件列表点一次「重载」，再回来刷新。";
  }
  return raw;
}

function modelOf(entry) {
  return entry.response_model || entry.model || "";
}

function visibleEntries() {
  const base = state.results === null ? state.entries : state.results;
  if (!state.model) return base;
  return base.filter((entry) => modelOf(entry) === state.model);
}

function statusInfo(status) {
  if (status === "failed") return { cls: "is-failed", label: "失败" };
  if (status === "timeout") return { cls: "is-timeout", label: "已超时" };
  if (status === "pending") return { cls: "is-pending", label: "进行中" };
  return { cls: "is-complete", label: "完成" };
}

function formatClock(time) {
  return String(time || "").slice(11, 19);
}

function formatSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value < 1024) return value + " 字节";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / 1024 / 1024).toFixed(1) + " MB";
}

function pickNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function trimFixed(value) {
  return value.toFixed(1).replace(/\.0$/, "");
}

function formatTokens(value) {
  const number = pickNumber(value);
  if (number === null) return "—";
  if (number < 10000) return String(Math.round(number));
  const wan = number / 10000;
  if (number < 100000000 && wan < 9999.95) return `${trimFixed(wan)}万`;
  return `${trimFixed(number / 100000000)}亿`;
}

function formatRate(value) {
  const number = pickNumber(value);
  if (number === null) return "—";
  return `${Math.round(number * 100)}%`;
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

// 右侧顶部那行用量：输入 / 缓存读取 / 未缓存 / 输出 / 合计
function formatUsageLine(usage) {
  const data = usage && typeof usage === "object" ? usage : {};
  const prompt = pickNumber(data.prompt_tokens);
  let cached = pickNumber(data.cached_tokens);
  if (cached === null) cached = pickNumber(data.prompt_cache_hit_tokens);
  const completion = pickNumber(data.completion_tokens);
  const uncached = prompt !== null && cached !== null ? Math.max(0, prompt - cached) : null;
  let total = pickNumber(data.total_tokens);
  if (total === null && prompt !== null && completion !== null) total = prompt + completion;
  return [
    `输入 ${formatTokens(prompt)}`,
    `缓存读取 ${formatTokens(cached)}`,
    `未缓存 ${formatTokens(uncached)}`,
    `输出 ${formatTokens(completion)}`,
    `合计 ${formatTokens(total)}`,
  ].join(" · ");
}

function renderDaySummary() {
  const summary = state.summary;
  if (!summary || !summary.count) {
    setText(elements.daySummary, "");
    return;
  }
  const bits = [];
  if (pickNumber(summary.prompt_tokens) !== null) {
    bits.push(`输入 ${formatTokens(summary.prompt_tokens)}`);
  }
  if (pickNumber(summary.hit_rate) !== null) {
    bits.push(`缓存 ${formatRate(summary.hit_rate)}`);
  }
  setText(elements.daySummary, bits.join(" · "));
}

function resultData(result) {
  if (!result) return {};
  if (result.data) return result.data;
  return result;
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

function setNotice(text) {
  if (noticeTimer) {
    clearTimeout(noticeTimer);
    noticeTimer = null;
  }
  if (!text) {
    elements.notice.hidden = true;
    setText(elements.noticeText, "");
    return;
  }
  setText(elements.noticeText, text);
  elements.notice.hidden = false;
  noticeTimer = setTimeout(() => {
    elements.notice.hidden = true;
  }, 8000);
}

// ------------------------------------------------------------ 左侧列表

function renderEntries() {
  const scrollTop = elements.listBody ? elements.listBody.scrollTop : 0;
  const shown = visibleEntries();
  const fragment = document.createDocumentFragment();
  for (const entry of shown) fragment.appendChild(createEntryNode(entry));
  elements.entryList.replaceChildren(fragment);

  if (!state.day) {
    showListEmpty("还没有抓包记录。先让机器人正常对话一次（抓包开关要开着），再回来点刷新。");
  } else if (!shown.length) {
    const hits = state.results === null ? 0 : state.results.length;
    if (state.query && hits > 0 && state.model) {
      showListEmpty(`命中「${state.query}」的记录里没有 ${state.model} 的，换个模型或选「全部模型」。`);
    } else if (state.query) {
      showListEmpty(`没有命中「${state.query}」的记录，换个词或清空检索。`);
    } else if (state.model) {
      showListEmpty(`这一天没有 ${state.model} 的记录，换个模型或选「全部模型」。`);
    } else {
      showListEmpty("这一天还没有记录。");
    }
  } else {
    elements.listEmpty.hidden = true;
    setText(elements.listEmpty, "");
  }

  renderListMeta();
  if (elements.listBody) elements.listBody.scrollTop = scrollTop;
}

function showListEmpty(text) {
  elements.listEmpty.hidden = false;
  setText(elements.listEmpty, text);
}

function renderListMeta() {
  const total = state.entries.length;
  const shown = visibleEntries().length;
  const bits = [];
  if (state.query) {
    bits.push(`命中 ${shown} 条`);
    if (total) bits.push(`共 ${total} 条`);
  } else if (state.model) {
    bits.push(`筛选 ${shown} 条`);
    if (total) bits.push(`共 ${total} 条`);
  } else {
    bits.push(`共 ${total} 条`);
  }
  if (state.truncated) bits.push("只显示前 200 条");
  setText(elements.listCount, bits.join(" · "));
  renderDaySummary();
}

function createEntryNode(entry) {
  const status = statusInfo(entry.status);
  const button = document.createElement("button");
  button.type = "button";
  button.className = `entry ${status.cls}`;
  if (entry.id === state.activeId) button.classList.add("active");
  button.dataset.id = entry.id;

  const top = document.createElement("div");
  top.className = "entry-top";
  const time = document.createElement("span");
  time.className = "entry-time";
  setText(time, formatClock(entry.time) || "—");
  const model = document.createElement("span");
  model.className = "entry-model";
  setText(model, modelOf(entry) || "（模型未回报）");
  model.title = modelOf(entry) || "";
  const stateLabel = document.createElement("span");
  stateLabel.className = `state ${status.cls}`;
  setText(stateLabel, status.label);
  top.append(time, model, stateLabel);

  const bottom = document.createElement("div");
  bottom.className = "entry-bottom";
  const tools = Array.isArray(entry.tools) ? entry.tools.length : 0;
  bottom.append(
    inlineText(`工具 ${tools}`),
    inlineText(`消息 ${entry.message_count ?? 0}`),
    inlineText(`缓存 ${formatRate(entry.hit_rate)}`),
    inlineText(`回答 ${entry.reply_length ?? 0} 字`),
  );
  if (entry.match_count) {
    const hit = document.createElement("span");
    hit.className = "entry-hit";
    setText(hit, `命中 ${entry.match_count} 处`);
    hit.title = `这条记录里「${state.query}」命中 ${entry.match_count} 处`;
    bottom.append(hit);
  }

  button.append(top, bottom);
  button.addEventListener("click", () => {
    openEntry(entry.index);
  });
  return button;
}

function inlineText(text) {
  const span = document.createElement("span");
  setText(span, text);
  return span;
}

// ------------------------------------------------------------ 右侧详情

function isNarrow() {
  try {
    return window.matchMedia("(max-width: 900px)").matches;
  } catch (error) {
    return false;
  }
}

async function openEntry(index, preserve = false) {
  const summary = state.entries.find((entry) => entry.index === index);
  if (!summary) return;
  const token = ++detailToken;
  const id = summary.id;
  const day = state.day;
  state.activeIndex = index;
  state.activeId = id;
  renderEntries();

  let data;
  try {
    data = resultData(await callGet("trace/entry", { date: day, index, id }));
  } catch (error) {
    setNotice("读取这条记录失败：" + errorText(error));
    return;
  }
  if (token !== detailToken || state.day !== day || state.activeId !== id) return;
  const entry = data && data.entry;
  if (!entry) {
    setNotice("这条记录已经不在文件里了，刷新一下列表。");
    return;
  }

  const previousMatch = matchIndex;
  const previousScroll = elements.detailScroll ? elements.detailScroll.scrollTop : 0;
  state.activeEntry = entry;
  renderDetail(entry);
  if (elements.detailScroll) {
    if (preserve) {
      elements.detailScroll.scrollTop = previousScroll;
    } else if (state.query) {
      // 检索模式下交给下面的定位逻辑
      elements.detailScroll.scrollTop = 0;
    } else {
      // 平时直接落到最底下：最新那条消息、以及模型回复，都在底部
      elements.detailScroll.scrollTop = elements.detailScroll.scrollHeight;
    }
  }
  if (!preserve && isNarrow()) {
    // 窄屏是上下堆叠的：点一条就把详情带进视野，不然还得自己往下划
    elements.detailPanel.scrollIntoView({ block: "start" });
  }
  if (state.query) {
    syncHighlight({
      scroll: !preserve,
      start: 0,
      keepIndex: preserve,
      previous: previousMatch,
    });
  } else {
    clearMarks();
    updateSearchStatus();
  }
}

function renderDetail(entry) {
  elements.detailEmpty.hidden = true;
  elements.detail.hidden = false;
  elements.detailHead.hidden = false;

  setText(elements.detailTime, entry.time || "");
  renderDetailState(entry);
  renderDuration(entry);
  setText(elements.detailUsage, formatUsageLine(entry.usage));
  renderMeta(entry);
  renderHealth(entry);
  renderSystem(entry);
  // 体检和来源清单都收进折叠区，这里只渲染摘要行上的小标记
  renderSourceList(elements.detailSources, entry, sourceSpans(entry).length > 0);
  renderBadges(entry);

  const messages = Array.isArray(entry.messages) ? entry.messages : [];
  setText(elements.messageTitle, messages.length ? `请求消息 · ${messages.length} 条` : "请求消息");
  renderMessages(elements.detailMessages, messages, collectInjections(entry));
  renderParameters(entry);
  renderReply(elements.detailReply, entry);
}

function renderDetailState(entry) {
  const info = statusInfo(entry.status);
  elements.detailState.className = `state ${info.cls}`;
  setText(elements.detailState, info.label);
}

function renderDuration(entry) {
  if (!entry) {
    setText(elements.detailDuration, "");
    return;
  }
  let duration = entry.duration_ms;
  if (entry.status === "pending") {
    const started = Date.parse(String(entry.time || "").replace(" ", "T"));
    if (Number.isFinite(started)) duration = Math.max(0, Date.now() - started);
  }
  const number = pickNumber(duration);
  setText(
    elements.detailDuration,
    number === null
      ? "耗时 —"
      : `耗时 ${Math.round(number)} ms${entry.status === "pending" ? " · 进行中" : ""}`,
  );
}

function metaRow(key, value) {
  const row = document.createElement("div");
  row.className = "meta-row";
  const keyNode = document.createElement("span");
  keyNode.className = "meta-key";
  setText(keyNode, key);
  const valueNode = document.createElement("span");
  valueNode.className = "meta-value";
  setText(valueNode, value);
  row.append(keyNode, valueNode);
  return row;
}

function renderMeta(entry) {
  const tools = Array.isArray(entry.tools) ? entry.tools.map((tool) => String(tool)) : [];
  elements.detailMeta.replaceChildren(
    metaRow("请求模型", entry.model || "（未指定）"),
    metaRow("回报模型", entry.response_model || "（未知）"),
  );
  const row = metaRow("带的工具", tools.length ? `${tools.length} 个 · ${tools.join("、")}` : "无");
  if (tools.length) {
    const value = row.querySelector(".meta-value");
    if (value) value.title = tools.join("\n");
  }
  elements.detailMeta.append(row);

  const requester = entry && entry.requester;
  let origin = "主对话";
  let originTitle = "";
  if (requester && requester.kind === "插件") {
    origin = `${requester.plugin} 发起的`;
    originTitle = `${requester.file || ""}:${requester.line || ""}`;
  } else if (requester && requester.kind === "未知") {
    origin = "看不出来";
  }
  const originRow = metaRow("谁发起的", origin);
  if (originTitle) {
    const value = originRow.querySelector(".meta-value");
    if (value) value.title = originTitle;
  }
  elements.detailMeta.append(originRow);
}

function renderHealth(entry) {
  const box = elements.detailHealth;
  if (!box) return;
  const health = entry && entry.health;
  const stats = (health && health.stats) || null;
  if (!health || !stats) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  const issues = Array.isArray(health.issues) ? health.issues : [];
  const level = ["ok", "warn", "bad"].includes(health.level) ? health.level : "ok";
  const label = { ok: "体检正常", warn: "体检有疑点", bad: "体检有问题" }[level];
  const ratio = Math.round((Number(stats.tool_ratio) || 0) * 100);

  box.hidden = false;
  box.className = `detail-health level-${level}`;

  const head = document.createElement("div");
  head.className = "health-head";
  setText(
    head,
    `${label} · ${stats.messages || 0} 条消息 · ${stats.chars || 0} 字 · 工具结果占 ${ratio}%`,
  );
  box.replaceChildren(head);

  if (issues.length) {
    const list = document.createElement("div");
    list.className = "health-issues";
    for (const item of issues) {
      const row = document.createElement("div");
      row.className = "health-issue";
      setText(row, `${item.kind}：${item.detail}`);
      list.append(row);
    }
    box.append(list);
  }
}

function sourceSpans(entry) {
  // 从来源记录里挑出带区间的（改了系统提示的），按起点排序并合并重叠
  const record = entry && entry.sources;
  const list = record && Array.isArray(record.sources) ? record.sources : [];
  const spans = [];
  for (const item of list) {
    const start = Number(item.start);
    const end = Number(item.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    spans.push({ start, end, plugin: String(item.plugin || "未知插件"), mode: item.mode || "" });
  }
  spans.sort((a, b) => a.start - b.start);
  const merged = [];
  for (const span of spans) {
    const last = merged[merged.length - 1];
    if (last && span.start <= last.end) {
      last.end = Math.max(last.end, span.end);
      if (!last.plugins.includes(span.plugin)) last.plugins.push(span.plugin);
    } else {
      merged.push({ start: span.start, end: span.end, plugins: [span.plugin] });
    }
  }
  return merged;
}

function colorFor(name) {
  let hash = 0;
  const text = String(name || "");
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) % 997;
  }
  return hash % 8;
}

// 本体自己的钩子不叫某个插件，统一显示成"本体"
function sourceLabel(name) {
  const text = String(name || "");
  if (!text) return "未知";
  if (text === "astrbot" || text === "core" || text.startsWith("astrbot.core")) {
    return "本体";
  }
  return text;
}

function renderSystemSpans(parent, raw, spans) {
  const body = document.createElement("div");
  body.className = "system-spans";
  let cursor = 0;
  for (const span of spans) {
    const start = Math.max(0, Math.min(span.start, raw.length));
    const end = Math.max(start, Math.min(span.end, raw.length));
    if (start > cursor) {
      const plain = document.createElement("span");
      plain.textContent = raw.slice(cursor, start);
      body.append(plain);
    }
    const node = document.createElement("span");
    node.className = `src-mark src-${colorFor(span.plugins[0])}`;
    node.title = `这一段来自：${span.plugins.map(sourceLabel).join("、")}`;
    node.textContent = raw.slice(start, end);
    body.append(node);

    const tag = document.createElement("sup");
    tag.className = "src-tag";
    tag.textContent = span.plugins.map(sourceLabel).join("、");
    body.append(tag);
    cursor = end;
  }
  if (cursor < raw.length) {
    const rest = document.createElement("span");
    rest.textContent = raw.slice(cursor);
    body.append(rest);
  }
  parent.append(body);
}

function renderBadges(entry) {
  const box = elements.detailBadges;
  if (!box) return;
  box.replaceChildren();

  const health = entry && entry.health;
  if (health && health.issues && health.issues.length) {
    const level = health.level === "bad" ? "bad" : "warn";
    const chip = document.createElement("span");
    chip.className = `badge badge-${level}`;
    chip.textContent = level === "bad" ? "有问题" : "有疑点";
    chip.title = (health.issues || []).map((i) => i.kind).join("、");
    box.append(chip);
  }

  const record = entry && entry.sources;
  const items = record && Array.isArray(record.sources) ? record.sources : [];
  if (items.length) {
    const chip = document.createElement("span");
    chip.className = "badge badge-src";
    chip.textContent = `动了 ${items.length} 处`;
    chip.title = items
      .map((i) => `${sourceLabel(i.plugin)} — ${i.mode || "改动"}`)
      .join("\n");
    box.append(chip);
  }
}

function renderSystem(entry) {
  const messages = Array.isArray(entry.messages) ? entry.messages : [];
  const inside = messages
    .filter((message) => message && message.role === "system")
    .map((message) => formatContent(message.content).trim());
  const raw = typeof entry.system_prompt === "string" ? entry.system_prompt : "";
  const system = raw ? formatContent(raw) : "";
  const show = !!system && !inside.includes(system.trim());

  elements.detailSystem.replaceChildren();
  elements.detailSystem.hidden = !show;
  if (!show) return;

  const title = document.createElement("h2");
  title.className = "section-title";
  title.textContent = "系统提示";
  elements.detailSystem.append(title);

  const spans = sourceSpans(entry);
  if (spans.length && raw) {
    renderSystemSpans(elements.detailSystem, raw, spans);
  } else {
    appendBody(elements.detailSystem, system);
  }
}

function renderSourceList(parent, entry, colored) {
  if (!parent) return;
  parent.replaceChildren();

  const record = entry && entry.sources;
  const list = record && Array.isArray(record.sources) ? record.sources : [];
  if (!list.length) {
    parent.hidden = true;
    return;
  }
  parent.hidden = false;

  const box = document.createElement("details");
  box.className = "source-box";
  box.open = !colored;
  const head = document.createElement("summary");
  head.textContent = `本次请求里动过内容的插件 · ${list.length} 处`;
  box.append(head);

  const grid = document.createElement("div");
  grid.className = "source-grid";
  for (const item of list) {
    const row = document.createElement("div");
    row.className = "source-row";
    const name = document.createElement("span");
    name.className = `source-name src-${colorFor(item.plugin)}`;
    name.textContent = sourceLabel(item.plugin || "未知插件");
    const what = document.createElement("span");
    what.className = "source-what";
    const bits = [];
    if (item.chars) bits.push(`${item.mode || "改动"} ${item.chars} 字`);
    if (item.parts_chars) bits.push(`其中注入 ${item.parts_chars} 字`);
    if (item.contexts_added) bits.push(`另加 ${item.contexts_added} 条消息`);
    if (item.hook) bits.push(item.hook);
    what.textContent = bits.join(" · ") || "有改动";
    if (Array.isArray(item.parts) && item.parts.length) {
      row.title = item.parts.join("\n---\n").slice(0, 800);
    }
    row.append(name, what);
    grid.append(row);
  }
  box.append(grid);
  parent.append(box);
}

function renderParameters(entry) {
  const request = entry.request_raw;
  const usable = request && typeof request === "object";
  elements.requestParameters.hidden = !usable;
  if (!usable) return;
  elements.requestParameters.open = false;
  const parameters = { ...request };
  delete parameters.messages;
  try {
    setText(elements.detailParameters, JSON.stringify(parameters, null, 2));
  } catch (error) {
    setText(elements.detailParameters, "（其他请求参数整理失败）");
  }
}

function renderReply(parent, entry) {
  parent.replaceChildren();
  if (entry.status === "pending") {
    const loading = document.createElement("span");
    loading.className = "response-loading";
    loading.textContent = "响应中";
    parent.appendChild(loading);
    return;
  }
  if (entry.status === "failed" || entry.status === "timeout") {
    const error = document.createElement("div");
    error.className = "response-error";
    setText(
      error,
      entry.status === "timeout" ? "已超时，尚未收到响应" : entry.error || "请求失败",
    );
    parent.appendChild(error);
    return;
  }
  const raw = entry.response_raw && typeof entry.response_raw === "object" ? entry.response_raw : {};
  const content = entry.response_text ?? raw.content;
  const calls = entry.response_tool_calls ?? raw.tool_calls ?? entry.tool_calls;
  const hasBody = appendBody(parent, content);
  appendCalls(parent, calls);
  if (!hasBody && (!Array.isArray(calls) || !calls.length)) {
    appendBody(parent, "这一发没有任何输出");
  }
}

function collectInjections(entry) {
  // 从来源记录里收集"被塞进用户消息的内容块"，供消息渲染时套色
  const record = entry && entry.sources;
  const list = record && Array.isArray(record.sources) ? record.sources : [];
  const out = [];
  for (const item of list) {
    const parts = Array.isArray(item.parts) ? item.parts : [];
    for (const text of parts) {
      const body = String(text || "");
      if (body.length < 12) continue;
      out.push({ text: body, plugin: String(item.plugin || "未知插件") });
    }
  }
  return out;
}

function appendBodyColored(parent, value, injections) {
  const text = value == null ? "" : formatContent(value);
  if (!text) return false;
  const body = document.createElement("div");
  body.className = "body";

  const hits = [];
  for (const item of injections || []) {
    const idx = text.indexOf(item.text);
    if (idx < 0) continue;
    hits.push({ start: idx, end: idx + item.text.length, plugin: item.plugin });
  }
  hits.sort((a, b) => a.start - b.start);

  if (!hits.length) {
    body.textContent = text;
    parent.appendChild(body);
    return true;
  }

  let cursor = 0;
  for (const hit of hits) {
    if (hit.start < cursor) continue;
    if (hit.start > cursor) body.append(text.slice(cursor, hit.start));
    const mark = document.createElement("span");
    mark.className = `src-mark src-${colorFor(hit.plugin)}`;
    mark.title = `这段由 ${hit.plugin} 注入`;
    mark.textContent = text.slice(hit.start, hit.end);
    body.append(mark);
    const tag = document.createElement("sup");
    tag.className = "src-tag";
    tag.textContent = hit.plugin;
    body.append(tag);
    cursor = hit.end;
  }
  if (cursor < text.length) body.append(text.slice(cursor));
  parent.appendChild(body);
  return true;
}

function renderMessages(parent, messages, injections) {
  parent.replaceChildren();
  messages.forEach((message, index) => {
    const box = document.createElement("div");
    box.className = "message";
    const role = document.createElement("div");
    role.className = "role";
    const roleName = message && message.role ? ROLE_LABELS[message.role] || message.role : "?";
    setText(role, `第 ${index + 1} 条 · ${roleName}`);
    box.dataset.role = (message && message.role) || "";
    box.appendChild(role);

    if (message && message.role === "tool") {
      const label = document.createElement("div");
      label.className = "tool-title";
      setText(
        label,
        [message.name, message.tool_call_id].filter(Boolean).join(" · ") || "（无工具名或调用标识）",
      );
      box.appendChild(label);
      appendFolded(box, message.content == null ? "" : formatContent(message.content), 300);
    } else {
      const hasBody = appendBodyColored(box, message && message.content, injections);
      const calls = message && message.tool_calls;
      appendCalls(box, calls);
      if (!hasBody && (!Array.isArray(calls) || !calls.length)) appendBody(box, "（空）");
    }
    parent.appendChild(box);
  });
}

function appendBody(parent, value) {
  const text = value == null ? "" : formatContent(value);
  if (!text) return false;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  parent.appendChild(body);
  return true;
}

function appendFolded(parent, value, limit) {
  const text = value == null ? "" : typeof value === "string" ? value : JSON.stringify(value);
  if (Array.from(text).length <= limit) {
    const body = document.createElement("div");
    body.className = "tool-value";
    body.textContent = text || "（无）";
    parent.appendChild(body);
    return;
  }
  const details = document.createElement("details");
  details.className = "folded-value";
  const summary = document.createElement("summary");
  const head = Array.from(text).slice(0, limit).join("");
  const collapsed = `${head}…（已截断，点击展开）`;
  summary.textContent = collapsed;
  const full = document.createElement("div");
  full.className = "tool-value";
  full.textContent = text;
  details.append(summary, full);
  details.addEventListener("toggle", () => {
    summary.textContent = details.open ? "收起" : collapsed;
  });
  parent.appendChild(details);
}

function appendCalls(parent, calls) {
  if (!Array.isArray(calls) || !calls.length) return;
  const heading = document.createElement("div");
  heading.className = "tool-heading";
  heading.textContent = "工具调用";
  parent.appendChild(heading);
  calls.forEach((call) => {
    const item = document.createElement("div");
    item.className = "tool-detail";
    const functionData = call && (call.function || call.function_call || call);
    const title = document.createElement("div");
    title.className = "tool-title";
    title.textContent =
      typeof call === "string" ? call : (functionData && functionData.name) || "（未知工具）";
    item.appendChild(title);
    if (typeof call !== "string") appendFolded(item, functionData && functionData.arguments, 120);
    parent.appendChild(item);
  });
}

// ------------------------------------------------------------ 检索

function normalizeSpace(value) {
  return String(value == null ? "" : value)
    .replace(/\s+/g, " ")
    .trim();
}

// 把一个文本节点压成"空白归一、大小写统一"的形态，并记住每个字符回到原文的区间。
// 这样换行处也能命中：原文 "foo\n  bar" 与检索词 "foo bar" 会落在同一处。
function findSpans(text, needle) {
  const folded = [];
  const starts = [];
  const ends = [];
  let offset = 0;
  let space = false;
  for (const char of text) {
    const start = offset;
    offset += char.length;
    if (/\s/.test(char)) {
      if (space) continue;
      space = true;
      folded.push(" ");
      starts.push(start);
      ends.push(offset);
      continue;
    }
    space = false;
    const lower = char.toLowerCase();
    for (let i = 0; i < lower.length; i++) {
      folded.push(lower[i]);
      starts.push(start);
      ends.push(offset);
    }
  }
  const haystack = folded.join("");
  const spans = [];
  if (!needle || !haystack) return spans;
  let from = 0;
  while (from <= haystack.length - needle.length) {
    const at = haystack.indexOf(needle, from);
    if (at < 0) break;
    spans.push([starts[at], ends[at + needle.length - 1]]);
    from = at + needle.length;
  }
  return spans;
}

function clearMarks() {
  const parents = new Set();
  for (const mark of matches) {
    if (!mark.parentNode) continue;
    const parent = mark.parentNode;
    parents.add(parent);
    mark.replaceWith(document.createTextNode(mark.textContent || ""));
  }
  parents.forEach((parent) => parent.normalize());
  matches = [];
  matchIndex = -1;
}

function buildMatches() {
  clearMarks();
  if (!state.query) return;
  const needle = normalizeSpace(state.query).toLowerCase();
  if (!needle) return;

  const walker = document.createTreeWalker(elements.detailPanel, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      if (parent.closest(SEARCH_SKIP)) return NodeFilter.FILTER_REJECT;
      if (!node.data) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  for (const node of nodes) {
    const spans = findSpans(node.data, needle);
    if (!spans.length) continue;
    const nodeMarks = [];
    for (let i = spans.length - 1; i >= 0; i--) {
      const [start, end] = spans[i];
      try {
        const part = node.splitText(start);
        part.splitText(end - start);
        const mark = document.createElement("mark");
        mark.className = "trace-match";
        part.replaceWith(mark);
        mark.appendChild(part);
        nodeMarks.push(mark);
      } catch (error) {
        console.warn("高亮失败", error);
        break;
      }
    }
    nodeMarks.reverse();
    for (const mark of nodeMarks) matches.push(mark);
  }
}

function updateSearchStatus() {
  const hasMatches = matches.length > 0;
  elements.searchPrev.disabled = !hasMatches;
  elements.searchNext.disabled = !hasMatches;
  const noResults = !!state.query && state.results !== null && state.results.length === 0;
  if (noResults) elements.searchInput.setAttribute("aria-invalid", "true");
  else elements.searchInput.removeAttribute("aria-invalid");

  if (!state.query) {
    setText(elements.searchStatus, "");
    return;
  }
  if (!state.activeEntry) {
    setText(elements.searchStatus, "未选中记录");
    return;
  }
  if (!hasMatches) {
    setText(elements.searchStatus, noResults ? "共 0 个匹配" : "当前记录无匹配");
    return;
  }
  setText(elements.searchStatus, `第 ${matchIndex + 1} / 共 ${matches.length} 个匹配`);
}

function selectMatch(index, scroll = true) {
  if (!matches.length) {
    matchIndex = -1;
    updateSearchStatus();
    return;
  }
  if (matchIndex >= 0 && matches[matchIndex]) matches[matchIndex].classList.remove("current");
  matchIndex = ((index % matches.length) + matches.length) % matches.length;
  const mark = matches[matchIndex];
  mark.classList.add("current");
  updateSearchStatus();
  if (!scroll) return;
  let ancestor = mark.parentElement;
  while (ancestor && ancestor !== elements.detailPanel) {
    if (ancestor.tagName === "DETAILS") ancestor.open = true;
    ancestor = ancestor.parentElement;
  }
  mark.scrollIntoView({ block: "center", inline: "nearest" });
}

function moveMatch(direction) {
  if (!state.query) return;
  if (!matches.length) buildMatches();
  if (!matches.length) {
    updateSearchStatus();
    return;
  }
  if (matchIndex < 0) {
    selectMatch(direction > 0 ? 0 : matches.length - 1);
    return;
  }
  selectMatch(matchIndex + direction);
}

// 当前详情还在命中结果里才重建高亮；保留序号用于自动刷新
function syncHighlight(options = {}) {
  const { scroll = false, start = 0, keepIndex = false, previous = -1 } = options;
  if (!state.query || !state.activeEntry) {
    clearMarks();
    updateSearchStatus();
    return;
  }
  if (state.results !== null && !state.results.some((entry) => entry.id === state.activeId)) {
    clearMarks();
    updateSearchStatus();
    return;
  }
  buildMatches();
  if (!matches.length) {
    updateSearchStatus();
    return;
  }
  const index = keepIndex && previous >= 0 ? Math.min(previous, matches.length - 1) : start;
  selectMatch(index, scroll);
}

function setSearchBusy(busy) {
  elements.searchForm.classList.toggle("is-busy", !!busy);
  elements.searchGo.disabled = !!busy;
  setText(elements.searchGo, busy ? "检索中…" : "检索");
}

async function applySearch() {
  const query = elements.searchInput.value.trim();
  if (!query) {
    resetSearch();
    return;
  }
  if (!state.day) {
    setNotice("这一天还没有记录，没法检索。");
    elements.searchInput.focus();
    return;
  }
  const token = ++searchToken;
  // 让还在飞的列表刷新作废，免得它拿旧检索词的结果覆盖这一次
  entriesGeneration += 1;
  setSearchBusy(true);
  let data;
  try {
    data = resultData(await callGet("trace/entries", { date: state.day, q: query }));
  } catch (error) {
    if (token !== searchToken) return;
    setSearchBusy(false);
    setNotice("检索失败：" + errorText(error));
    return;
  }
  if (token !== searchToken) return;
  setSearchBusy(false);

  state.query = query;
  state.results = Array.isArray(data.entries) ? data.entries : [];
  state.truncated = !!data.truncated;
  if (data.summary) state.summary = data.summary;

  renderEntries();
  syncHighlight({ scroll: true, start: 0 });
}

function resetSearch() {
  searchToken += 1;
  state.query = "";
  state.results = null;
  state.truncated = false;
  if (elements.searchInput.value) elements.searchInput.value = "";
  clearMarks();
  updateSearchStatus();
  renderEntries();
  setSearchBusy(false);
}

// ------------------------------------------------------------ 数据加载

async function loadDays() {
  const data = resultData(await callGet("trace/days"));
  const days = Array.isArray(data.days) ? data.days : [];
  elements.daySelect.replaceChildren();

  if (!days.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "（暂无记录）";
    elements.daySelect.appendChild(option);
    state.day = "";
    return;
  }

  for (const day of days) {
    const option = document.createElement("option");
    option.value = day.date;
    option.textContent = `${day.date} · ${day.count} 条`;
    option.title = `${day.date} 一共 ${day.count} 条，${formatSize(day.size)}`;
    elements.daySelect.appendChild(option);
  }
  if (!state.day || !days.some((day) => day.date === state.day)) {
    state.day = days[0].date;
  }
  elements.daySelect.value = state.day;
}

function fillModelSelect() {
  const names = [];
  for (const entry of state.entries) {
    const name = modelOf(entry);
    if (name && !names.includes(name)) names.push(name);
  }
  names.sort();
  if (state.model && !names.includes(state.model)) state.model = "";

  elements.modelSelect.replaceChildren();
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

async function loadEntries() {
  const generation = ++entriesGeneration;
  if (!state.day) {
    state.entries = [];
    state.results = null;
    state.summary = null;
    state.truncated = false;
    fillModelSelect();
    renderEntries();
    return;
  }

  const day = state.day;
  const query = state.query;
  const previous = state.entries.find((entry) => entry.id === state.activeId);
  let allData;
  let filtered = null;
  try {
    allData = resultData(await callGet("trace/entries", { date: day }));
    if (query) {
      filtered = resultData(
        await callGet("trace/entries", { date: day, q: query }),
      );
    }
  } catch (error) {
    if (generation === entriesGeneration) {
      setNotice("读取抓包失败：" + errorText(error));
    }
    return;
  }
  // 检索词在请求途中变了：这次结果作废，让新的检索自己去铺列表
  if (generation !== entriesGeneration || day !== state.day || state.query !== query) return;

  state.entries = Array.isArray(allData.entries) ? allData.entries : [];
  state.summary = allData.summary || null;
  if (query && filtered) {
    state.results = Array.isArray(filtered.entries) ? filtered.entries : [];
    state.truncated = !!filtered.truncated;
  } else if (!query) {
    state.results = null;
    state.truncated = false;
  }

  fillModelSelect();
  renderEntries();

  const current = state.entries.find((entry) => entry.id === state.activeId);
  if (current) {
    state.activeIndex = current.index;
    if (previous && (previous.revision !== current.revision || previous.status !== current.status)) {
      await openEntry(current.index, true);
    } else if (state.query) {
      syncHighlight({ scroll: false, keepIndex: true, previous: matchIndex });
    }
  } else if (state.activeId) {
    state.activeId = "";
    state.activeIndex = -1;
    resetDetail();
    renderEntries();
  }
}

async function refreshAll() {
  try {
    await loadDays();
    await loadEntries();
  } catch (error) {
    setNotice("刷新失败：" + errorText(error));
  }
}

// ------------------------------------------------------------ 底部文件区

function fileButton(text, className) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className || "btn";
  button.textContent = text;
  return button;
}

function renderFiles() {
  const total = files.reduce((sum, file) => sum + (Number(file.size) || 0), 0);
  setText(elements.filesSummary, files.length ? `${files.length} 个 · ${formatSize(total)}` : "暂无文件");
  setText(elements.filesDir, filesDir || "（路径未知）");
  elements.filesDir.title = filesDir || "";
  const openTitle = filesDir ? `打开抓包目录\n${filesDir}` : "打开抓包目录";
  elements.openDir.title = openTitle;
  elements.openDir.setAttribute("aria-label", openTitle);

  elements.filesList.replaceChildren();
  if (!files.length) {
    const empty = document.createElement("p");
    empty.className = "files-empty";
    empty.textContent = "抓包目录里还没有文件。";
    elements.filesList.appendChild(empty);
    return;
  }
  for (const file of files) elements.filesList.appendChild(createFileRow(file));
}

function createFileRow(file) {
  const row = document.createElement("div");
  row.className = "file-row";
  const name = document.createElement("span");
  name.className = "file-name";
  name.textContent = file.name;
  name.title = file.name;
  const size = document.createElement("span");
  size.className = "file-size";
  size.textContent = formatSize(file.size);
  const view = fileButton("查看");
  view.addEventListener("click", () => previewFile(file.name));
  const remove = fileButton("删除");
  remove.addEventListener("click", () => deleteFile(file.name, remove));
  row.append(name, size, view, remove);
  return row;
}

async function previewFile(name) {
  try {
    const data = resultData(await callGet("trace/file", { name }));
    elements.filesPreview.hidden = false;
    setText(elements.filesPreview, data.error ? data.error : data.text || "（空文件）");
    elements.filesPreview.scrollTop = 0;
  } catch (error) {
    elements.filesPreview.hidden = false;
    setText(elements.filesPreview, "读取失败：" + errorText(error));
  }
}

function deleteFile(name, button) {
  if (filesArmed !== name) {
    filesArmed = name;
    button.textContent = "确认删除";
    if (filesTimer) clearTimeout(filesTimer);
    filesTimer = setTimeout(() => {
      filesArmed = "";
      button.textContent = "删除";
    }, 5000);
    return;
  }
  filesArmed = "";
  if (filesTimer) clearTimeout(filesTimer);
  button.disabled = true;
  button.textContent = "删除中…";
  callPost("trace/file_delete", { name })
    .then((raw) => {
      const data = resultData(raw);
      if (data && data.ok === false) throw new Error(data.error || "删除失败");
      elements.filesPreview.hidden = true;
      setNotice(`已删除 ${name}`);
      loadFiles(true);
    })
    .catch((error) => {
      button.disabled = false;
      button.textContent = "删除";
      setNotice("删除失败：" + errorText(error));
    });
}

async function loadFiles(force = false) {
  if (!bridge) {
    setText(elements.filesSummary, "接口未就绪");
    return;
  }
  if (filesLoaded && !force) return;
  try {
    const data = resultData(await callGet("trace/files"));
    files = Array.isArray(data.files) ? data.files : [];
    filesDir = data.dir || "";
    filesLoaded = true;
    renderFiles();
  } catch (error) {
    filesLoaded = false;
    setText(elements.filesSummary, "读取失败");
    setText(elements.filesDir, errorText(error));
  }
}

// ------------------------------------------------------------ 交互

elements.refresh.addEventListener("click", () => {
  refreshAll();
});

// 终止键：跟/stop一个效果，只是不管会话，所有正在跑的请求一起停。
// 同样是"点一次变确认、再点一次才动手"的两步式，不依赖弹窗。
let stopArmed = false;
let stopTimer = null;

function disarmStop() {
  stopArmed = false;
  elements.stopAll.classList.remove("is-armed");
  if (stopTimer) {
    clearTimeout(stopTimer);
    stopTimer = null;
  }
}

elements.stopAll.addEventListener("click", async () => {
  if (!stopArmed) {
    stopArmed = true;
    elements.stopAll.classList.add("is-armed");
    stopTimer = setTimeout(disarmStop, 5000);
    setNotice("再点一次方块按钮，确认终止所有会话里正在跑的请求。");
    return;
  }
  disarmStop();
  try {
    const data = resultData(await callPost("trace/stop_all"));
    if (data.ok === false) {
      setNotice("终止失败：" + (data.error || "原因不明"));
      return;
    }
    const stopped = Number(data.stopped) || 0;
    const sessions = Number(data.sessions) || 0;
    if (!stopped) {
      setNotice("现在没有正在跑的请求。");
      return;
    }
    setNotice(`已对 ${sessions} 个会话的 ${stopped} 个任务发出停止请求。`);
  } catch (error) {
    setNotice("终止失败：" + errorText(error));
  }
});

elements.openDir.addEventListener("click", async () => {
  try {
    const data = resultData(await callPost("trace/open_dir"));
    if (data.opened) {
      setNotice("已请求打开抓包目录。");
      return;
    }
    openFilesPopover();
    await loadFiles();
    setNotice("系统没有自动打开文件夹。路径已放在「抓包文件」悬浮窗里，打开就能复制。");
  } catch (error) {
    setNotice("打开抓包目录失败：" + errorText(error));
  }
});

elements.noticeClose.addEventListener("click", () => setNotice(""));

elements.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  applySearch();
});

elements.searchPrev.addEventListener("click", () => moveMatch(-1));
elements.searchNext.addEventListener("click", () => moveMatch(1));

elements.searchInput.addEventListener("input", () => {
  if (!elements.searchInput.value.trim() && state.query) resetSearch();
});

elements.searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && (elements.searchInput.value || state.query)) {
    event.preventDefault();
    resetSearch();
  }
});

elements.daySelect.addEventListener("change", async () => {
  state.model = "";
  resetSearch();
  state.day = elements.daySelect.value;
  resetDetail();
  disarmClear();
  await refreshAll();
  await restartEvents();
});

elements.modelSelect.addEventListener("change", () => {
  state.model = elements.modelSelect.value;
  resetDetail();
  renderEntries();
});

// 面板给插件页开的沙箱里没有弹窗权限，window.confirm 会被静默拦掉，
// 所以清空改成"点一次变成确认、再点一次才删"的两步式，不依赖弹窗。
let clearArmed = false;
let clearTimer = null;

function disarmClear() {
  clearArmed = false;
  elements.clear.classList.remove("is-armed");
  if (clearTimer) {
    clearTimeout(clearTimer);
    clearTimer = null;
  }
}

elements.clear.addEventListener("click", async () => {
  if (!state.day) {
    setNotice("没有可以清空的日期。");
    return;
  }
  if (!clearArmed) {
    clearArmed = true;
    elements.clear.classList.add("is-armed");
    clearTimer = setTimeout(disarmClear, 5000);
    setNotice(`再点一次垃圾桶按钮，确认清空 ${state.day} 的全部记录。`);
    return;
  }
  disarmClear();
  const day = state.day;
  try {
    await callPost("trace/clear", { date: day });
  } catch (error) {
    setNotice("清空失败：" + errorText(error));
    return;
  }
  state.activeId = "";
  state.activeIndex = -1;
  resetDetail();
  resetSearch();
  await refreshAll();
  setNotice(`已清空 ${day} 的记录。`);
});

// 抓包文件改成右上角的小悬浮窗：点工具栏按钮开，点关闭或点外面收
function openFilesPopover() {
  if (!elements.filesPopover) return;
  elements.filesPopover.hidden = false;
}

function closeFilesPopover() {
  if (!elements.filesPopover) return;
  elements.filesPopover.hidden = true;
  elements.filesPreview.hidden = true;
}

elements.filesToggle.addEventListener("click", (event) => {
  event.stopPropagation();
  if (elements.filesPopover.hidden) {
    openFilesPopover();
    loadFiles();
  } else {
    closeFilesPopover();
  }
});

if (elements.filesClose) {
  elements.filesClose.addEventListener("click", (event) => {
    event.stopPropagation();
    closeFilesPopover();
  });
}

document.addEventListener("click", (event) => {
  if (!elements.filesPopover || elements.filesPopover.hidden) return;
  if (elements.filesPopover.contains(event.target)) return;
  if (elements.filesToggle && elements.filesToggle.contains(event.target)) return;
  closeFilesPopover();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.filesPopover && !elements.filesPopover.hidden) {
    closeFilesPopover();
  }
});

elements.filesRefresh.addEventListener("click", () => {
  elements.filesPreview.hidden = true;
  loadFiles(true);
});

// ------------------------------------------------------------ 自动刷新

async function syncChanges() {
  if (document.hidden) return;
  if (syncing) {
    syncAgain = true;
    return;
  }
  syncing = true;
  try {
    do {
      syncAgain = false;
      await refreshAll();
    } while (syncAgain && !document.hidden);
  } finally {
    syncing = false;
  }
}

async function restartEvents() {
  if (!bridge) {
    setNotice("页面桥接对象不可用，请从 AstrBot 面板打开本页。");
    return;
  }
  const current = ++sseGeneration;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  if (durationTimer) clearInterval(durationTimer);
  durationTimer = null;
  const old = subscription;
  subscription = null;
  if (old && bridge && typeof bridge.unsubscribeSSE === "function") {
    try {
      await bridge.unsubscribeSSE(old);
    } catch (error) {
      console.warn(error);
    }
  }
  if (document.hidden || current !== sseGeneration) return;

  durationTimer = setInterval(() => {
    if (state.activeEntry) renderDuration(state.activeEntry);
  }, 1000);
  try {
    await syncChanges();
  } catch (error) {
    console.warn("刷新失败", error);
  }
  if (document.hidden || current !== sseGeneration) return;
  try {
    if (!bridge || typeof bridge.subscribeSSE !== "function") {
      throw new Error("当前面板不支持事件推送");
    }
    const id = await bridge.subscribeSSE(
      "trace/events",
      {
        onMessage: () => {
          if (!document.hidden && current === sseGeneration) syncChanges();
        },
        onError: () => {
          if (!document.hidden && current === sseGeneration) {
            retryTimer = setTimeout(restartEvents, 2000);
          }
        },
      },
      { date: state.day },
    );
    if (document.hidden || current !== sseGeneration) {
      await bridge.unsubscribeSSE(id);
    } else {
      subscription = id;
    }
  } catch (error) {
    console.warn("抓包事件连接失败", error);
    if (!document.hidden && current === sseGeneration) {
      retryTimer = setTimeout(restartEvents, 2000);
    }
  }
}

function resetDetail() {
  state.activeIndex = -1;
  state.activeId = "";
  state.activeEntry = null;
  elements.detailHead.hidden = true;
  elements.detail.hidden = true;
  elements.detailEmpty.hidden = false;
  setText(elements.detailDuration, "");
  setText(elements.detailUsage, "");
  elements.detailMeta.replaceChildren();
  if (elements.detailHealth) {
    elements.detailHealth.hidden = true;
    elements.detailHealth.replaceChildren();
  }
  if (elements.detailSources) {
    elements.detailSources.hidden = true;
    elements.detailSources.replaceChildren();
  }
  if (elements.detailBadges) {
    elements.detailBadges.replaceChildren();
  }
  clearMarks();
  updateSearchStatus();
}

document.addEventListener("visibilitychange", restartEvents);
restartEvents();
loadFiles();
