/* 视频字幕 Agent 前端逻辑 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    config: null,
    videos: [],
    current: null,
    cues: [],
    activeIndex: -1,
    dirty: false,
    jobId: null,
    pollTimer: null,
    history: [],
    projectConfig: null,
    docs: [],
    view: "edit",
  };

  const els = {
    projectDir: $("projectDir"),
    llmBadge: $("llmBadge"),
    whisperBadge: $("whisperBadge"),
    videoList: $("videoList"),
    search: $("search"),
    refreshVideos: $("refreshVideos"),
    player: $("player"),
    overlay: $("overlay"),
    emptyHint: $("emptyHint"),
    currentName: $("currentName"),
    currentMeta: $("currentMeta"),
    maxChars: $("maxChars"),
    minDuration: $("minDuration"),
    blockChars: $("blockChars"),
    useLlm: $("useLlm"),
    useFrames: $("useFrames"),
    force: $("force"),
    btnFull: $("btnFull"),
    btnClean: $("btnClean"),
    btnTranscribe: $("btnTranscribe"),
    btnFrames: $("btnFrames"),
    btnSave: $("btnSave"),
    btnDownload: $("btnDownload"),
    btnAddCue: $("btnAddCue"),
    btnUndo: $("btnUndo"),
    followPlay: $("followPlay"),
    playbackRate: $("playbackRate"),
    // 上下文 / 资料
    configPath: $("configPath"),
    cfgTitle: $("cfgTitle"),
    ctxContext: $("ctxContext"),
    ctxGlossary: $("ctxGlossary"),
    ctxUseFrames: $("ctxUseFrames"),
    ctxFrameInterval: $("ctxFrameInterval"),
    docList: $("docList"),
    docCount: $("docCount"),
    docInput: $("docInput"),
    btnAddDoc: $("btnAddDoc"),
    docFile: $("docFile"),
    reuseSelect: $("reuseSelect"),
    btnReuse: $("btnReuse"),
    configFile: $("configFile"),
    btnExportConfig: $("btnExportConfig"),
    btnSaveConfig: $("btnSaveConfig"),
    btnPreviewCtx: $("btnPreviewCtx"),
    btnClearCache: $("btnClearCache"),
    ctxPreview: $("ctxPreview"),
    progressBox: $("progressBox"),
    progressFill: $("progressFill"),
    progressMsg: $("progressMsg"),
    progressPct: $("progressPct"),
    jobLog: $("jobLog"),
    cueList: $("cueList"),
    cueCount: $("cueCount"),
    editorTitle: $("editorTitle"),
    editView: $("editView"),
    trajectoryView: $("trajectoryView"),
    toast: $("toast"),
  };

  // ---------------------------------------------------------------- 工具
  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        detail = data.detail || detail;
      } catch (_) {}
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  }

  function toast(message, type = "") {
    els.toast.textContent = message;
    els.toast.className = "toast show " + type;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (els.toast.className = "toast " + type), 3000);
  }

  function formatTime(sec) {
    sec = Math.max(0, Number(sec) || 0);
    const ms = Math.round(sec * 1000);
    const h = Math.floor(ms / 3600000);
    const m = Math.floor((ms % 3600000) / 60000);
    const s = Math.floor((ms % 60000) / 1000);
    const msec = ms % 1000;
    return (
      String(h).padStart(2, "0") + ":" +
      String(m).padStart(2, "0") + ":" +
      String(s).padStart(2, "0") + "." +
      String(msec).padStart(3, "0")
    );
  }

  function parseTime(value) {
    const str = String(value).trim().replace(",", ".");
    if (!str) return 0;
    const parts = str.split(":").map((p) => p.trim());
    let h = 0, m = 0, s = 0;
    if (parts.length === 3) [h, m, s] = parts.map(Number);
    else if (parts.length === 2) [m, s] = parts.map(Number);
    else s = Number(parts[0]);
    if ([h, m, s].some((n) => Number.isNaN(n))) return null;
    return h * 3600 + m * 60 + s;
  }

  function formatDuration(sec) {
    if (!sec || sec <= 0) return "";
    const total = Math.round(sec);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = Math.max(34, el.scrollHeight) + "px";
  }

  function pushHistory() {
    state.history.push(JSON.stringify(state.cues));
    if (state.history.length > 50) state.history.shift();
    updateDirty(true);
  }

  function undo() {
    if (!state.history.length) return;
    state.cues = JSON.parse(state.history.pop());
    renderCues();
    updateDirty(true);
    toast("已撤销");
  }

  function updateDirty(dirty) {
    state.dirty = dirty;
    els.btnSave.disabled = !state.current || (!dirty && els.btnSave.dataset.saved === "1");
  }

  // ---------------------------------------------------------------- 配置/视频库
  async function loadConfig() {
    const cfg = await api("/api/config");
    state.config = cfg;
    els.projectDir.textContent = cfg.project_dir;
    els.maxChars.value = cfg.default_max_chars;
    els.minDuration.value = cfg.default_min_duration;
    els.blockChars.value = cfg.llm_block_chars;
    els.useLlm.checked = cfg.llm_enabled;
    els.ctxFrameInterval.value = cfg.frame_interval;
    els.llmBadge.textContent = cfg.llm_enabled ? `LLM: ${cfg.llm_model}` : "LLM 未配置";
    els.llmBadge.className = "badge " + (cfg.llm_enabled ? "on" : "off");
    els.whisperBadge.textContent = `whisper: ${cfg.whisper_model.split("/").pop()}`;
  }

  async function loadVideos() {
    const data = await api("/api/videos");
    if (data.error) toast(data.error, "error");
    state.videos = data.videos || [];
    renderVideoList();
  }

  function renderVideoList() {
    const keyword = els.search.value.trim().toLowerCase();
    els.videoList.innerHTML = "";
    const list = state.videos.filter((v) => !keyword || v.path.toLowerCase().includes(keyword));
    if (!list.length) {
      els.videoList.innerHTML = '<div class="muted" style="padding:10px">没有视频</div>';
      return;
    }
    for (const video of list) {
      const item = document.createElement("div");
      item.className = "video-item" + (state.current && state.current.path === video.path ? " active" : "");
      item.dataset.path = video.path;
      const chips = [];
      chips.push(`<span class="chip ${video.has_wav ? "ok" : ""}">WAV</span>`);
      chips.push(`<span class="chip ${video.has_whisper ? "ok" : ""}">ASR</span>`);
      chips.push(`<span class="chip ${video.has_srt ? "ok" : "warn"}">${video.has_srt ? "SRT " + video.cue_count : "无字幕"}</span>`);
      if (video.duration) chips.push(`<span class="chip">${formatDuration(video.duration)}</span>`);
      item.innerHTML = `<div class="vname">${escapeHtml(video.name)}</div><div class="vmeta">${chips.join("")}</div>`;
      item.addEventListener("click", () => selectVideo(video.path));
      els.videoList.appendChild(item);
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function selectVideo(path) {
    if (state.dirty && !confirm("当前字幕有未保存的修改，确定要切换吗？")) return;
    state.current = state.videos.find((v) => v.path === path) || { path };
    state.activeIndex = -1;
    state.history = [];
    els.emptyHint.classList.add("hidden");
    els.player.src = `/api/media?path=${encodeURIComponent(path)}`;
    renderVideoList();
    await Promise.all([refreshStatus(), loadCues(), loadVideoConfig(), loadReusableConfigs()]);
    if (state.view === "trajectory") loadTrajectory();
  }

  async function refreshStatus() {
    if (!state.current) return;
    try {
      const status = await api(`/api/videos/status?path=${encodeURIComponent(state.current.path)}`);
      state.current = { ...state.current, ...status };
      const index = state.videos.findIndex((v) => v.path === status.path);
      if (index >= 0) state.videos[index] = status;
      else state.videos.push(status);
      els.currentName.textContent = status.name;
      els.currentMeta.textContent = [status.dir, formatDuration(status.duration)].filter(Boolean).join("  ·  ");
      renderVideoList();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ---------------------------------------------------------------- 字幕
  async function loadCues() {
    if (!state.current) return;
    const data = await api(`/api/subtitles?path=${encodeURIComponent(state.current.path)}`);
    state.cues = data.cues || [];
    state.dirty = false;
    renderCues();
    els.btnDownload.disabled = !data.exists;
    els.btnSave.disabled = !state.cues.length;
    els.btnSave.dataset.saved = data.exists ? "1" : "0";
    els.currentName.textContent = data.video_name;
    if (!data.exists) toast("暂无字幕，请先运行生成流程");
  }

  function renderCues() {
    els.cueList.innerHTML = "";
    els.cueCount.textContent = `（${state.cues.length} 条）`;
    const fragment = document.createDocumentFragment();
    state.cues.forEach((cue, index) => fragment.appendChild(renderRow(cue, index)));
    els.cueList.appendChild(fragment);
    updateActiveCue(videoTime(), true);
  }

  function renderRow(cue, index) {
    const row = document.createElement("div");
    row.className = "cue-row";
    row.dataset.index = index;
    row.innerHTML = `
      <div class="idx">${index + 1}</div>
      <input class="time" data-field="start" value="${formatTime(cue.start)}" />
      <input class="time" data-field="end" value="${formatTime(cue.end)}" />
      <textarea data-field="text" rows="1">${escapeHtml(cue.text)}</textarea>
      <div class="row-tools">
        <button data-action="play" title="从这句播放">▶</button>
        <button data-action="insert" title="在下方插入">＋</button>
        <button data-action="split" title="按光标拆分">✂</button>
        <button data-action="merge" title="与上一句合并">⇧</button>
        <button data-action="delete" class="danger" title="删除">🗑</button>
      </div>`;
    const textarea = row.querySelector("textarea");
    requestAnimationFrame(() => autoGrow(textarea));
    return row;
  }

  function videoTime() {
    return els.player.currentTime || 0;
  }

  function updateActiveCue(time, force) {
    let index = state.cues.findIndex((c) => time >= c.start && time < c.end);
    if (index === -1 && state.cues.length && time >= state.cues[state.cues.length - 1].end) {
      index = state.cues.length - 1;
    }
    els.overlay.innerHTML = index >= 0 ? `<span>${escapeHtml(state.cues[index].text)}</span>` : "";
    if (index === state.activeIndex && !force) return;
    state.activeIndex = index;
    const rows = els.cueList.querySelectorAll(".cue-row");
    rows.forEach((row, i) => row.classList.toggle("active", i === index));
    if (index >= 0 && els.followPlay.checked) {
      const row = rows[index];
      if (row) {
        const rect = row.getBoundingClientRect();
        const listRect = els.cueList.getBoundingClientRect();
        if (rect.top < listRect.top || rect.bottom > listRect.bottom) {
          row.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      }
    }
  }

  els.cueList.addEventListener("input", (event) => {
    const row = event.target.closest(".cue-row");
    if (!row) return;
    const index = Number(row.dataset.index);
    const field = event.target.dataset.field;
    const cue = state.cues[index];
    if (!cue) return;
    if (field === "text") {
      cue.text = event.target.value;
      autoGrow(event.target);
      updateDirty(true);
    } else if (field === "start" || field === "end") {
      const value = parseTime(event.target.value);
      if (value === null) {
        event.target.style.borderColor = "var(--danger)";
        return;
      }
      event.target.style.borderColor = "";
      cue[field] = value;
      updateDirty(true);
    }
  });

  els.cueList.addEventListener("blur", (event) => {
    const row = event.target.closest(".cue-row");
    if (!row) return;
    const index = Number(row.dataset.index);
    const field = event.target.dataset.field;
    if (field === "start" || field === "end") {
      event.target.value = formatTime(state.cues[index][field]);
      updateActiveCue(videoTime(), true);
    }
  }, true);

  els.cueList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest(".cue-row");
    const index = Number(row.dataset.index);
    const cue = state.cues[index];
    const action = button.dataset.action;

    if (action === "play") {
      els.player.currentTime = cue.start;
      els.player.play();
    } else if (action === "insert") {
      pushHistory();
      state.cues.splice(index + 1, 0, { start: cue.end, end: cue.end + 1.5, text: "" });
      renderCues();
    } else if (action === "delete") {
      pushHistory();
      state.cues.splice(index, 1);
      renderCues();
    } else if (action === "merge") {
      if (index === 0) return;
      pushHistory();
      const prev = state.cues[index - 1];
      prev.end = cue.end;
      prev.text = (prev.text + cue.text).trim();
      state.cues.splice(index, 1);
      renderCues();
    } else if (action === "split") {
      splitCue(index, row.querySelector('textarea[data-field="text"]'));
    }
  });

  function splitCue(index, textarea) {
    const cue = state.cues[index];
    let pos = textarea.selectionStart;
    if (pos <= 0 || pos >= cue.text.length) pos = Math.ceil(cue.text.length / 2);
    const head = cue.text.slice(0, pos).trim();
    const tail = cue.text.slice(pos).trim();
    if (!head || !tail) return;
    pushHistory();
    const ratio = head.length / (head.length + tail.length);
    const mid = cue.start + (cue.end - cue.start) * ratio;
    state.cues.splice(index, 1, { start: cue.start, end: mid, text: head }, { start: mid, end: cue.end, text: tail });
    renderCues();
  }

  function addCueAtPlayhead() {
    if (!state.current) return toast("请先选择视频", "error");
    pushHistory();
    const time = videoTime();
    state.cues.push({ start: time, end: time + 2, text: "新字幕" });
    state.cues.sort((a, b) => a.start - b.start);
    renderCues();
    const index = state.cues.findIndex((c) => Math.abs(c.start - time) < 1e-6);
    const row = els.cueList.querySelectorAll(".cue-row")[index];
    if (row) {
      row.scrollIntoView({ block: "center" });
      row.querySelector("textarea").focus();
      row.querySelector("textarea").select();
    }
  }

  async function saveCues() {
    if (!state.current) return;
    try {
      const data = await api("/api/subtitles", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path, cues: state.cues }),
      });
      updateDirty(false);
      els.btnSave.dataset.saved = "1";
      els.btnDownload.disabled = false;
      toast(`已保存 ${data.count} 条字幕到 ${data.srt_name}`, "ok");
      refreshStatus();
    } catch (err) {
      toast("保存失败：" + err.message, "error");
    }
  }

  // ---------------------------------------------------------------- 生成轨迹
  function setView(view) {
    state.view = view;
    document.querySelectorAll(".view-toggle button").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === view)
    );
    els.editView.classList.toggle("hidden", view !== "edit");
    els.trajectoryView.classList.toggle("hidden", view !== "trajectory");
    els.editorTitle.textContent = view === "edit" ? "字幕编辑" : "生成轨迹";
    if (view === "trajectory") loadTrajectory();
  }

  async function loadTrajectory() {
    if (state.view !== "trajectory") return;
    if (!state.current) {
      els.trajectoryView.innerHTML = '<div class="trace-empty">请先选择一个视频</div>';
      return;
    }
    els.trajectoryView.innerHTML = '<div class="trace-empty">加载中…</div>';
    try {
      const data = await api(`/api/trajectory?path=${encodeURIComponent(state.current.path)}`);
      renderTrajectory(data);
    } catch (err) {
      els.trajectoryView.innerHTML = `<div class="trace-empty">${escapeHtml(
        err.message
      )}<br><span class="muted">运行一次「全流程生成字幕」或「仅重新清洗/重排」后即可查看轨迹。</span></div>`;
    }
  }

  function diffHtml(ops, side) {
    let html = "";
    (ops || []).forEach((op) => {
      const a = escapeHtml(op.a || "");
      const b = escapeHtml(op.b || "");
      if (op.op === "equal") html += a;
      else if (op.op === "delete") {
        if (side === "raw") html += `<span class="d-del">${a}</span>`;
      } else if (op.op === "insert") {
        if (side === "clean") html += `<span class="d-ins">${b}</span>`;
      } else if (op.op === "replace") {
        html += `<span class="d-rep">${side === "raw" ? a : b}</span>`;
      }
    });
    return html || '<span class="muted">（空）</span>';
  }

  function frameImg(videoPath, name, time) {
    if (!name) return "";
    const src = `/api/frame?path=${encodeURIComponent(videoPath)}&name=${encodeURIComponent(name)}`;
    return `<figure><img loading="lazy" src="${src}" alt="${escapeHtml(name)}" /><figcaption>${formatTime(
      time
    )}</figcaption></figure>`;
  }

  function renderTrajectory(data) {
    const s = data.stats || {};
    const params = data.params || {};
    const ctx = data.context || {};
    const videoPath = data.video || (state.current && state.current.path) || "";

    const chips = [
      `whisper 片段 <b>${s.segments ?? "-"}</b>`,
      `文本块 <b>${s.blocks ?? "-"}</b>`,
      `LLM 请求 <b>${s.batches ?? "-"}</b>`,
      `字幕 <b>${s.cues ?? "-"}</b>`,
      `<span class="del">删除 ${s.deleted_chars ?? 0} 字</span>`,
      `<span class="ins">新增/改写 ${s.inserted_chars ?? 0} 字</span>`,
      `模型 ${escapeHtml(params.model || "未使用 LLM")}`,
      `每句 ≤ ${params.max_chars ?? "-"} 字`,
      `截图 ${(ctx.frames || []).length} 张`,
    ];

    const info = [
      ["标题", ctx.title || "—"],
      ["内容说明", ctx.context || "—"],
      ["模型", params.model || "未使用"],
      ["接口", params.llm_base_url || "—"],
      ["thinking", params.llm_thinking || "—"],
      ["并发", params.concurrency ?? "—"],
      ["每批截图", params.frames_per_batch ?? "—"],
      ["使用截图", params.use_frames ? "是" : "否"],
      ["截图间隔", (params.frame_interval ?? "—") + "s"],
      ["分块字数", params.block_chars ?? "—"],
      ["最短显示", (params.min_duration ?? "—") + "s"],
      ["whisper 模型", params.whisper_model || "—"],
    ]
      .map(([k, v]) => `<div><b>${k}：</b>${escapeHtml(String(v))}</div>`)
      .join("");

    const glossary = (ctx.glossary || [])
      .map((g) => {
        const aliases = (g.aliases || []).length ? ` <em>= ${escapeHtml(g.aliases.join("、"))}</em>` : "";
        const note = g.note ? ` <span class="muted">${escapeHtml(g.note)}</span>` : "";
        return `<span class="gloss-item">${escapeHtml(g.term)}${aliases}${note}</span>`;
      })
      .join("");

    const documents = (ctx.documents || [])
      .map(
        (d) =>
          `<div>${d.found ? "✓" : "✗"} ${escapeHtml(d.raw)} <span class="muted">${
            d.found ? d.chars + " 字" : "未找到"
          }</span></div>`
      )
      .join("");

    const frames = (ctx.frames || [])
      .slice(0, 60)
      .map((f) => frameImg(videoPath, f.name, f.time))
      .join("");

    const contextCard = `<details class="trace-context">
      <summary>🧭 本次实际注入的上下文（术语表 / 资料 / 截图 / 提示词）</summary>
      <div class="tc-body">
        <div class="trace-kv">${info}</div>
        <h4>术语表 (${(ctx.glossary || []).length})</h4>
        <div class="gloss-list">${glossary || '<span class="muted">无</span>'}</div>
        <h4>参考资料 (${(ctx.documents || []).length})</h4>
        <div class="trace-kv">${documents || '<span class="muted">无</span>'}</div>
        ${frames ? `<h4>可用截图 (${(ctx.frames || []).length})</h4><div class="frames-grid">${frames}</div>` : ""}
        <h4>注入给 LLM 的参考上下文（${(ctx.reference || "").length} 字）</h4>
        <pre>${escapeHtml(ctx.reference || "（空）")}</pre>
      </div>
    </details>`;

    const batches = data.batches || [];
    const blocksHtml = (data.blocks || [])
      .map((block) => {
        const del = (block.diff || [])
          .filter((o) => o.op === "delete" || o.op === "replace")
          .reduce((n, o) => n + (o.a || "").length, 0);
        const ins = (block.diff || [])
          .filter((o) => o.op === "insert" || o.op === "replace")
          .reduce((n, o) => n + (o.b || "").length, 0);
        const cueList = (block.cues || [])
          .map((c) => `<div class="tb-cue"><span class="t">${formatTime(c.start)}</span><span class="x">${escapeHtml(c.text)}</span></div>`)
          .join("");
        const batch = block.batch != null ? batches[block.batch] : null;
        let request = "";
        if (batch) {
          const bFrames = (batch.frames || []).map((f) => frameImg(videoPath, f.name, f.time)).join("");
          request = `<details class="tb-request">
            <summary>查看实际请求（batch #${block.batch}，附 ${batch.attached_images || 0} 张截图）</summary>
            ${batch.error ? `<div class="trace-kv">⚠️ 该批失败，已回退原文：${escapeHtml(batch.error)}</div>` : ""}
            ${bFrames ? `<h4>本批使用的截图</h4><div class="frames-grid">${bFrames}</div>` : ""}
            <h4>发送的 user 消息（含参考上下文与待处理文本）</h4>
            <pre>${escapeHtml(batch.request_text || "")}</pre>
            <h4>模型原始返回</h4>
            <pre>${escapeHtml(batch.response_text || "（空）")}</pre>
          </details>`;
        }
        return `<details class="trace-block">
          <summary>
            <span class="tb-index">#${block.index}</span>
            <span class="tb-time">${formatTime(block.start)} → ${formatTime(block.end)}</span>
            <span class="tb-stat"><span class="del">−${del}</span> / <span class="ins">+${ins}</span> 字</span>
            <span class="muted">${(block.cues || []).length} 条字幕</span>
          </summary>
          <div class="tb-body">
            <div class="trace-grid">
              <div class="tb-pane"><div class="tb-label">whisper 原始（删除/改写为红色）</div><div class="tb-text">${diffHtml(block.diff, "raw")}</div></div>
              <div class="tb-pane"><div class="tb-label">LLM 清洗后（新增/改写为绿/黄色）</div><div class="tb-text">${diffHtml(block.diff, "clean")}</div></div>
            </div>
            <div class="tb-cues">${cueList || '<span class="muted">（本块被全部删除）</span>'}</div>
            ${request}
          </div>
        </details>`;
      })
      .join("");

    els.trajectoryView.innerHTML = `
      <div class="trace-summary">${chips.map((c) => `<span class="chip">${c}</span>`).join("")}</div>
      ${contextCard}
      ${blocksHtml || '<div class="trace-empty">没有文本块</div>'}`;
  }

  // ---------------------------------------------------------------- 上下文 / 资料
  function glossaryToText(list) {
    return (list || [])
      .map((g) => {
        let line = g.term || "";
        if (g.aliases && g.aliases.length) line += " = " + g.aliases.join(", ");
        if (g.note) line += " | " + g.note;
        return line;
      })
      .join("\n");
  }

  function textToGlossary(text) {
    return String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        let note = "";
        const bar = line.indexOf("|");
        if (bar >= 0) {
          note = line.slice(bar + 1).trim();
          line = line.slice(0, bar).trim();
        }
        let term = line;
        let aliases = [];
        const eq = line.indexOf("=");
        if (eq >= 0) {
          term = line.slice(0, eq).trim();
          aliases = line.slice(eq + 1).split(/[,，]/).map((s) => s.trim()).filter(Boolean);
        }
        return { term, aliases, note };
      })
      .filter((g) => g.term);
  }

  async function loadVideoConfig() {
    if (!state.current) return;
    try {
      const data = await api(`/api/video-config?path=${encodeURIComponent(state.current.path)}`);
      state.projectConfig = data.config || {};
      state.docs = (state.projectConfig.documents || []).slice();
      const eff = data.effective || {};
      els.configPath.textContent =
        "本视频配置：" + (data.config_path || "—") + (data.exists ? "" : "（尚未创建，保存后生成）");
      els.cfgTitle.value = state.projectConfig.title || "";
      els.ctxContext.value = state.projectConfig.context || "";
      els.ctxGlossary.value = glossaryToText(eff.glossary || []);
      els.ctxUseFrames.checked = eff.use_frames !== false;
      els.ctxFrameInterval.value = eff.frame_interval || state.config.frame_interval;
      els.useFrames.checked = eff.use_frames !== false;
      renderDocs();
      els.btnSaveConfig.disabled = true;
      els.btnPreviewCtx.disabled = false;
      els.ctxPreview.classList.add("hidden");
    } catch (err) {
      toast("读取配置失败：" + err.message, "error");
    }
  }

  async function loadReusableConfigs() {
    try {
      const q = state.current ? `?path=${encodeURIComponent(state.current.path)}` : "";
      const data = await api(`/api/video-configs${q}`);
      els.reuseSelect.innerHTML = '<option value="">从其他视频复用配置…</option>';
      (data.configs || []).forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.config_path;
        opt.textContent = `${c.title || c.video_path}（术语${c.glossary_count}·资料${c.document_count}）`;
        els.reuseSelect.appendChild(opt);
      });
      els.reuseSelect.disabled = !(data.configs || []).length;
    } catch (_) {
      /* 忽略 */
    }
  }

  function renderDocs() {
    els.docList.innerHTML = "";
    els.docCount.textContent = state.docs.length ? `共 ${state.docs.length} 个` : "（暂无）";
    if (!state.docs.length) {
      els.docList.innerHTML = '<div class="muted" style="font-size:12px">可添加 PDF / Word / Markdown / 文本 / 网页链接</div>';
      return;
    }
    state.docs.forEach((raw) => {
      const item = document.createElement("div");
      item.className = "doc-item";
      item.innerHTML = `<span class="doc-name">${escapeHtml(raw)}</span><button title="移除">✕</button>`;
      item.querySelector("button").addEventListener("click", () => {
        state.docs = state.docs.filter((d) => d !== raw);
        renderDocs();
        markConfigDirty();
      });
      els.docList.appendChild(item);
    });
  }

  function addDocument(raw) {
    raw = String(raw || "").trim();
    if (!raw) return;
    if (state.docs.includes(raw)) return toast("该资料已在列表中");
    state.docs.push(raw);
    renderDocs();
    markConfigDirty();
  }

  async function uploadDocument(file) {
    if (!state.current || !file) return;
    const form = new FormData();
    form.append("path", state.current.path);
    form.append("file", file);
    try {
      const data = await api("/api/upload", { method: "POST", body: form });
      addDocument(data.rel_path);
      toast("已上传 " + data.name, "ok");
    } catch (err) {
      toast("上传失败：" + err.message, "error");
    }
  }

  function markConfigDirty() {
    els.btnSaveConfig.disabled = false;
  }

  async function saveVideoConfig() {
    if (!state.current) return;
    const config = {
      title: els.cfgTitle.value,
      context: els.ctxContext.value,
      glossary: textToGlossary(els.ctxGlossary.value),
      documents: state.docs,
      use_frames: els.ctxUseFrames.checked,
      frame_interval: Number(els.ctxFrameInterval.value) || 45,
    };
    try {
      const data = await api("/api/video-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path, config }),
      });
      state.projectConfig = data.config;
      els.btnSaveConfig.disabled = true;
      els.useFrames.checked = els.ctxUseFrames.checked;
      els.configPath.textContent = "本视频配置：" + data.config_path;
      await Promise.all([refreshStatus(), loadReusableConfigs()]);
      toast("配置已保存到 " + data.config_path, "ok");
    } catch (err) {
      toast("保存配置失败：" + err.message, "error");
    }
  }

  async function reuseConfig() {
    const source = els.reuseSelect.value;
    if (!source) return toast("请先选择要复用的配置");
    if (!confirm("将用所选配置覆盖当前视频的配置，确定吗？")) return;
    try {
      await api("/api/video-config/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path, source_config_path: source }),
      });
      await loadVideoConfig();
      toast("已复用配置", "ok");
    } catch (err) {
      toast("复用失败：" + err.message, "error");
    }
  }

  async function importConfigFile(file) {
    if (!state.current || !file) return;
    const form = new FormData();
    form.append("path", state.current.path);
    form.append("file", file);
    try {
      await api("/api/video-config/upload", { method: "POST", body: form });
      await loadVideoConfig();
      toast("已导入配置", "ok");
    } catch (err) {
      toast("导入失败：" + err.message, "error");
    }
  }

  function exportConfig() {
    if (!state.current) return;
    window.location.href = `/api/video-config/download?path=${encodeURIComponent(state.current.path)}`;
  }

  async function previewContext() {
    if (!state.current) return;
    els.ctxPreview.classList.remove("hidden");
    els.ctxPreview.textContent = "加载中…";
    try {
      const data = await api(`/api/context-preview?path=${encodeURIComponent(state.current.path)}`);
      const docLines = (data.documents || [])
        .map((d) => `${d.found ? "✓" : "✗"} ${d.raw}`)
        .join("\n");
      const logs = (data.logs || []).join("\n");
      els.ctxPreview.textContent =
        `【资料可用性】\n${docLines || "（无）"}\n\n` +
        (logs ? `【处理日志】\n${logs}\n\n` : "") +
        `【注入给 LLM 的参考上下文】(${data.chars} 字)\n${data.reference || "（空）"}`;
    } catch (err) {
      els.ctxPreview.textContent = "预览失败：" + err.message;
    }
  }

  async function clearContextCache() {
    if (!state.current) return;
    try {
      const data = await api("/api/clear-context-cache", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path }),
      });
      toast(data.removed ? "已清除摘要缓存" : "没有缓存需要清除", "ok");
    } catch (err) {
      toast("清除失败：" + err.message, "error");
    }
  }

  // ---------------------------------------------------------------- 任务
  function jobOptions() {
    return {
      max_chars: Number(els.maxChars.value) || 18,
      min_duration: Number(els.minDuration.value) || 0.8,
      block_chars: Number(els.blockChars.value) || 220,
      use_llm: els.useLlm.checked,
      use_frames: els.useFrames.checked,
      frame_interval: Number(els.ctxFrameInterval.value) || state.config.frame_interval,
      force: els.force.checked,
    };
  }

  async function startJob(steps) {
    if (!state.current) return toast("请先选择视频", "error");
    if (state.dirty && !confirm("当前字幕有未保存的修改，运行生成任务会覆盖它，确定继续吗？")) return;
    try {
      const job = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path, steps, ...jobOptions() }),
      });
      state.jobId = job.id;
      els.progressBox.classList.remove("hidden");
      els.jobLog.textContent = "";
      setButtonsBusy(true);
      updateJob(job);
      pollJob();
    } catch (err) {
      toast("启动失败：" + err.message, "error");
    }
  }

  function updateJob(job) {
    els.progressFill.style.width = job.progress + "%";
    els.progressPct.textContent = job.progress + "%";
    els.progressMsg.textContent = job.message || job.status;
    els.jobLog.textContent = (job.logs || []).join("\n");
    els.jobLog.scrollTop = els.jobLog.scrollHeight;
  }

  function pollJob() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      if (!state.jobId) return;
      try {
        const job = await api(`/api/jobs/${state.jobId}`);
        updateJob(job);
        if (job.status === "done") {
          setButtonsBusy(false);
          toast("生成完成", "ok");
          state.jobId = null;
          await Promise.all([refreshStatus(), loadCues()]);
          if (state.view === "trajectory") loadTrajectory();
          return;
        }
        if (job.status === "error") {
          setButtonsBusy(false);
          toast("任务失败：" + (job.error || ""), "error");
          state.jobId = null;
          return;
        }
      } catch (err) {
        console.error(err);
      }
      pollJob();
    }, 800);
  }

  function setButtonsBusy(busy) {
    [els.btnFull, els.btnClean, els.btnTranscribe, els.btnFrames].forEach((b) => (b.disabled = busy));
  }

  // ---------------------------------------------------------------- 播放器
  const RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];

  function setRate(rate, notify) {
    rate = Math.max(0.25, Math.min(4, Number(rate) || 1));
    rate = Math.round(rate * 100) / 100;
    els.player.playbackRate = rate;
    if (!RATES.some((r) => Math.abs(r - rate) < 0.001)) {
      const option = document.createElement("option");
      option.value = String(rate);
      option.textContent = rate + "×";
      els.playbackRate.appendChild(option);
    }
    els.playbackRate.value = String(rate);
    try {
      localStorage.setItem("subtitlePlaybackRate", String(rate));
    } catch (_) {}
    if (notify) toast(`倍速 ${rate}×`);
  }

  function stepRate(direction) {
    const current = Number(els.playbackRate.value) || 1;
    let index = RATES.findIndex((r) => Math.abs(r - current) < 0.001);
    if (index === -1) index = 2;
    index = Math.max(0, Math.min(RATES.length - 1, index + direction));
    setRate(RATES[index], true);
  }

  function tick() {
    if (state.cues.length) updateActiveCue(videoTime());
    requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------- 事件绑定
  els.refreshVideos.addEventListener("click", loadVideos);
  els.search.addEventListener("input", renderVideoList);
  els.btnFull.addEventListener("click", () => startJob(["extract_audio", "transcribe", "frames", "clean"]));
  els.btnClean.addEventListener("click", () => startJob(["clean"]));
  els.btnTranscribe.addEventListener("click", () => startJob(["transcribe", "clean"]));
  els.btnFrames.addEventListener("click", () => startJob(["frames"]));
  els.btnSaveConfig.addEventListener("click", saveVideoConfig);
  els.btnPreviewCtx.addEventListener("click", previewContext);
  els.btnClearCache.addEventListener("click", clearContextCache);
  els.btnReuse.addEventListener("click", reuseConfig);
  els.btnExportConfig.addEventListener("click", exportConfig);
  els.configFile.addEventListener("change", () => {
    if (els.configFile.files[0]) importConfigFile(els.configFile.files[0]);
    els.configFile.value = "";
  });
  els.btnAddDoc.addEventListener("click", () => {
    addDocument(els.docInput.value);
    els.docInput.value = "";
  });
  els.docInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      addDocument(els.docInput.value);
      els.docInput.value = "";
    }
  });
  els.docFile.addEventListener("change", () => {
    if (els.docFile.files[0]) uploadDocument(els.docFile.files[0]);
    els.docFile.value = "";
  });
  [els.cfgTitle, els.ctxContext, els.ctxGlossary, els.ctxUseFrames, els.ctxFrameInterval]
    .forEach((el) => el.addEventListener("input", markConfigDirty));
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.add("hidden"));
      $("tab-" + tab.dataset.tab).classList.remove("hidden");
    });
  });
  document.querySelectorAll(".view-toggle button").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  els.btnSave.addEventListener("click", saveCues);
  els.btnAddCue.addEventListener("click", addCueAtPlayhead);
  els.btnUndo.addEventListener("click", undo);
  els.playbackRate.addEventListener("change", () => setRate(els.playbackRate.value, true));
  els.player.addEventListener("loadedmetadata", () => {
    els.player.playbackRate = Number(els.playbackRate.value) || 1;
  });
  try {
    const savedRate = parseFloat(localStorage.getItem("subtitlePlaybackRate") || "1");
    if (savedRate) setRate(savedRate, false);
  } catch (_) {}
  els.btnDownload.addEventListener("click", () => {
    if (state.current) {
      window.location.href = `/api/subtitles/download?path=${encodeURIComponent(state.current.path)}`;
    }
  });

  document.addEventListener("keydown", (event) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!els.btnSave.disabled) saveCues();
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
      if (tag === "TEXTAREA") return;
      event.preventDefault();
      undo();
    }
    if (!typing && (event.key === "[" || event.key === "]")) {
      event.preventDefault();
      stepRate(event.key === "]" ? 1 : -1);
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ---------------------------------------------------------------- 启动
  (async () => {
    try {
      await loadConfig();
      await loadVideos();
      requestAnimationFrame(tick);
    } catch (err) {
      toast("初始化失败：" + err.message, "error");
    }
  })();
})();
