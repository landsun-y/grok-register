(function () {
  const state = {
    runtime: null,
  };

  const settingsFormEl = document.getElementById("settingsForm");
  const settingsMessageEl = document.getElementById("settingsMessage");
  const toggleSettingsBtnEl = document.getElementById("toggleSettingsBtn");
  const healthRefreshBtnEl = document.getElementById("healthRefreshBtn");
  const healthGridEl = document.getElementById("healthGrid");
  const healthMetaEl = document.getElementById("healthMeta");
  const controllerSummaryEl = document.getElementById("controllerSummary");
  const consoleOutputEl = document.getElementById("consoleOutput");
  const startBtnEl = document.getElementById("startBtn");
  const stopBtnEl = document.getElementById("stopBtn");
  const manualCheckBtnEl = document.getElementById("manualCheckBtn");

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function statusClass(status) {
    return `status-pill status-${status || "unknown"}`;
  }

  function healthClass(ok) {
    return ok ? "health-pill health-ok" : "health-pill health-bad";
  }

  function setDefaults() {
    const defaults = window.__DEFAULTS__ || {};
    const controller = defaults.controller || {};
    settingsFormEl.elements.proxy.value = defaults.proxy || "";
    settingsFormEl.elements.browser_proxy.value = defaults.browser_proxy || "";
    settingsFormEl.elements.temp_mail_api_base.value = defaults.temp_mail_api_base || "";
    settingsFormEl.elements.temp_mail_admin_password.value = defaults.temp_mail_admin_password || "";
    settingsFormEl.elements.temp_mail_domain.value = defaults.temp_mail_domain || "";
    settingsFormEl.elements.temp_mail_site_password.value = defaults.temp_mail_site_password || "";
    settingsFormEl.elements.api_endpoint.value = defaults.api?.endpoint || "";
    settingsFormEl.elements.api_token.value = defaults.api?.token || "";
    settingsFormEl.elements.api_append.checked = defaults.api?.append !== false;
    settingsFormEl.elements.concurrency.value = controller.concurrency || 1;
    settingsFormEl.elements.auto_refill_enabled.checked = Boolean(controller.auto_refill_enabled);
    settingsFormEl.elements.start_threshold.value = controller.start_threshold || 20;
    settingsFormEl.elements.stop_threshold.value = controller.stop_threshold || 50;
    settingsFormEl.elements.push_batch_size.value = controller.push_batch_size || 10;
    settingsFormEl.elements.poll_interval_sec.value = controller.poll_interval_sec || 30;
  }

  function renderHealth(data) {
    const items = data.items || [];
    healthMetaEl.textContent = `最近检测时间 ${data.checked_at || "-"}`;
    if (!items.length) {
      healthGridEl.innerHTML = '<div class="empty">暂无健康检查结果</div>';
      return;
    }
    healthGridEl.innerHTML = items.map((item) => `
      <div class="health-card">
        <div class="task-row">
          <strong>${escapeHtml(item.label)}</strong>
          <span class="${healthClass(item.ok)}">${item.ok ? "正常" : "异常"}</span>
        </div>
        <div class="health-summary">${escapeHtml(item.summary || "-")}</div>
        <div class="health-target">${escapeHtml(item.target || "-")}</div>
        <div class="health-detail">${escapeHtml(item.detail || "-")}</div>
      </div>
    `).join("");
  }

  function renderRuntime(runtime) {
    if (!runtime) {
      controllerSummaryEl.innerHTML = '<div class="empty">暂无控制器状态</div>';
      return;
    }
    state.runtime = runtime;
    controllerSummaryEl.innerHTML = [
      ["状态", `<span class="${statusClass(runtime.status)}">${escapeHtml(runtime.status)}</span>`],
      ["远端账号数", runtime.remote_token_count],
      ["活跃 worker", runtime.current_running_workers],
      ["累计成功", runtime.completed_count],
      ["累计失败", runtime.failed_count],
      ["已完成轮次", runtime.current_round],
      ["待推送数", runtime.pending_token_count],
      ["累计推送数", runtime.total_pushed_count],
      ["当前阶段", runtime.current_phase || "-"],
      ["最近邮箱", runtime.last_email || "-"],
      ["最近错误", runtime.last_error || "-"],
      ["最近检测", runtime.last_check_at || "-"],
      ["最近推送", runtime.last_push_at || "-"],
      ["推送结果", runtime.last_push_result || "-"],
      ["最近开始", runtime.last_started_at || "-"],
      ["最近停止", runtime.last_stopped_at || "-"],
    ].map(([label, value]) => `
      <div class="summary-item">
        <div class="meta-item-label">${escapeHtml(label)}</div>
        <div class="meta-item-value">${typeof value === "string" && value.includes("status-pill") ? value : escapeHtml(value)}</div>
      </div>
    `).join("");
    startBtnEl.disabled = runtime.status === "manual_running" || runtime.status === "auto_running";
    stopBtnEl.disabled = runtime.status === "idle" || runtime.status === "auto_idle";
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Request failed");
    }
    return data;
  }

  async function refreshHealth() {
    try {
      healthMetaEl.textContent = "检测中...";
      const data = await fetchJson("/api/health");
      renderHealth(data);
    } catch (error) {
      healthMetaEl.textContent = `检测失败: ${error.message}`;
      healthGridEl.innerHTML = '<div class="empty">健康检查失败</div>';
    }
  }

  async function refreshController() {
    const data = await fetchJson("/api/controller");
    renderRuntime(data.runtime || null);
  }

  async function refreshLogs() {
    const data = await fetchJson("/api/controller/logs?limit=400");
    consoleOutputEl.innerHTML = escapeHtml((data.lines || []).join("\n"));
    consoleOutputEl.scrollTop = consoleOutputEl.scrollHeight;
  }

  async function refreshAll() {
    try {
      await refreshController();
      await refreshLogs();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  }

  settingsFormEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      proxy: settingsFormEl.elements.proxy.value.trim(),
      browser_proxy: settingsFormEl.elements.browser_proxy.value.trim(),
      temp_mail_api_base: settingsFormEl.elements.temp_mail_api_base.value.trim(),
      temp_mail_admin_password: settingsFormEl.elements.temp_mail_admin_password.value.trim(),
      temp_mail_domain: settingsFormEl.elements.temp_mail_domain.value.trim(),
      temp_mail_site_password: settingsFormEl.elements.temp_mail_site_password.value.trim(),
      api_endpoint: settingsFormEl.elements.api_endpoint.value.trim(),
      api_token: settingsFormEl.elements.api_token.value.trim(),
      api_append: settingsFormEl.elements.api_append.checked,
      concurrency: Number(settingsFormEl.elements.concurrency.value),
      auto_refill_enabled: settingsFormEl.elements.auto_refill_enabled.checked,
      start_threshold: Number(settingsFormEl.elements.start_threshold.value),
      stop_threshold: Number(settingsFormEl.elements.stop_threshold.value),
      push_batch_size: Number(settingsFormEl.elements.push_batch_size.value),
      poll_interval_sec: Number(settingsFormEl.elements.poll_interval_sec.value),
    };
    try {
      const data = await fetchJson("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      window.__DEFAULTS__ = data.defaults || window.__DEFAULTS__;
      settingsMessageEl.textContent = "默认配置已保存";
      settingsMessageEl.className = "form-message success";
      setDefaults();
      await refreshHealth();
      await refreshAll();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  });

  toggleSettingsBtnEl.addEventListener("click", () => {
    settingsFormEl.classList.toggle("hidden");
    toggleSettingsBtnEl.textContent = settingsFormEl.classList.contains("hidden") ? "展开系统默认配置" : "收起系统默认配置";
  });

  startBtnEl.addEventListener("click", async () => {
    try {
      await fetchJson("/api/controller/start", { method: "POST" });
      await refreshAll();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  });

  stopBtnEl.addEventListener("click", async () => {
    try {
      await fetchJson("/api/controller/stop", { method: "POST" });
      await refreshAll();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  });

  manualCheckBtnEl.addEventListener("click", async () => {
    try {
      await fetchJson("/api/controller/check", { method: "POST" });
      await refreshAll();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  });

  healthRefreshBtnEl.addEventListener("click", refreshHealth);

  setDefaults();
  refreshHealth();
  refreshAll();
  window.setInterval(refreshAll, 2500);
  window.setInterval(refreshHealth, 15000);
})();
