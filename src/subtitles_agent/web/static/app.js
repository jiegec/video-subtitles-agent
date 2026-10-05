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
    saveTimer: null,
    preGenHadSrt: false,
    llmResolved: null,
    overlayText: "",
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
    btnFull: $("btnFull"),
    btnDownload: $("btnDownload"),
    btnAddCue: $("btnAddCue"),
    btnUndo: $("btnUndo"),
    btnHistory: $("btnHistory"),
    saveStatus: $("saveStatus"),
    backupPanel: $("backupPanel"),
    followPlay: $("followPlay"),
    playbackRate: $("playbackRate"),
    // 上下文 / 资料
    configPath: $("configPath"),
    cfgTitle: $("cfgTitle"),
    ctxContext: $("ctxContext"),
    ctxGlossary: $("ctxGlossary"),
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
    chapterList: $("chapterList"),
    chaptersFile: $("chaptersFile"),
    btnGenChapters: $("btnGenChapters"),
    btnCopyChapters: $("btnCopyChapters"),
    btnDownloadChapters: $("btnDownloadChapters"),
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
    diffModal: $("diffModal"),
    settingsModal: $("settingsModal"),
    btnSettings: $("btnSettings"),
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

  function formatClock(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const h = String(Math.floor(sec / 3600)).padStart(2, "0");
    const m = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
    const s = String(sec % 60).padStart(2, "0");
    return `${h}:${m}:${s}`;
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

  function setSaveStatus(text) {
    els.saveStatus.textContent = text || "";
  }

  function scheduleSave() {
    if (!state.current || !state.cues.length) return;
    setSaveStatus("编辑中…");
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => saveCues(true), 1500);
  }

  async function flushSave() {
    if (state.saveTimer) {
      clearTimeout(state.saveTimer);
      state.saveTimer = null;
    }
    if (state.dirty) await saveCues(true);
  }

  function updateDirty(dirty) {
    state.dirty = dirty;
    if (dirty) scheduleSave();
  }

  // ---------------------------------------------------------------- 配置/视频库
  async function loadConfig() {
    const cfg = await api("/api/config");
    state.config = cfg;
    els.projectDir.textContent = cfg.project_dir;
    els.maxChars.value = cfg.default_max_chars;
    els.minDuration.value = cfg.default_min_duration;
    els.blockChars.value = cfg.llm_block_chars;
    els.ctxFrameInterval.value = cfg.frame_interval;
    els.llmBadge.textContent = cfg.llm_enabled
      ? `LLM: ${cfg.llm_model}${cfg.llm_source === "pi" ? " · pi" : ""}`
      : "LLM 未配置";
    els.llmBadge.className = "badge " + (cfg.llm_enabled ? "on" : "off");
    els.whisperBadge.textContent =
      cfg.asr_backend === "tencent"
        ? "ASR: 腾讯云"
        : `whisper: ${cfg.whisper_model.split("/").pop()}`;
  }

  // ---------------------------------------------------------------- 全局设置
  const SETTINGS_SECTIONS = [
    {
      title: "路径与命令行",
      desc: "相对路径都相对于 config.toml 所在目录。改完保存立即生效。",
      fields: [
        { path: "paths.project_dir", label: "视频目录", type: "text", placeholder: "project" },
        { path: "paths.ffmpeg_bin", label: "ffmpeg 路径", type: "text", placeholder: "ffmpeg" },
        { path: "paths.whisper_bin", label: "whisper 可执行文件", type: "text", placeholder: "whisper-cli" },
        { path: "paths.whisper_model", label: "whisper 模型", type: "text", placeholder: "ggml-medium.bin" },
      ],
    },
    {
      title: "语音识别",
      desc: "选择识别后端：本地 whisper-cli，或腾讯云录音文件识别。",
      noteId: "asrNote",
      fields: [
        {
          path: "asr.backend",
          label: "识别后端",
          type: "select",
          wide: true,
          options: [
            { value: "whisper", label: "whisper-cli（本地）" },
            { value: "tencent", label: "腾讯云录音文件识别（云端）" },
          ],
        },
        { path: "whisper.lang", label: "语言", type: "text", placeholder: "zh", group: "whisper" },
        { path: "whisper.threads", label: "线程数", type: "number", min: 1, group: "whisper" },
        { path: "whisper.prompt", label: "初始提示词", type: "textarea", wide: true, group: "whisper" },
        { path: "tencent.secret_id", label: "腾讯云 SecretId", type: "text", wide: true, group: "tencent" },
        { path: "tencent.secret_key", label: "腾讯云 SecretKey", type: "password", wide: true, group: "tencent" },
        { path: "tencent.region", label: "地域", type: "text", placeholder: "ap-shanghai", group: "tencent" },
        { path: "tencent.engine", label: "引擎模型", type: "text", placeholder: "16k_zh_en_2.0", group: "tencent" },
        { path: "tencent.chunk_seconds", label: "分段秒数(每段<5MB)", type: "number", min: 60, max: 1200, group: "tencent" },
      ],
      footer: '<span id="asrTestWrap"><button type="button" id="asrTestBtn">测试密钥</button><span id="asrTestResult" class="test-result"></span></span>',
    },
    {
      title: "LLM（OpenAI 兼容）",
      desc: "默认从 pi（~/.pi/agent）读取接口地址 / 密钥 / 模型，不会写入 config.toml；也可以改为手动配置。",
      noteId: "llmSourceNote",
      fields: [
        {
          path: "llm.source",
          label: "模型来源",
          type: "select",
          wide: true,
          options: [
            { value: "pi", label: "从 pi 自动获取" },
            { value: "manual", label: "手动配置" },
          ],
        },
        { path: "llm.base_url", label: "接口地址 Base URL", type: "text", placeholder: "https://api.openai.com/v1", wide: true, llmManaged: true },
        { path: "llm.api_key", label: "API Key", type: "password", wide: true, llmManaged: true },
        { path: "llm.model", label: "模型", type: "text", placeholder: "deepseek-v4-flash", llmManaged: true },
        { path: "llm.concurrency", label: "并发请求数", type: "number", min: 1 },
        { path: "llm.max_tokens", label: "单次返回上限", type: "number", min: 1 },
        { path: "llm.timeout", label: "超时(秒)", type: "number", min: 1 },
        { path: "llm.thinking", label: "推理强度", type: "select", options: ["off", "low", "medium", "high", "auto"] },
      ],
      footer: '<button type="button" id="llmTestBtn">测试连接</button><span id="llmTestResult" class="test-result"></span>',
    },
    {
      title: "字幕默认参数",
      fields: [
        { path: "subtitles.max_chars", label: "每句最多字数", type: "number", min: 6 },
        { path: "subtitles.min_duration", label: "最短显示(秒)", type: "number", min: 0.3, step: 0.1 },
        { path: "subtitles.block_chars", label: "LLM 分块字数", type: "number", min: 60 },
      ],
    },
    {
      title: "截图与参考资料",
      fields: [
        { path: "frames.interval", label: "截图间隔(秒)", type: "number", min: 5 },
        { path: "frames.max_per_batch", label: "每批最多截图", type: "number", min: 0 },
        { path: "frames.max_count", label: "单视频最多截图", type: "number", min: 1 },
        { path: "context.max_chars", label: "资料注入上限(字)", type: "number", min: 1000 },
      ],
    },
    {
      title: "漏识别补齐",
      fields: [
        { path: "recover.model", label: "补转模型(留空同主模型)", type: "text" },
        { path: "recover.noise_db", label: "静音噪声阈值(dB)", type: "number", step: 1 },
        { path: "recover.min_silence", label: "判定静音时长(秒)", type: "number", step: 0.1 },
        { path: "recover.min_gap", label: "最短缺口(秒)", type: "number", step: 0.1 },
        { path: "recover.min_cps", label: "语速下限(字/秒)", type: "number", step: 0.1 },
        { path: "recover.low_conf_threshold", label: "低置信阈值", type: "number", step: 0.05 },
        { path: "recover.max_regions", label: "最多重转区间", type: "number", min: 1 },
      ],
    },
    {
      title: "历史版本",
      fields: [
        { path: "backup.keep", label: "最多保留版本", type: "number", min: 0 },
        { path: "backup.interval", label: "备份最小间隔(秒)", type: "number", min: 0 },
      ],
    },
  ];

  function getPath(obj, path) {
    return path.split(".").reduce((acc, key) => (acc == null ? undefined : acc[key]), obj);
  }

  function setPath(obj, path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    let cursor = obj;
    for (const key of keys) {
      if (typeof cursor[key] !== "object" || cursor[key] === null) cursor[key] = {};
      cursor = cursor[key];
    }
    cursor[last] = value;
  }

  function renderSettingField(field, value) {
    const current = value == null ? "" : value;
    const classes = field.wide ? ' class="wide"' : "";
    const group = field.group ? ` data-group="${field.group}"` : "";
    const attrs = `data-path="${field.path}"`;
    if (field.type === "textarea") {
      return `<label${classes}${group}>${field.label}<textarea ${attrs} rows="2">${escapeHtml(current)}</textarea></label>`;
    }
    if (field.type === "select") {
      const options = field.options
        .map((opt) => {
          const optValue = typeof opt === "string" ? opt : opt.value;
          const label = typeof opt === "string" ? opt : opt.label;
          const selected = String(optValue) === String(current) ? " selected" : "";
          return `<option value="${optValue}"${selected}>${label}</option>`;
        })
        .join("");
      return `<label${classes}${group}>${field.label}<select ${attrs}>${options}</select></label>`;
    }
    const min = field.min != null ? ` min="${field.min}"` : "";
    const max = field.max != null ? ` max="${field.max}"` : "";
    const step = field.step != null ? ` step="${field.step}"` : "";
    const placeholder = field.placeholder ? ` placeholder="${field.placeholder}"` : "";
    if (field.type === "password") {
      return `<label${classes}${group}>${field.label}<span class="pw-wrap"><input type="password" ${attrs} value="${escapeHtml(current)}"${placeholder} /><button type="button" class="icon-btn" data-toggle-pw>显示</button></span></label>`;
    }
    return `<label${classes}${group}>${field.label}<input type="${field.type}" ${attrs} value="${escapeHtml(current)}"${min}${max}${step}${placeholder} /></label>`;
  }

  function renderSettingsForm(configData, configName) {
    const sections = SETTINGS_SECTIONS.map((section) => {
      const fields = section.fields
        .map((field) => renderSettingField(field, getPath(configData, field.path)))
        .join("");
      const desc = section.desc ? `<p class="hint">${section.desc}</p>` : "";
      const note = section.noteId ? `<p class="llm-source-note" id="${section.noteId}"></p>` : "";
      const footer = section.footer ? `<div class="settings-footer">${section.footer}</div>` : "";
      return `<section class="settings-section"><h3>${section.title}</h3>${desc}${note}<div class="settings-grid">${fields}</div>${footer}</section>`;
    }).join("");
    els.settingsModal.innerHTML = `
      <div class="modal-card settings-card">
        <div class="modal-head"><strong>全局设置</strong><button class="icon-btn" data-act="close">✕</button></div>
        <div class="modal-body">
          <p class="hint">所有配置都保存在 <code>${escapeHtml(configName)}</code>，保存后立即生效。</p>
          ${sections}
        </div>
        <div class="modal-foot">
          <span class="save-status" id="settingsStatus"></span>
          <button data-act="close">取消</button>
          <button class="primary" data-act="save">保存设置</button>
        </div>
      </div>`;
  }

  async function openSettings() {
    try {
      const data = await api("/api/settings");
      state.llmResolved = (data.settings && data.settings.llm && data.settings.llm.resolved) || null;
      renderSettingsForm(data.settings, data.config_path);
      els.settingsModal.classList.remove("hidden");
      const sourceEl = els.settingsModal.querySelector('[data-path="llm.source"]');
      if (sourceEl) sourceEl.addEventListener("change", updateLlmSourceFields);
      const asrEl = els.settingsModal.querySelector('[data-path="asr.backend"]');
      if (asrEl) asrEl.addEventListener("change", updateAsrFields);
      const testBtn = els.settingsModal.querySelector("#llmTestBtn");
      if (testBtn) testBtn.addEventListener("click", testLlm);
      const asrTestBtn = els.settingsModal.querySelector("#asrTestBtn");
      if (asrTestBtn) asrTestBtn.addEventListener("click", testAsr);
      updateLlmSourceFields();
      updateAsrFields();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function updateLlmSourceFields() {
    const sourceEl = els.settingsModal.querySelector('[data-path="llm.source"]');
    if (!sourceEl) return;
    const fromPi = sourceEl.value === "pi";
    els.settingsModal.querySelectorAll("[data-path^='llm.']").forEach((el) => {
      if (["llm.base_url", "llm.api_key", "llm.model"].includes(el.dataset.path)) {
        el.disabled = fromPi;
      }
    });
    const note = els.settingsModal.querySelector("#llmSourceNote");
    if (!note) return;
    const resolved = state.llmResolved;
    if (fromPi) {
      if (resolved && resolved.model) {
        const parts = [resolved.model];
        if (resolved.base_url) parts.push(resolved.base_url);
        parts.push(resolved.has_key ? "已找到密钥" : "未找到密钥，将跳过 LLM");
        note.textContent = `当前从 pi 读取：${parts.join(" · ")}`;
      } else {
        note.textContent = "未从 pi 找到 LLM 配置，请改为手动配置。";
      }
    } else {
      note.textContent = "手动填写的接口地址 / 密钥 / 模型会写入 config.toml。";
    }
  }

  function updateAsrFields() {
    const backendEl = els.settingsModal.querySelector('[data-path="asr.backend"]');
    if (!backendEl) return;
    const backend = backendEl.value;
    els.settingsModal.querySelectorAll("[data-group]").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.group !== backend);
    });
    const note = els.settingsModal.querySelector("#asrNote");
    if (!note) return;
    note.textContent =
      backend === "tencent"
        ? "腾讯云按量计费；音频会自动分片上传（每段 < 5MB），需填好 SecretId / SecretKey。首次使用需安装：uv sync --extra tencent。"
        : "本地识别，需要安装 whisper-cli 并准备好模型文件。";
    const testWrap = els.settingsModal.querySelector("#asrTestWrap");
    if (testWrap) testWrap.classList.toggle("hidden", backend !== "tencent");
  }

  function collectSettingsForm() {
    const data = {};
    els.settingsModal.querySelectorAll("[data-path]").forEach((el) => {
      if (el.disabled) return;
      if (el.type === "number") {
        if (el.value === "") return;
        const num = Number(el.value);
        if (Number.isNaN(num)) return;
        setPath(data, el.dataset.path, num);
      } else {
        setPath(data, el.dataset.path, el.value);
      }
    });
    return data;
  }

  async function testLlm() {
    const button = els.settingsModal.querySelector("#llmTestBtn");
    const output = els.settingsModal.querySelector("#llmTestResult");
    if (!button || !output) return;
    button.disabled = true;
    output.className = "test-result";
    output.textContent = "测试中…";
    try {
      const form = collectSettingsForm();
      const data = await api("/api/llm/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: { llm: form.llm || {} } }),
      });
      output.className = "test-result ok";
      output.textContent = `✓ ${data.model} · ${data.elapsed}s · 回复：${data.reply || "(空)"}`;
    } catch (err) {
      output.className = "test-result error";
      output.textContent = `✗ ${err.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function testAsr() {
    const button = els.settingsModal.querySelector("#asrTestBtn");
    const output = els.settingsModal.querySelector("#asrTestResult");
    if (!button || !output) return;
    button.disabled = true;
    output.className = "test-result";
    output.textContent = "测试中…";
    try {
      const form = collectSettingsForm();
      const data = await api("/api/asr/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          settings: { asr: form.asr || {}, tencent: form.tencent || {} },
        }),
      });
      output.className = "test-result ok";
      output.textContent = `✓ ${data.message || "密钥有效"}`;
    } catch (err) {
      output.className = "test-result error";
      output.textContent = `✗ ${err.message}`;
    } finally {
      button.disabled = false;
    }
  }

  function closeSettings() {
    els.settingsModal.classList.add("hidden");
    els.settingsModal.innerHTML = "";
  }

  async function saveSettings() {
    const data = collectSettingsForm();
    const status = els.settingsModal.querySelector("#settingsStatus");
    if (status) status.textContent = "保存中…";
    try {
      await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: data }),
      });
      toast("设置已保存", "ok");
      closeSettings();
      await loadConfig();
      await loadVideos();
    } catch (err) {
      if (status) status.textContent = "";
      toast(err.message, "error");
    }
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
      chips.push(`<span class="chip ${video.has_asr ? "ok" : ""}">ASR</span>`);
      chips.push(`<span class="chip ${video.has_srt ? "ok" : "warn"}">${video.has_srt ? "SRT " + video.cue_count : "无字幕"}</span>`);
      if (video.duration) chips.push(`<span class="chip">${formatDuration(video.duration)}</span>`);
      if (video.recovery_regions)
        chips.push(`<span class="chip ok">补齐 ${video.recovery_recovered || video.recovery_regions}</span>`);
      item.innerHTML = `<div class="vname">${escapeHtml(video.name)}</div><div class="vmeta">${chips.join("")}</div>`;
      item.addEventListener("click", () => selectVideo(video.path));
      els.videoList.appendChild(item);
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function selectVideo(path) {
    await flushSave();
    state.current = state.videos.find((v) => v.path === path) || { path };
    state.activeIndex = -1;
    state.history = [];
    try {
      localStorage.setItem("subtitlesSelected", path);
    } catch (_) {}
    els.emptyHint.classList.add("hidden");
    els.player.src = `/api/media?path=${encodeURIComponent(path)}`;
    renderVideoList();
    await Promise.all([refreshStatus(), loadCues(), loadVideoConfig(), loadReusableConfigs(), loadChapters()]);
    if (state.view === "trajectory") loadTrajectory();
    await resumeJob();
  }

  async function resumeJob() {
    if (!state.current) return;
    try {
      const data = await api(`/api/jobs/active?path=${encodeURIComponent(state.current.path)}`);
      const job = data.job;
      if (!job) return;
      state.jobId = job.id;
      els.progressBox.classList.remove("hidden");
      updateJob(job);
      if (job.status === "pending" || job.status === "running") {
        setButtonsBusy(true);
        pollJob();
      } else {
        setButtonsBusy(false);
      }
    } catch (_) {
      /* 忽略 */
    }
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
    setSaveStatus("");
    els.backupPanel.classList.add("hidden");
    renderCues();
    els.btnDownload.disabled = !data.exists;
    els.btnGenChapters.disabled = !data.exists;
    els.currentName.textContent = data.video_name;
    if (!data.exists) toast("暂无字幕，请先运行生成流程");
  }

  function renderCues() {
    els.cueCount.textContent = `（${state.cues.length} 条）`;
    // 一次拼好整段 HTML 交给浏览器解析，比逐行 createElement 快很多
    els.cueList.innerHTML = state.cues.map(rowHtml).join("");
    autoGrowAllCues();
    updateActiveCue(videoTime(), true);
  }

  // 批量自适应高度：先统一读取 scrollHeight，再统一写入，避免逐行触发重排（大文件会卡死）
  function autoGrowAllCues() {
    const areas = els.cueList.querySelectorAll('textarea[data-field="text"]');
    const heights = new Array(areas.length);
    for (let i = 0; i < areas.length; i++) heights[i] = areas[i].scrollHeight;
    for (let i = 0; i < areas.length; i++) {
      areas[i].style.height = Math.max(34, heights[i]) + "px";
    }
  }

  function rowHtml(cue, index) {
    return `<div class="cue-row" data-index="${index}">
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
      </div>
    </div>`;
  }

  function videoTime() {
    return els.player.currentTime || 0;
  }

  function updateActiveCue(time, force) {
    let index = state.cues.findIndex((c) => time >= c.start && time < c.end);
    if (index === -1 && state.cues.length && time >= state.cues[state.cues.length - 1].end) {
      index = state.cues.length - 1;
    }
    const text = index >= 0 ? state.cues[index].text : "";
    // 每帧都会调用：内容没变就直接返回，避免无谓的 DOM 写入
    if (!force && index === state.activeIndex && text === state.overlayText) return;
    const indexChanged = index !== state.activeIndex || force;
    state.activeIndex = index;
    state.overlayText = text;
    els.overlay.innerHTML = index >= 0 ? `<span>${escapeHtml(text)}</span>` : "";
    if (!indexChanged) return;
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

  async function saveCues(silent) {
    if (!state.current || !state.cues.length) return;
    state.saveTimer = null;
    try {
      const data = await api("/api/subtitles", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path, cues: state.cues }),
      });
      updateDirty(false);
      els.btnDownload.disabled = false;
      setSaveStatus(`已自动保存 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      if (!silent) toast(`已保存 ${data.count} 条字幕`, "ok");
      refreshStatus();
    } catch (err) {
      setSaveStatus("保存失败");
      toast("保存失败：" + err.message, "error");
    }
  }

  // ---------------------------------------------------------------- 章节列表
  async function loadChapters() {
    if (!state.current) return;
    try {
      const data = await api(`/api/chapters?path=${encodeURIComponent(state.current.path)}`);
      renderChapters(data);
    } catch (_) {
      /* 忽略 */
    }
  }

  function renderChapters(data) {
    const chapters = (data && data.chapters) || [];
    if (chapters.length) {
      els.chapterList.textContent = chapters
        .map((c) => `${formatClock(c.start)} ${c.title}`)
        .join("\n");
    } else {
      els.chapterList.textContent = data && data.exists ? "（文件为空）" : "尚未生成";
    }
    els.chaptersFile.textContent = data && data.file_name ? data.file_name : "";
    els.btnCopyChapters.disabled = !chapters.length;
    els.btnDownloadChapters.disabled = !chapters.length;
  }

  async function generateChapters() {
    if (!state.current) return toast("请先选择视频", "error");
    const label = els.btnGenChapters.textContent;
    els.btnGenChapters.disabled = true;
    els.btnGenChapters.textContent = "生成中…";
    try {
      const data = await api("/api/chapters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.current.path }),
      });
      renderChapters(data);
      toast(`已生成 ${data.chapters.length} 个章节 → ${data.file_name}`, "ok");
    } catch (err) {
      toast("生成章节失败：" + err.message, "error");
    } finally {
      els.btnGenChapters.textContent = label;
      els.btnGenChapters.disabled = !(state.current && state.current.has_srt);
    }
  }

  // ---------------------------------------------------------------- 历史版本
  function toggleHistory() {
    const hidden = els.backupPanel.classList.toggle("hidden");
    if (!hidden) loadBackups();
  }

  async function loadBackups() {
    if (!state.current) return;
    els.backupPanel.innerHTML = '<div class="muted" style="padding:8px">加载中…</div>';
    try {
      const data = await api(`/api/backups?path=${encodeURIComponent(state.current.path)}`);
      renderBackups(data.backups || []);
    } catch (err) {
      els.backupPanel.innerHTML = `<div class="muted" style="padding:8px">读取失败：${escapeHtml(
        err.message
      )}</div>`;
    }
  }

  function renderBackups(list) {
    if (!list.length) {
      els.backupPanel.innerHTML = '<div class="muted" style="padding:8px">暂无历史版本</div>';
      return;
    }
    els.backupPanel.innerHTML = "";
    list.forEach((backup) => {
      const when = new Date(backup.mtime * 1000).toLocaleString("zh-CN", { hour12: false });
      const item = document.createElement("div");
      item.className = "backup-item";
      item.innerHTML = `<span class="bname">${escapeHtml(backup.name)}</span>
        <span class="muted">${when} · ${(backup.size / 1024).toFixed(1)}KB</span>
        <button data-act="restore">恢复</button>
        <button data-act="download">下载</button>
        <button data-act="delete" class="danger">删除</button>`;
      item.querySelectorAll("button").forEach((button) =>
        button.addEventListener("click", () => backupAction(button.dataset.act, backup.name))
      );
      els.backupPanel.appendChild(item);
    });
  }

  async function backupAction(action, name) {
    if (!state.current) return;
    if (action === "download") {
      window.location.href = `/api/backups/download?path=${encodeURIComponent(
        state.current.path
      )}&name=${encodeURIComponent(name)}`;
      return;
    }
    if (action === "restore") {
      if (!confirm(`用 ${name} 覆盖当前字幕？当前版本会先备份。`)) return;
      try {
        await api("/api/backups/restore", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.current.path, name }),
        });
        await loadCues();
        await loadBackups();
        toast("已恢复 " + name, "ok");
      } catch (err) {
        toast("恢复失败：" + err.message, "error");
      }
      return;
    }
    if (action === "delete") {
      if (!confirm(`删除备份 ${name}？`)) return;
      try {
        await api("/api/backups/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.current.path, name }),
        });
        await loadBackups();
      } catch (err) {
        toast("删除失败：" + err.message, "error");
      }
    }
  }

  // ---------------------------------------------------------------- 新旧字幕对比
  async function showDiffModal() {
    state.preGenHadSrt = false;
    if (!state.current) return;
    try {
      const data = await api(`/api/backups?path=${encodeURIComponent(state.current.path)}`);
      const list = data.backups || [];
      if (!list.length) return;
      const against = list[0].name; // 最新备份 = 本次生成前的版本
      const diff = await api(
        `/api/subtitles/diff?path=${encodeURIComponent(
          state.current.path
        )}&against=${encodeURIComponent(against)}`
      );
      renderDiffModal(diff, against);
    } catch (err) {
      console.error(err);
    }
  }

  function renderDiffModal(data, against) {
    const s = data.stats || {};
    const changes = data.changes || [];
    const rows = changes
      .slice(0, 300)
      .map((c) => {
        if (c.type === "added")
          return `<div class="diff-row"><div class="d-old muted">（新增）</div><div class="d-new">${escapeHtml(
            c.new.text
          )}</div></div>`;
        if (c.type === "removed")
          return `<div class="diff-row"><div class="d-old">${escapeHtml(
            c.old.text
          )}</div><div class="d-new muted">（已删除）</div></div>`;
        return `<div class="diff-row"><div class="d-old">${diffHtml(
          c.diff,
          "raw"
        )}</div><div class="d-new">${diffHtml(c.diff, "clean")}</div></div>`;
      })
      .join("");
    const more =
      changes.length > 300
        ? `<div class="muted" style="padding:8px 0">仅显示前 300 处，共 ${changes.length} 处差异</div>`
        : "";
    els.diffModal.innerHTML = `
      <div class="modal-card">
        <div class="modal-head"><strong>字幕对比：旧版 → 新版</strong><button class="icon-btn" data-act="close">✕</button></div>
        <div class="modal-body">
          <div class="muted" style="margin-bottom:10px">旧 ${s.total_old ?? "-"} 条 → 新 ${
      s.total_new ?? "-"
    } 条 · 新增 ${s.added ?? 0} · 删除 ${s.removed ?? 0} · 修改 ${s.changed ?? 0} · 相同 ${
      s.equal ?? 0
    }</div>
          ${rows || '<div class="muted" style="padding:24px;text-align:center">两版内容一致</div>'}
          ${more}
        </div>
        <div class="modal-foot">
          <button data-act="merge">合并新旧</button>
          <button data-act="old">保留旧版</button>
          <button class="primary" data-act="new">用新版覆盖</button>
        </div>
      </div>`;
    els.diffModal.classList.remove("hidden");
    els.diffModal.querySelectorAll("button[data-act]").forEach((button) =>
      button.addEventListener("click", () => diffAction(button.dataset.act, against))
    );
  }

  async function diffAction(action, against) {
    if (action === "close" || action === "new") {
      els.diffModal.classList.add("hidden");
      return;
    }
    if (!state.current) return;
    try {
      if (action === "merge") {
        const result = await api("/api/subtitles/merge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.current.path, against }),
        });
        toast(`已合并，共 ${result.count} 条`, "ok");
      } else if (action === "old") {
        await api("/api/backups/restore", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.current.path, name: against }),
        });
        toast("已保留旧版", "ok");
      }
      await Promise.all([loadCues(), refreshStatus()]);
    } catch (err) {
      toast("操作失败：" + err.message, "error");
    }
    els.diffModal.classList.add("hidden");
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
      )}<br><span class="muted">运行一次「全流程生成字幕」后即可查看轨迹。</span></div>`;
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
    const llm = data.llm || {};
    const lt = llm.totals || {};
    const videoPath = data.video || (state.current && state.current.path) || "";
    const asrBackend = params.asr_backend || (data.asr && data.asr.backend) || "whisper";
    const asrEngine =
      params.asr_engine || (data.asr && data.asr.engine) || (asrBackend === "whisper" ? params.whisper_model : "");
    const asrLabel = `${
      asrBackend === "tencent" ? "腾讯云" : "whisper"
    }${asrEngine ? " · " + asrEngine : ""}`;

    const chips = [
      `识别片段 <b>${s.segments ?? "-"}</b>`,
      `文本块 <b>${s.blocks ?? "-"}</b>`,
      `LLM 请求 <b>${s.batches ?? "-"}</b>`,
      `字幕 <b>${s.cues ?? "-"}</b>`,
      `<span class="del">删除 ${s.deleted_chars ?? 0} 字</span>`,
      `<span class="ins">新增/改写 ${s.inserted_chars ?? 0} 字</span>`,
      `模型 ${escapeHtml(params.model || "未使用 LLM")}`,
      `每句 ≤ ${params.max_chars ?? "-"} 字`,
      `截图 ${(ctx.frames || []).length} 张`,
    ];
    if (data.recovery) {
      chips.push(`补齐 <b>${(data.recovery.regions || []).length}</b> 处`);
    }
    if (lt.calls) {
      chips.push(`LLM <b>${lt.calls}</b> 次`);
      chips.push(`${lt.total_tokens || 0} tokens`);
      chips.push(`${lt.seconds || 0}s`);
    }

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
      ["语音识别", asrLabel || "—"],
    ]
      .map(([k, v]) => `<div><b>${k}：</b>${escapeHtml(String(v))}</div>`)
      .join("");

    const glossary = (ctx.glossary || [])
      .map((g) => {
        const note = g.note ? ` <span class="muted">${escapeHtml(g.note)}</span>` : "";
        return `<span class="gloss-item">${escapeHtml(g.term)}${note}</span>`;
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

    let llmCard = "";
    if (llm.calls && llm.calls.length) {
      const rows = llm.calls
        .map((call, i) => {
          const u = call.usage || {};
          const stage =
            call.stage === "digest" ? "资料摘要(digest)" : `清洗 batch #${call.batch}`;
          const tok =
            `in ${u.prompt_tokens ?? "—"} / out ${u.completion_tokens ?? "—"}` +
            (u.reasoning_tokens ? ` / 思考 ${u.reasoning_tokens}` : "") +
            (u.cached_tokens ? ` / 缓存 ${u.cached_tokens}` : "");
          const note = call.error
            ? `<span class="d-del">${escapeHtml(call.error)}</span>`
            : call.cached
            ? "缓存命中（未调用）"
            : `${call.finish_reason || ""}${call.attempts > 1 ? ` · 重试 ${call.attempts - 1} 次` : ""}${
                call.attached_images ? ` · ${call.attached_images} 张截图` : ""
              }`;
          return `<div class="llm-row"><span class="lidx">${i + 1}</span><span class="lstage">${escapeHtml(
            stage
          )}</span><span class="ltok">${tok}</span><span class="ltime">${call.elapsed ?? 0}s</span><span class="lnote">${note}</span></div>`;
        })
        .join("");
      llmCard = `<details class="trace-context">
        <summary>🤖 LLM 调用：${lt.calls} 次 · ${lt.total_tokens || 0} tokens · ${
        lt.seconds || 0
      }s${lt.failed ? ` · 失败 ${lt.failed}` : ""}</summary>
        <div class="tc-body">
          <div class="trace-kv"><b>模型：</b>${escapeHtml(
            String(llm.model || "—")
          )} · <b>输入/输出：</b>${lt.prompt_tokens || 0} / ${
        lt.completion_tokens || 0
      } tokens${lt.reasoning_tokens ? ` · 思考 ${lt.reasoning_tokens}` : ""}${
        lt.cached_tokens ? ` · 缓存命中 ${lt.cached_tokens}` : ""
      }</div>
          <div class="llm-table">${rows}</div>
        </div>
      </details>`;
    }

    let recoveryCard = "";
    if (data.recovery && (data.recovery.regions || []).length) {
      const rr = data.recovery.stats || {};
      const rp = data.recovery.params || {};
      const rows = (data.recovery.regions || [])
        .map((r) => {
          const kind = { gap: "时间缺口", sparse: "稀疏片段", low_conf: "低置信" }[r.kind] || r.kind;
          const rec = r.recovered_text
            ? escapeHtml(r.recovered_text)
            : '<span class="muted">未补回</span>';
          const orig = r.original_text
            ? `<div class="muted">原文：${escapeHtml(r.original_text)}</div>`
            : "";
          return `<div class="tb-cue"><span class="t">${formatTime(r.start)}</span><span class="x"><span class="chip">${kind}</span> ${rec}${orig}</span></div>`;
        })
        .join("");
      recoveryCard = `<details class="trace-context">
        <summary>🩹 漏识别补齐：补回 ${rr.recovered_segments || 0} 段 / ${rr.recovered_chars || 0} 字（共 ${data.recovery.regions.length} 处可疑区间）</summary>
        <div class="tc-body">
          <div class="trace-kv"><b>重转设置：</b>最短缺口 ${rp.min_gap ?? "—"}s · 语速下限 ${rp.min_cps ?? "—"} 字/s · 低置信阈值 ${rp.low_conf_threshold ?? "—"} · 模型 ${escapeHtml(String(rp.model || "—"))}</div>
          <div class="tb-cues">${rows}</div>
        </div>
      </details>`;
    }

    const allBlocks = data.blocks || [];
    const renderBlocks = (list) =>
      list.map((block) => {
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
            <summary>查看实际请求（batch #${block.batch}，附 ${batch.attached_images || 0} 张截图${
            batch.elapsed ? `，${batch.elapsed}s` : ""
          }${
            batch.usage && batch.usage.total_tokens ? `，${batch.usage.total_tokens} tokens` : ""
          }）</summary>
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
              <div class="tb-pane"><div class="tb-label">识别原文（删除/改写为红色）</div><div class="tb-text">${diffHtml(block.diff, "raw")}</div></div>
              <div class="tb-pane"><div class="tb-label">LLM 清洗后（新增/改写为绿/黄色）</div><div class="tb-text">${diffHtml(block.diff, "clean")}</div></div>
            </div>
            ${block.alt_text ? `<div class="tb-pane" style="margin-top:8px"><div class="tb-label">另一版识别（alt，供 LLM 参考）</div><div class="tb-text">${escapeHtml(block.alt_text)}</div></div>` : ""}
            <div class="tb-cues">${cueList || '<span class="muted">（本块被全部删除）</span>'}</div>
            ${request}
          </div>
        </details>`;
      })
      .join("");

    // 文本块可能上千个，一次性渲染会卡死浏览器：每次只渲染 BLOCK_LIMIT 个，点按钮再渲染下一批
    const BLOCK_LIMIT = 200;
    const renderChunk = (start) => renderBlocks(allBlocks.slice(start, start + BLOCK_LIMIT));
    let rendered = 0;

    els.trajectoryView.innerHTML = `
      <div class="trace-summary">${chips.map((c) => `<span class="chip">${c}</span>`).join("")}</div>
      ${llmCard}
      ${contextCard}
      ${recoveryCard}
      ${allBlocks.length ? renderChunk(0) : '<div class="trace-empty">没有文本块</div>'}
      <div class="trace-empty" id="traceMoreWrap"></div>`;
    rendered = Math.min(BLOCK_LIMIT, allBlocks.length);

    const refreshMore = () => {
      const wrap = els.trajectoryView.querySelector("#traceMoreWrap");
      if (!wrap) return;
      if (rendered >= allBlocks.length) {
        wrap.innerHTML = "";
        return;
      }
      wrap.innerHTML = `<button id="traceMoreBtn">显示其余 ${allBlocks.length - rendered} 个文本块</button>`;
      wrap.querySelector("#traceMoreBtn").addEventListener("click", () => {
        const container = document.createElement("div");
        container.innerHTML = renderChunk(rendered);
        rendered += BLOCK_LIMIT;
        wrap.parentElement.insertBefore(container, wrap);
        refreshMore();
      });
    };
    refreshMore();
  }

  // ---------------------------------------------------------------- 上下文 / 资料
  function glossaryToText(list) {
    return (list || [])
      .map((g) => {
        let line = g.term || "";
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
        let term = line;
        let note = "";
        for (const sep of ["|", "="]) {
          const index = line.indexOf(sep);
          if (index >= 0) {
            term = line.slice(0, index).trim();
            note = line.slice(index + 1).trim();
            break;
          }
        }
        return { term, note };
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
      els.ctxFrameInterval.value = eff.frame_interval || state.config.frame_interval;
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
      frame_interval: Number(els.ctxFrameInterval.value) || state.config.frame_interval,
    };
  }

  async function startJob() {
    if (!state.current) return toast("请先选择视频", "error");
    await flushSave();
    state.preGenHadSrt = state.cues.length > 0;
    try {
      const job = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: state.current.path,
          steps: ["extract_audio", "transcribe", "recover", "frames", "clean"],
          ...jobOptions(),
        }),
      });
      state.jobId = job.id;
      els.progressBox.classList.remove("hidden");
      els.jobLog.textContent = "";
      setButtonsBusy(true);
      updateJob(job);
      pollJob();
    } catch (err) {
      if (String(err.message).includes("已有任务在运行")) {
        toast("该视频已有任务在运行，正在显示进度");
        await resumeJob();
      } else {
        toast("启动失败：" + err.message, "error");
      }
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
          if (!els.backupPanel.classList.contains("hidden")) loadBackups();
          if (state.preGenHadSrt) showDiffModal();
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
    els.btnFull.disabled = busy;
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
      localStorage.setItem("subtitlesPlaybackRate", String(rate));
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
    // 只在播放时每帧刷新，暂停时交给 timeupdate/seeked 事件，避免空闲时白跑
    if (state.cues.length && !els.player.paused) updateActiveCue(videoTime());
    requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------- 事件绑定
  els.refreshVideos.addEventListener("click", loadVideos);
  els.search.addEventListener("input", renderVideoList);
  els.btnSettings.addEventListener("click", openSettings);
  els.settingsModal.addEventListener("click", (event) => {
    if (event.target === els.settingsModal) {
      closeSettings();
      return;
    }
    const toggle = event.target.closest("[data-toggle-pw]");
    if (toggle) {
      const input = toggle.parentElement.querySelector("input");
      input.type = input.type === "password" ? "text" : "password";
      toggle.textContent = input.type === "password" ? "显示" : "隐藏";
      return;
    }
    const action = event.target.closest("[data-act]");
    if (!action) return;
    if (action.dataset.act === "close") closeSettings();
    else if (action.dataset.act === "save") saveSettings();
  });
  els.btnFull.addEventListener("click", startJob);
  els.btnHistory.addEventListener("click", toggleHistory);
  els.btnSaveConfig.addEventListener("click", saveVideoConfig);
  els.btnPreviewCtx.addEventListener("click", previewContext);
  els.btnClearCache.addEventListener("click", clearContextCache);
  els.btnReuse.addEventListener("click", reuseConfig);
  els.btnExportConfig.addEventListener("click", exportConfig);
  els.btnGenChapters.addEventListener("click", generateChapters);
  els.btnCopyChapters.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(els.chapterList.textContent);
      toast("章节已复制", "ok");
    } catch (_) {
      toast("复制失败，请手动选择", "error");
    }
  });
  els.btnDownloadChapters.addEventListener("click", () => {
    if (state.current) {
      window.location.href = `/api/chapters/download?path=${encodeURIComponent(
        state.current.path
      )}`;
    }
  });
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
  [els.cfgTitle, els.ctxContext, els.ctxGlossary, els.ctxFrameInterval]
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
  els.btnAddCue.addEventListener("click", addCueAtPlayhead);
  els.btnUndo.addEventListener("click", undo);
  els.playbackRate.addEventListener("change", () => setRate(els.playbackRate.value, true));
  els.player.addEventListener("loadedmetadata", () => {
    els.player.playbackRate = Number(els.playbackRate.value) || 1;
    updateActiveCue(videoTime(), true);
  });
  els.player.addEventListener("timeupdate", () => {
    if (state.cues.length) updateActiveCue(videoTime());
  });
  els.player.addEventListener("seeked", () => {
    if (state.cues.length) updateActiveCue(videoTime(), true);
  });
  try {
    const savedRate = parseFloat(localStorage.getItem("subtitlesPlaybackRate") || "1");
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
      saveCues(false);
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
    if (event.key === "Escape" && !els.settingsModal.classList.contains("hidden")) {
      closeSettings();
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
      let saved = null;
      try {
        saved = localStorage.getItem("subtitlesSelected");
      } catch (_) {}
      if (saved && state.videos.some((v) => v.path === saved)) {
        await selectVideo(saved);
      }
    } catch (err) {
      toast("初始化失败：" + err.message, "error");
    }
  })();
})();
