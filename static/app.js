/* B 站视频下载器 —— 前端逻辑 */

const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  info: null,
  streams: null,
  mode: "video",
  fmt: "m4a",
  pollTimer: null,
  busy: false,
};

/* ------------------------------------------------------------ 基础工具 */

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opts);
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    /* 忽略空响应 */
  }
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`);
  return data;
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtCount(n) {
  n = Number(n) || 0;
  if (n >= 1e8) return `${(n / 1e8).toFixed(1)}亿`;
  if (n >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return String(n);
}

function fmtSize(bytes) {
  if (!bytes || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)}${units[i]}`;
}

function showAlert(msg, type = "error") {
  const el = $("#alert");
  el.textContent = msg;
  el.className = `alert${type === "info" ? " info" : ""}`;
  el.title = "点击关闭";
  el.onclick = () => el.classList.add("hidden");
}

function hideAlert() {
  $("#alert").classList.add("hidden");
}

/* ------------------------------------------------------------ 自绘下拉
   不用原生 <select>：WebView2 的原生下拉弹窗在无边框窗口里不会正常显示，
   而且系统菜单是浅色的，和整套玻璃风格也不搭。 */
class Dropdown {
  constructor(root) {
    this.root = root;
    this.items = [];
    this._value = null;
    this.panel = null;
    this.isOpen = false;
    this.activeIndex = -1;

    this.root.classList.add("dd");
    this.root.tabIndex = 0;
    this.root.setAttribute("role", "combobox");
    this.root.setAttribute("aria-haspopup", "listbox");
    this.root.innerHTML = "";

    this.labelEl = document.createElement("span");
    this.labelEl.className = "dd-label";
    this.root.appendChild(this.labelEl);

    const arrow = document.createElement("i");
    arrow.className = "dd-arrow";
    this.root.appendChild(arrow);

    this.root.addEventListener("mousedown", (e) => {
      e.preventDefault();
      this.toggle();
    });
    this.root.addEventListener("keydown", (e) => this.onKey(e));
  }

  get value() {
    return this._value;
  }

  set value(v) {
    const item = this.items.find((it) => String(it.value) === String(v) && !it.disabled);
    if (!item) return;
    this._value = String(item.value);
    this.renderLabel();
  }

  setOptions(items) {
    this.items = items;
    const valid = items.some((it) => String(it.value) === this._value && !it.disabled);
    if (!valid) {
      const first = items.find((it) => !it.disabled);
      this._value = first ? String(first.value) : null;
    }
    this.renderLabel();
    if (this.panel) this.renderPanel();
  }

  currentItem() {
    return this.items.find((it) => String(it.value) === this._value) || null;
  }

  renderLabel() {
    const item = this.currentItem();
    this.labelEl.textContent = item ? item.label : "—";
    this.root.classList.toggle("dd-empty", !item);
    // 不在触发器上挂 title：原生 tooltip 会压住刚弹出的面板
    this.root.removeAttribute("title");
  }

  renderPanel() {
    if (!this.panel) return;
    this.panel.innerHTML = "";
    this.items.forEach((it, i) => {
      const opt = document.createElement("div");
      opt.className = "dd-opt";
      opt.dataset.index = String(i);
      opt.textContent = it.label;
      if (it.disabled) opt.classList.add("disabled");
      if (String(it.value) === this._value) opt.classList.add("active");
      this.panel.appendChild(opt);
    });
  }

  toggle() {
    if (this.isOpen) this.close();
    else this.open();
  }

  open() {
    if (this.isOpen || !this.items.length) return;

    const panel = document.createElement("div");
    panel.className = "dd-panel glass";
    document.body.appendChild(panel);
    this.panel = panel;
    this.renderPanel();

    const rect = this.root.getBoundingClientRect();
    panel.style.minWidth = `${Math.round(rect.width)}px`;
    panel.style.left = "0px";
    panel.style.top = "0px";

    // 预渲染后量高度，决定往下弹还是往上弹
    const ph = panel.offsetHeight;
    const below = window.innerHeight - rect.bottom;
    const flip = below < ph + 14 && rect.top > ph + 14;
    const top = flip ? rect.top - ph - 6 : rect.bottom + 6;
    const left = Math.min(rect.left, window.innerWidth - panel.offsetWidth - 10);

    panel.style.left = `${Math.max(8, left)}px`;
    panel.style.top = `${Math.max(8, top)}px`;

    const activeIdx = this.items.findIndex((it) => String(it.value) === this._value);
    this.activeIndex = activeIdx >= 0 ? activeIdx : 0;
    this.scrollActiveIntoView();

    panel.addEventListener("mousedown", (e) => {
      const opt = e.target.closest(".dd-opt");
      if (!opt) return;
      e.preventDefault();
      this.pick(parseInt(opt.dataset.index, 10));
    });

    this.root.classList.add("dd-open");
    this.isOpen = true;

    // 延后注册：避免同一次 mousedown 立刻把面板关掉
    setTimeout(() => {
      if (!this.isOpen) return;
      document.addEventListener("mousedown", this.onDocDown);
      document.addEventListener("keydown", this.onDocKey);
      window.addEventListener("resize", this.close);
      document.querySelector(".app")?.addEventListener("scroll", this.close, true);
    }, 0);
  }

  onDocDown = (e) => {
    if (this.root.contains(e.target) || this.panel?.contains(e.target)) return;
    this.close();
  };

  onDocKey = (e) => {
    if (e.key === "Escape") {
      this.close();
      this.root.focus();
    }
  };

  close = () => {
    if (!this.isOpen) return;
    this.isOpen = false;
    this.root.classList.remove("dd-open");
    this.panel?.remove();
    this.panel = null;
    document.removeEventListener("mousedown", this.onDocDown);
    document.removeEventListener("keydown", this.onDocKey);
    window.removeEventListener("resize", this.close);
    document.querySelector(".app")?.removeEventListener("scroll", this.close, true);
  };

  pick(index) {
    const item = this.items[index];
    if (!item || item.disabled) return;
    const changed = String(item.value) !== String(this._value);
    this._value = String(item.value);
    this.renderLabel();
    this.close();
    this.root.focus();
    if (changed) this.root.dispatchEvent(new Event("change", { bubbles: true }));
  }

  scrollActiveIntoView() {
    const opts = this.panel?.querySelectorAll(".dd-opt");
    opts?.[this.activeIndex]?.scrollIntoView({ block: "nearest" });
  }

  move(delta) {
    if (!this.items.length) return;
    let i = this.activeIndex;
    for (let n = 0; n < this.items.length; n++) {
      i = (i + delta + this.items.length) % this.items.length;
      if (!this.items[i].disabled) break;
    }
    this.activeIndex = i;
    this.panel?.querySelectorAll(".dd-opt").forEach((el, idx) => {
      el.classList.toggle("hover", idx === i);
    });
    this.scrollActiveIntoView();
  }

  onKey(e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (this.isOpen) this.pick(this.activeIndex);
      else this.open();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      if (this.isOpen) this.move(1);
      else this.open();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (this.isOpen) this.move(-1);
    }
  }
}

const dropdowns = {};

function initDropdowns() {
  ["pageSelect", "qualitySelect", "codecSelect", "audioSelect"].forEach((id) => {
    const root = document.getElementById(id);
    if (root) dropdowns[id] = new Dropdown(root);
  });
}

/* ------------------------------------------------------------ 解析流程 */

/* ------------------------------------------------------------ 解析进度
   解析是「一个请求干完所有事」，前端拿不到中途进度。
   所以后端把阶段写进一个全局状态，前端在请求进行时轮询它；
   显示值再用缓动追过去，阶段跳变时才不会一格一格地弹。 */

const pp = {
  seq: 0,      // 每轮解析自增，用来丢弃上一轮迟到的回调
  target: 0,   // 后端上报的真实进度
  shown: 0,    // 缓动后的显示进度
  raf: 0,
  timer: 0,
};

function ppPaint() {
  $("#ppFill").style.width = `${pp.shown.toFixed(1)}%`;
  $("#ppPercent").textContent = `${Math.round(pp.shown)}%`;
}

function ppStartAnim() {
  if (pp.raf) return;
  const step = () => {
    // 目标值之上留一小段「正在处理」的余量，但绝不提前冲到 100
    const goal = pp.target >= 100 ? 100 : Math.min(pp.target + 12, 95);
    const diff = goal - pp.shown;
    if (Math.abs(diff) < 0.12) {
      pp.shown = goal;
      ppPaint();
      pp.raf = 0;
      return;
    }
    // 收尾阶段追得快一点，别让「解析完成」拖太久
    pp.shown += diff * (pp.target >= 100 ? 0.22 : 0.14);
    ppPaint();
    pp.raf = requestAnimationFrame(step);
  };
  pp.raf = requestAnimationFrame(step);
}

async function ppPoll(seq) {
  try {
    const data = await api("/api/parse/progress");
    if (seq !== pp.seq) return; // 已经开了新一轮
    // 只认「正在解析」的快照：上一轮结束时留下的 100% 会在新一轮请求刚发出、
    // 后端还没重置状态的瞬间被读到，直接用会让进度条瞬间冲满
    if (!data.active) return;
    if (typeof data.percent === "number" && data.percent > pp.target) {
      pp.target = data.percent;
      ppStartAnim();
    }
    if (data.stage && $("#ppStage").textContent !== data.stage) {
      $("#ppStage").textContent = data.stage;
    }
  } catch (_) {
    /* 进度拿不到不影响解析本身 */
  }
}

function ppBegin() {
  pp.seq += 1;
  pp.target = 0;
  pp.shown = 0;
  if (pp.raf) {
    cancelAnimationFrame(pp.raf);
    pp.raf = 0;
  }
  clearInterval(pp.timer);

  const box = $("#parseProgress");
  box.classList.remove("hidden", "done", "failed", "leaving");
  $("#ppStage").textContent = "正在连接 B 站";
  ppPaint();
  ppStartAnim();

  const seq = pp.seq;
  pp.timer = setInterval(() => ppPoll(seq), 180);
  return seq;
}

function ppHide(seq, after) {
  if (seq !== pp.seq) return; // 已开新一轮，别动新的进度条
  const box = $("#parseProgress");
  box.classList.add("leaving");
  setTimeout(() => {
    if (seq !== pp.seq) return;
    box.classList.add("hidden");
    box.classList.remove("leaving", "done", "failed");
    if (after) after();
  }, 260);
}

function ppFinish(ok, message, after) {
  clearInterval(pp.timer);
  pp.timer = 0;
  const seq = pp.seq;
  const box = $("#parseProgress");

  if (!ok) {
    // 失败时不要继续往前跑，停住转红，让用户对上方的错误提示有反应时间
    if (pp.raf) {
      cancelAnimationFrame(pp.raf);
      pp.raf = 0;
    }
    box.classList.add("failed");
    $("#ppStage").textContent = message || "解析失败";
    setTimeout(() => ppHide(seq, after), 1200);
    return;
  }

  pp.target = 100;
  ppStartAnim();

  const waitDone = () => {
    if (seq !== pp.seq) return;
    if (pp.shown < 99.4) {
      setTimeout(waitDone, 60);
      return;
    }
    pp.shown = 100;
    ppPaint();
    // 文案等到条子真正走满再切成「完成」，否则会出现「已完成」停在 60% 的怪象
    $("#ppStage").textContent = message || "解析完成";
    box.classList.add("done");
    setTimeout(() => ppHide(seq, after), 340);
  };
  waitDone();
}

async function doParse() {
  const raw = $("#urlInput").value.trim();
  if (!raw || state.busy) return;

  state.busy = true;
  const btn = $("#btnParse");
  btn.disabled = true;
  btn.textContent = "解析中";
  hideAlert();
  ppBegin();

  // 接口偶尔抖动，超过 8 秒在进度条上说明一下，避免用户以为卡死
  const slowTimer = setTimeout(() => {
    if (!state.busy) return;
    $("#ppStage").textContent = "网络较慢，正在重试…";
  }, 8000);

  let ok = false;
  try {
    const data = await api("/api/parse", { url: raw });
    state.info = data.info;
    state.streams = data.streams;
    ok = true;
  } catch (err) {
    $("#result").classList.add("hidden");
    showAlert(err.message);
    ppFinish(false, "解析失败");
  } finally {
    clearTimeout(slowTimer);
    state.busy = false;
    btn.disabled = false;
    btn.textContent = "解析";
    if (ok) {
      // 等进度条真正收完再渲染结果：它占一条高度，若同时出现会让下面的内容
      // 先被顶下去、再随进度条消失弹回来
      ppFinish(true, "解析完成", () => {
        try {
          renderResult();
          $("#result").scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch (e) {
          showAlert(`结果渲染失败：${e.message}`);
        }
      });
    }
  }
}

function renderResult() {
  const info = state.info;
  const streams = state.streams;
  $("#result").classList.remove("hidden");

  $("#cover").src = info.cover || "";
  $("#duration").textContent = fmtDuration(info.duration);
  $("#videoTitle").textContent = info.title;

  const meta = $("#metaRow");
  meta.innerHTML = "";
  const bits = [
    ["UP", info.owner?.name || "未知"],
    ["播放", fmtCount(info.stat?.view)],
    ["弹幕", fmtCount(info.stat?.danmaku)],
    ["收藏", fmtCount(info.stat?.favorite)],
  ];
  bits.forEach(([k, v], i) => {
    const span = document.createElement("span");
    if (i === 0) span.className = "up";
    span.textContent = `${k} ${v}`;
    meta.appendChild(span);
  });

  // 分 P
  const pageRow = $("#pageRow");
  if (info.pages.length > 1) {
    pageRow.classList.remove("hidden");
    dropdowns.pageSelect.setOptions(
      info.pages.map((p) => ({ value: String(p.cid), label: `P${p.page} ${p.part}` }))
    );
    dropdowns.pageSelect.value = String(info.current_cid);
  } else {
    pageRow.classList.add("hidden");
  }

  renderStreams();
}

function renderStreams() {
  const streams = state.streams;
  const info = state.info;
  if (!streams) return;

  const duration = currentPage()?.duration || info.duration || 0;

  // 清晰度
  const items = streams.qualities.map((q) => {
    const est = q.bandwidth ? fmtSize((q.bandwidth * duration) / 8) : "";
    const kbps = q.bandwidth ? Math.round(q.bandwidth / 1000) : 0;
    return {
      value: String(q.qn),
      label: `${q.label}${est ? ` · ${est}` : ""}`,
      hint: `${q.label}${est ? `，预计 ${est}` : ""}${kbps ? `（${kbps}kbps）` : ""}`,
    };
  });

  // 需要登录才能解锁的清晰度以禁用项展示，顺便说明原因
  (streams.locked || []).forEach((q) => {
    items.push({
      value: `locked-${q.qn}`,
      label: `${q.label}（需登录）`,
      hint: "登录后才能下载，点右上角「未登录」即可登录",
      disabled: true,
    });
  });
  dropdowns.qualitySelect.setOptions(items);

  renderCodecs();
  renderAudios();

  const tip = $("#lockedTip");
  const lockedLabels = (streams.locked || []).map((q) => q.label).join("、");
  if (lockedLabels) {
    tip.classList.remove("hidden");
    const best = streams.qualities[0]?.label || "低清晰度";
    tip.textContent = streams.logged_in
      ? `当前账号无法获取：${lockedLabels}（可能需要大会员，或该清晰度需购买）`
      : `未登录仅能下载到 ${best}，登录后可解锁 ${lockedLabels}`;
  } else {
    tip.classList.add("hidden");
  }

  $("#btnDownloadAll").classList.toggle("hidden", info.pages.length <= 1);
  $("#btnDownloadAll").textContent = `下载全部 ${info.pages.length} P`;

  applyMode();
}

function renderCodecs() {
  const q = selectedQuality();
  $("#codecField").classList.toggle("hidden", !q || q.codecs.length <= 1);
  if (!q) {
    dropdowns.codecSelect.setOptions([]);
    return;
  }

  const names = { avc1: "H.264 / AVC", hevc: "H.265 / HEVC", av1: "AV1" };
  dropdowns.codecSelect.setOptions(
    q.codecs.map((c) => ({ value: c, label: names[c] || c.toUpperCase() }))
  );
}

function renderAudios() {
  const streams = state.streams;
  const audios = streams?.audios || [];
  $("#audioField").classList.toggle("hidden", audios.length === 0);

  dropdowns.audioSelect.setOptions(
    audios.map((a) => {
      const kbps = a.bandwidth ? Math.round(a.bandwidth / 1000) : 0;
      return {
        value: String(a.id),
        label: a.label,
        hint: kbps ? `${a.label} · ${kbps}kbps` : a.label,
      };
    })
  );
}

function currentPage() {
  const info = state.info;
  if (!info) return null;
  return info.pages.find((p) => p.cid === info.current_cid) || info.pages[0];
}

function selectedQuality() {
  const qn = parseInt(dropdowns.qualitySelect.value, 10);
  const list = state.streams?.qualities || [];
  return list.find((q) => q.qn === qn) || list[0] || null;
}

/* ------------------------------------------------------------ 切换处理 */

$("#parseForm").addEventListener("submit", (e) => {
  e.preventDefault();
  doParse();
});

$("#pageSelect").addEventListener("change", async () => {
  const cid = parseInt(dropdowns.pageSelect.value, 10);
  const info = state.info;
  info.current_cid = cid;
  const page = info.pages.find((p) => p.cid === cid);
  if (page) info.current_page = page.page;

  hideAlert();
  try {
    const data = await api("/api/streams", { bvid: info.bvid, cid });
    state.streams = data.streams;
    renderStreams();
  } catch (err) {
    showAlert(err.message);
  }
});

$("#qualitySelect").addEventListener("change", renderCodecs);

$("#modeSeg").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...$("#modeSeg").children].forEach((b) => b.classList.toggle("active", b === btn));
  state.mode = btn.dataset.mode;
  state.fmt = btn.dataset.fmt || "m4a";
  applyMode();
});

function applyMode() {
  const isAudio = state.mode === "audio";
  // 音频模式下只剩音轨一个控件，单独收窄，别拉成整行
  document.querySelector(".controls").classList.toggle("single", isAudio);
  // 音频模式下清晰度与视频编码都无意义
  $("#qualitySelect").closest(".field").classList.toggle("hidden", isAudio);
  $("#codecField").classList.toggle("hidden", isAudio || (selectedQuality()?.codecs?.length || 0) <= 1);
  $("#btnDownloadAll").classList.toggle("hidden", isAudio || (state.info?.pages?.length || 0) <= 1);
}

/* ------------------------------------------------------------ 下载 */

$("#btnDownload").addEventListener("click", async () => {
  const btn = $("#btnDownload");
  if (!state.info || btn.disabled) return;

  const q = selectedQuality();
  if (!q) {
    showAlert("没有可用的清晰度");
    return;
  }

  const page = currentPage();
  const suffix = state.info.pages.length > 1 ? ` - P${page.page} ${page.part}` : "";
  const title = state.info.title + suffix;

  btn.disabled = true;
  const original = btn.innerHTML;
  btn.textContent = "创建任务中…";
  hideAlert();

  try {
    await api("/api/download", {
      bvid: state.info.bvid,
      cid: state.info.current_cid,
      qn: q.qn,
      codec: dropdowns.codecSelect.value || q.codecs[0],
      audio_id: parseInt(dropdowns.audioSelect.value, 10) || null,
      title,
      mode: state.mode,
      audio_format: state.fmt,
    });
    await refreshTasks();
    startPoll();
  } catch (err) {
    showAlert(err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
});

/* ------------------------------------------------------------ 批量下载 */

$("#btnDownloadAll").addEventListener("click", async () => {
  const info = state.info;
  const q = selectedQuality();
  if (!info || !q) return;

  const btn = $("#btnDownloadAll");
  const original = btn.textContent;
  const pages = info.pages;
  btn.disabled = true;
  hideAlert();

  let failed = 0;
  try {
    for (let i = 0; i < pages.length; i++) {
      const p = pages[i];
      btn.textContent = `创建中 ${i + 1}/${pages.length}`;
      try {
        await api("/api/download", {
          bvid: info.bvid,
          cid: p.cid,
          qn: q.qn,
          codec: dropdowns.codecSelect.value || q.codecs[0],
          audio_id: parseInt(dropdowns.audioSelect.value, 10) || null,
          title: `${info.title} - P${p.page} ${p.part}`,
          mode: "video",
          audio_format: "m4a",
        });
      } catch (err) {
        failed++;
      }
    }
    await refreshTasks();
    startPoll();
    if (failed) showAlert(`${failed} 个分集创建失败，请检查后重试`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

/* ------------------------------------------------------------ 任务列表 */

function createTaskNode(task) {
  const node = document.createElement("div");
  node.className = "task glass";
  node.dataset.id = task.id;
  node.innerHTML = `
    <div class="task-top">
      <span class="task-name"></span>
      <span class="task-quality"></span>
      <span class="task-status"></span>
    </div>
    <div class="track"><div class="bar"></div></div>
    <div class="task-err hidden"></div>
    <div class="task-actions hidden"></div>`;
  return node;
}

function updateTaskNode(node, task) {
  node.querySelector(".task-name").textContent = task.filename;
  node.querySelector(".task-quality").textContent = task.quality;

  const statusEl = node.querySelector(".task-status");
  const bar = node.querySelector(".bar");
  const errEl = node.querySelector(".task-err");
  const actions = node.querySelector(".task-actions");

  node.classList.toggle("done", task.status === "done");  node.classList.toggle("error", task.status === "error");
  statusEl.className = `task-status ${task.status}`;

  if (task.status === "downloading") {
    const speed = task.speed ? `${fmtSize(task.speed)}/s` : "…";
    const left = task.total ? fmtSize(Math.max(0, task.total - task.downloaded)) : "";
    statusEl.textContent = `${task.progress.toFixed(0)}% · ${speed}${left ? ` · 剩 ${left}` : ""}`;
    bar.style.width = `${Math.max(1.5, task.progress)}%`;
  } else if (task.status === "merging") {
    statusEl.textContent = "合并中…";
    bar.style.width = "100%";
  } else if (task.status === "queued") {
    statusEl.textContent = "排队中";
    bar.style.width = "2%";
  } else if (task.status === "done") {
    statusEl.textContent = `已完成 · ${fmtSize(task.total) || ""}`.trim();
    bar.style.width = "100%";
  } else if (task.status === "error") {
    statusEl.textContent = "失败";
    bar.style.width = "100%";
  } else {
    statusEl.textContent = "已取消";
    bar.style.width = "100%";
  }

  errEl.classList.toggle("hidden", !task.error);
  errEl.textContent = task.error || "";

  // 操作按钮按状态重建（内容简单，重建成本低）
  const want =
    task.status === "done"
      ? "done"
      : task.status === "error" || task.status === "canceled"
        ? "retry"
        : task.status === "queued" || task.status === "downloading"
          ? "cancel"
          : "";
  if (actions.dataset.kind !== want) {
    actions.dataset.kind = want;
    actions.innerHTML = "";
    actions.classList.toggle("hidden", !want);

    if (want === "done") {
      actions.innerHTML = `
        <button class="save glass" data-act="save">保存到本地</button>
        <button class="glass" data-act="folder">打开所在文件夹</button>`;
    } else if (want === "cancel") {
      actions.innerHTML = `<button class="glass" data-act="cancel">取消下载</button>`;
    }
  }
}

function renderTasks(tasks) {
  const list = $("#taskList");
  $("#taskEmpty").classList.toggle("hidden", tasks.length > 0);

  const seen = new Set();
  tasks.forEach((task) => {
    seen.add(task.id);
    let node = list.querySelector(`.task[data-id="${task.id}"]`);
    if (!node) {
      node = createTaskNode(task);
      list.appendChild(node);
    }
    updateTaskNode(node, task);
  });

  [...list.children].forEach((n) => {
    if (!seen.has(n.dataset.id)) n.remove();
  });
}

$("#taskList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest(".task").dataset.id;
  const act = btn.dataset.act;

  if (act === "save") {
    if (isDesktop()) {
      // 窗口版走原生「另存为」对话框，比浏览器下载更符合桌面习惯
      const res = await window.pywebview.api.save_task(id);
      if (res && res.ok) showAlert(`已保存到 ${res.path}`, "info");
      else if (res && !res.canceled) showAlert(res.detail || "保存失败");
    } else {
      window.location.href = `/api/tasks/${id}/file`;
    }
  } else if (act === "cancel") {
    await api(`/api/tasks/${id}/cancel`, {});
    await refreshTasks();
  } else if (act === "folder") {
    await api("/api/open-folder", {});
  }
});

async function refreshTasks() {
  try {
    const data = await api("/api/tasks");
    renderTasks(data.tasks);
    const active = data.tasks.some((t) => ["queued", "downloading", "merging"].includes(t.status));
    if (active) startPoll();
    else stopPoll();
  } catch (_) {
    /* 轮询失败不打扰用户 */
  }
}

function startPoll() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(refreshTasks, 700);
}

function stopPoll() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

$("#btnFolder").addEventListener("click", () => api("/api/open-folder", {}));

/* ------------------------------------------------------------ 设置 */

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    const chips = $("#chips");
    chips.innerHTML = "";

    const addChip = (text, cls, onClick) => {
      const span = document.createElement("span");
      span.className = `chip ${cls || ""}${onClick ? " clickable" : ""}`;
      span.innerHTML = '<i class="dot"></i>';
      span.appendChild(document.createTextNode(text));
      if (onClick) {
        span.onclick = onClick;
        span.title = "点击登录 B 站，自动获取 Cookie";
      }
      chips.appendChild(span);
      return span;
    };

    if (s.logged_in) addChip("已登录", "ok");
    else addChip("未登录 · 点击登录", "warn", startLogin);
    addChip(s.ffmpeg ? "ffmpeg 就绪" : "缺少 ffmpeg", s.ffmpeg ? "ok" : "warn");

    $("#loginState").textContent = s.logged_in ? "已登录" : "未登录";
    $("#loginState").style.color = s.logged_in ? "var(--green)" : "var(--muted)";

    $("#threadsInput").value = s.threads;
    $("#proxyInput").checked = !!s.use_proxy;
    $("#cookieState").textContent = s.logged_in ? "已登录" : s.has_cookie ? "Cookie 已保存但未生效" : "未设置";
    $("#cookieState").style.color = s.logged_in ? "var(--green)" : "var(--muted)";

    const env = $("#envInfo");
    env.innerHTML = "";
    const rows = [
      ["下载目录", s.download_dir],
      ["ffmpeg", s.ffmpeg ? "可用" : "未找到（无法合并音视频）"],
      ["登录状态", s.logged_in ? "已登录" : "未登录"],
    ];
    rows.forEach(([k, v]) => {
      const div = document.createElement("div");
      div.innerHTML = `${k}：<b></b>`;
      div.querySelector("b").textContent = v;
      env.appendChild(div);
    });
  } catch (_) {
    /* 静默失败 */
  }
}

/* ------------------------------------------------------------ 一键登录 */

let loggingIn = false;

async function startLogin() {
  if (loggingIn) return;
  if (!isDesktop()) {
    showAlert("自动登录只在桌面窗口版可用；网页版请手动填入 SESSDATA", "info");
    return;
  }

  loggingIn = true;
  const chip = document.querySelector(".chip.clickable");
  if (chip && chip.lastChild) chip.lastChild.nodeValue = "等待登录…";
  const btn = $("#btnLogin");
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  showAlert("已打开 B 站登录窗口，扫码或账号登录后会记住 Cookie", "info");

  try {
    const res = await window.pywebview.api.login_bilibili();
    if (res?.ok) {
      showAlert(res.already ? "已经是登录状态了" : "登录成功，高清画质已解锁", "info");
      await refreshAfterLogin();
    } else if (res?.canceled) {
      hideAlert();
    } else {
      showAlert(res?.detail || "登录未完成，请重试");
    }
  } catch (err) {
    showAlert(err?.message || "登录窗口打开失败");
  } finally {
    loggingIn = false;
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
    await loadSettings();
  }
}

/* ------------------------------------------------------------ 登录后刷新码率
   登录前拿到的是未登录的码率表（最高 480P），登录后必须重新请求一次，
   否则用户还得手动再点一次「解析」才能看到 1080P。 */

let loginRefreshAt = 0;

async function refreshAfterLogin(notify = true) {
  if (!state.info) return;
  // Python 侧的 onLoginStateChanged 与接口返回会各触发一次，这里做个去重
  if (Date.now() - loginRefreshAt < 1500) return;
  loginRefreshAt = Date.now();

  try {
    const data = await api("/api/streams", {
      bvid: state.info.bvid,
      cid: state.info.current_cid,
    });
    state.streams = data.streams;
    renderStreams();

    // 登录的目的就是解锁高画质，刷新后直接选到当前可用的最高档
    const best = [...state.streams.qualities].sort((a, b) => b.qn - a.qn)[0];
    if (best) {
      dropdowns.qualitySelect.value = String(best.qn);
      renderCodecs();
    }
    if (notify) {
      showAlert(best ? `已按登录状态刷新码率，当前最高可用：${best.label}` : "已刷新码率选项", "info");
    }
  } catch (err) {
    if (notify) showAlert(`刷新码率失败：${err.message}`);
  }
}

// Python 侧登录成功后会调用它刷新界面
window.onLoginStateChanged = () => {
  loadSettings();
  refreshAfterLogin();
};

$("#btnLogin").addEventListener("click", startLogin);

$("#btnSettings").addEventListener("click", () => {
  $("#settingsModal").classList.remove("hidden");
  loadSettings();
});

$("#settingsModal").addEventListener("click", (e) => {
  // 用 closest：点在 × 图标的 svg/path 上时，事件目标不是按钮本身
  if (e.target.closest("[data-close]")) $("#settingsModal").classList.add("hidden");
});

$("#btnSaveSettings").addEventListener("click", async () => {
  const btn = $("#btnSaveSettings");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    const data = await api("/api/settings", {
      sessdata: $("#sessdataInput").value.trim(),
      threads: parseInt($("#threadsInput").value, 10) || 8,
      use_proxy: $("#proxyInput").checked,
    });
    await loadSettings();
    if (data.logged_in) {
      showAlert("Cookie 校验通过，已解锁高清画质", "info");
      await refreshAfterLogin();
    } else if ($("#sessdataInput").value.trim()) {
      showAlert("Cookie 已保存，但服务端校验为未登录，请确认 SESSDATA 是否失效");
    } else {
      showAlert("设置已保存", "info");
    }
    $("#settingsModal").classList.add("hidden");
  } catch (err) {
    showAlert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "保存设置";
  }
});

/* ------------------------------------------------------------ 粘贴即解析 */

$("#urlInput").addEventListener("paste", (e) => {
  const text = (e.clipboardData || window.clipboardData).getData("text");
  if (!text) return;
  if (/bilibili\.com|b23\.tv|bili2233|BV[0-9A-Za-z]{10}|av\d{5,}/i.test(text)) {
    setTimeout(() => doParse(), 60);
  }
});

/* ------------------------------------------------------------ 桌面窗口 */

function isDesktop() {
  return window.pywebview !== undefined;
}

// 窗口控件（自绘标题栏）
document.querySelectorAll("[data-win]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!isDesktop()) return;
    const act = btn.dataset.win;
    if (act === "min") window.pywebview.api.minimize();
    else if (act === "close") window.pywebview.api.close();
    else if (act === "max") {
      const maxed = await window.pywebview.api.toggle_maximize();
      document.documentElement.classList.toggle("maxed", !!maxed);
    }
  });
});

// 双击标题栏空白处 = 最大化 / 还原（和系统窗口的习惯一致）
document.querySelector(".titlebar-drag")?.addEventListener("dblclick", async () => {
  if (!isDesktop()) return;
  const maxed = await window.pywebview.api.toggle_maximize();
  document.documentElement.classList.toggle("maxed", !!maxed);
});

/* ------------------------------------------------------------ 液态玻璃高光 */

// 指针在玻璃面移动时，把坐标写进 CSS 变量，::after 的高光跟着走
const GLASS_SELECTOR = ".glass, .glass-strong";

// 移出时清掉坐标，让高光回到默认位置（配合 CSS 的 opacity 过渡淡出），
// 否则光斑会停在鼠标最后停留的地方，看着像卡住了
function resetSheen(el) {
  if (!el || !el.style) return;
  el.style.removeProperty("--mx");
  el.style.removeProperty("--my");
}

// 高亮状态用类名显式维护，不依赖 CSS 的 :hover ——
// 在 WebView2 里 :not(:hover) 对伪元素的表现不稳定（会反过来匹配）
function setSheen(el, on, related) {
  if (!el || el.nodeType !== 1 || !el.matches) return;
  if (on) {
    el.classList.add("sheen-on");
    return;
  }
  // 指针只是移到了子元素里，不算离开
  if (related instanceof Node && el.contains(related)) return;
  el.classList.remove("sheen-on");
  resetSheen(el);
}

function walkGlassChain(start, on, related) {
  let node = start;
  while (node && node.nodeType === 1) {
    if (node.matches(GLASS_SELECTOR)) setSheen(node, on, related);
    node = node.parentElement;
  }
}

document.addEventListener(
  "pointerover",
  (e) => {
    const el = e.target instanceof Element ? e.target.closest(GLASS_SELECTOR) : null;
    if (el) walkGlassChain(el, true);
  },
  { passive: true }
);

document.addEventListener(
  "pointerout",
  (e) => {
    const el = e.target instanceof Element ? e.target.closest(GLASS_SELECTOR) : null;
    if (el) walkGlassChain(el, false, e.relatedTarget);
  },
  { passive: true }
);

// 切到别的应用 / 指针离开窗口时全量复位，避免残留旧光斑
window.addEventListener("blur", () => {
  document.querySelectorAll(GLASS_SELECTOR).forEach((el) => {
    el.classList.remove("sheen-on");
    resetSheen(el);
  });
});
let sheenFrame = 0;
let sheenPoint = null;

document.addEventListener(
  "pointermove",
  (e) => {
    const el = e.target instanceof Element ? e.target.closest(GLASS_SELECTOR) : null;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    sheenPoint = { el, x: e.clientX - rect.left, y: e.clientY - rect.top };
    if (sheenFrame) return;
    sheenFrame = requestAnimationFrame(() => {
      sheenFrame = 0;
      if (!sheenPoint) return;
      sheenPoint.el.style.setProperty("--mx", `${sheenPoint.x}px`);
      sheenPoint.el.style.setProperty("--my", `${sheenPoint.y}px`);
    });
  },
  { passive: true }
);

/* ------------------------------------------------------------ 启动 */

initDropdowns();
loadSettings();
refreshTasks();
