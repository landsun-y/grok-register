(function () {
  const state = {
    runtime: null,
  };

  const page = document.body.dataset.page || "controller";
  const settingsFormEl = document.getElementById("settingsForm");
  const settingsMessageEl = document.getElementById("settingsMessage");
  const healthRefreshBtnEl = document.getElementById("healthRefreshBtn");
  const healthGridEl = document.getElementById("healthGrid");
  const healthMetaEl = document.getElementById("healthMeta");
  const controllerSummaryEl = document.getElementById("controllerSummary");
  const controllerMessageEl = document.getElementById("controllerMessage");
  const consoleOutputEl = document.getElementById("consoleOutput");
  const masterToggleBtnEl = document.getElementById("masterToggleBtn");
  const manualCheckBtnEl = document.getElementById("manualCheckBtn");
  const toggleControllerSettingsBtnEl = document.getElementById("toggleControllerSettingsBtn");
  const controllerSettingsFormEl = document.getElementById("controllerSettingsForm");
  const controllerSettingsModalEl = document.getElementById("controllerSettingsModal");
  const controllerSettingsBackdropEl = document.getElementById("controllerSettingsBackdrop");
  const closeControllerSettingsBtnEl = document.getElementById("closeControllerSettingsBtn");

  function escapeHtml(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value)
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

  function defaults() {
    return window.__DEFAULTS__ || {};
  }

  function controllerDefaults() {
    return defaults().controller || {};
  }

  function setSettingsDefaults() {
    if (!settingsFormEl) {
      return;
    }
    const conf = defaults();
    settingsFormEl.elements.proxy.value = conf.proxy || "";
    settingsFormEl.elements.browser_proxy.value = conf.browser_proxy || "";
    settingsFormEl.elements.temp_mail_api_base.value = conf.temp_mail_api_base || "";
    settingsFormEl.elements.temp_mail_admin_password.value = conf.temp_mail_admin_password || "";
    settingsFormEl.elements.temp_mail_domain.value = conf.temp_mail_domain || "";
    settingsFormEl.elements.temp_mail_site_password.value = conf.temp_mail_site_password || "";
    settingsFormEl.elements.api_endpoint.value = conf.api?.endpoint || "";
    settingsFormEl.elements.api_token.value = conf.api?.token || "";
    settingsFormEl.elements.api_append.checked = conf.api?.append !== false;
    settingsFormEl.elements.push_batch_size.value = controllerDefaults().push_batch_size ?? 10;
    settingsFormEl.elements.poll_interval_sec.value = controllerDefaults().poll_interval_sec ?? 30;
  }

  function setControllerDefaults() {
    if (!controllerSettingsFormEl) {
      return;
    }
    const controller = controllerDefaults();
    controllerSettingsFormEl.elements.concurrency.value = controller.concurrency ?? 1;
    controllerSettingsFormEl.elements.auto_refill_enabled.checked = Boolean(controller.auto_refill_enabled);
    controllerSettingsFormEl.elements.start_threshold.value = controller.start_threshold ?? 20;
    controllerSettingsFormEl.elements.stop_threshold.value = controller.stop_threshold ?? 50;
  }

  function setControllerSettingsVisible(visible) {
    if (!controllerSettingsModalEl || !toggleControllerSettingsBtnEl) {
      return;
    }
    controllerSettingsModalEl.classList.toggle("hidden", !visible);
    controllerSettingsModalEl.hidden = !visible;
    toggleControllerSettingsBtnEl.textContent = "设置";
    toggleControllerSettingsBtnEl.setAttribute("aria-expanded", visible ? "true" : "false");
  }

  function renderHealth(data) {
    if (!healthGridEl || !healthMetaEl) {
      return;
    }
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
    if (!controllerSummaryEl) {
      return;
    }
    if (!runtime) {
      controllerSummaryEl.innerHTML = '<div class="empty">暂无控制器状态</div>';
      return;
    }
    state.runtime = runtime;
    const controller = controllerDefaults();
    const summaryRows = [
      [["状态", `<span class="${statusClass(runtime.status)}">${escapeHtml(runtime.status)}</span>`]],
      [["远端账号数", runtime.remote_token_count], ["活跃 worker", runtime.current_running_workers]],
      [["累计成功", runtime.completed_count], ["累计失败", runtime.failed_count]],
      [["是否自动补号", controller.auto_refill_enabled ? "开启" : "关闭"]],
      [["启动阈值", controller.start_threshold ?? 20], ["停止阈值", controller.stop_threshold ?? 50]],
    ];
    controllerSummaryEl.innerHTML = summaryRows.map((row) => `
      <div class="summary-row summary-row-cols-${row.length}">
        ${row.map(([label, value]) => `
          <div class="summary-item">
            <div class="meta-item-label">${escapeHtml(label)}</div>
            <div class="meta-item-value">${typeof value === "string" && value.includes("status-pill") ? value : escapeHtml(value)}</div>
          </div>
        `).join("")}
      </div>
    `).join("");

    if (masterToggleBtnEl) {
      masterToggleBtnEl.textContent = runtime.controller_enabled ? "停止" : "开始";
      masterToggleBtnEl.className = runtime.controller_enabled ? "button button-danger" : "button";
    }
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
    if (!healthMetaEl) {
      return;
    }
    try {
      healthMetaEl.textContent = "检测中...";
      const data = await fetchJson("/api/health");
      renderHealth(data);
    } catch (error) {
      healthMetaEl.textContent = `检测失败: ${error.message}`;
      if (healthGridEl) {
        healthGridEl.innerHTML = '<div class="empty">健康检查失败</div>';
      }
    }
  }

  async function refreshController() {
    if (!controllerSummaryEl) {
      return;
    }
    const data = await fetchJson("/api/controller");
    window.__DEFAULTS__ = data.defaults || window.__DEFAULTS__;
    setControllerDefaults();
    renderRuntime(data.runtime || null);
  }

  async function refreshLogs() {
    if (!consoleOutputEl) {
      return;
    }
    const data = await fetchJson("/api/controller/logs?limit=400");
    consoleOutputEl.innerHTML = escapeHtml((data.lines || []).join("\n"));
    consoleOutputEl.scrollTop = consoleOutputEl.scrollHeight;
  }

  async function refreshAll() {
    if (page !== "controller") {
      return;
    }
    try {
      await refreshController();
      await refreshLogs();
    } catch (error) {
      if (controllerMessageEl) {
        controllerMessageEl.textContent = error.message;
        controllerMessageEl.className = "form-message error";
      }
    }
  }

  if (settingsFormEl) {
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
        setSettingsDefaults();
        await refreshHealth();
      } catch (error) {
        settingsMessageEl.textContent = error.message;
        settingsMessageEl.className = "form-message error";
      }
    });
  }

  if (controllerSettingsFormEl) {
    controllerSettingsFormEl.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = {
        concurrency: Number(controllerSettingsFormEl.elements.concurrency.value),
        auto_refill_enabled: controllerSettingsFormEl.elements.auto_refill_enabled.checked,
        start_threshold: Number(controllerSettingsFormEl.elements.start_threshold.value),
        stop_threshold: Number(controllerSettingsFormEl.elements.stop_threshold.value),
      };
      try {
        const data = await fetchJson("/api/controller/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        window.__DEFAULTS__ = data.defaults || window.__DEFAULTS__;
        setControllerDefaults();
        renderRuntime(data.runtime || state.runtime);
        controllerMessageEl.textContent = "控制设置已保存";
        controllerMessageEl.className = "form-message success";
        setControllerSettingsVisible(false);
      } catch (error) {
        controllerMessageEl.textContent = error.message;
        controllerMessageEl.className = "form-message error";
      }
    });
  }

  if (toggleControllerSettingsBtnEl && controllerSettingsModalEl) {
    toggleControllerSettingsBtnEl.addEventListener("click", () => {
      const nextVisible = controllerSettingsModalEl.classList.contains("hidden") || controllerSettingsModalEl.hidden;
      setControllerSettingsVisible(nextVisible);
    });
  }

  if (closeControllerSettingsBtnEl) {
    closeControllerSettingsBtnEl.addEventListener("click", () => setControllerSettingsVisible(false));
  }

  if (controllerSettingsBackdropEl) {
    controllerSettingsBackdropEl.addEventListener("click", () => setControllerSettingsVisible(false));
  }

  if (masterToggleBtnEl) {
    masterToggleBtnEl.addEventListener("click", async () => {
      const shouldStart = !state.runtime?.controller_enabled;
      try {
        const url = shouldStart ? "/api/controller/start" : "/api/controller/stop";
        const data = await fetchJson(url, { method: "POST" });
        renderRuntime(data.runtime || null);
        await refreshAll();
      } catch (error) {
        controllerMessageEl.textContent = error.message;
        controllerMessageEl.className = "form-message error";
      }
    });
  }

  if (manualCheckBtnEl) {
    manualCheckBtnEl.addEventListener("click", async () => {
      try {
        await fetchJson("/api/controller/check", { method: "POST" });
        await refreshAll();
      } catch (error) {
        controllerMessageEl.textContent = error.message;
        controllerMessageEl.className = "form-message error";
      }
    });
  }

  if (healthRefreshBtnEl) {
    healthRefreshBtnEl.addEventListener("click", refreshHealth);
  }

  setSettingsDefaults();
  setControllerDefaults();
  setControllerSettingsVisible(false);
  if (page === "settings") {
    refreshHealth();
    window.setInterval(refreshHealth, 15000);
  } else {
    refreshAll();
    window.setInterval(refreshAll, 2500);
  }
})();
