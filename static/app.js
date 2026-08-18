const state = {
  config: { configured: false, api_id: null, api_hash_configured: false },
  accounts: [],
  login: { phone: null, stage: "code" },
  share: { accountId: null, dialogs: [], selected: new Set() },
  groups: { accountId: null, dialogs: [] },
  taskId: null,
  pollTimer: null,
};

function el(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        ch
      ],
  );
}

function kindLabel(kind) {
  return { private: "私聊", group: "群组", channel: "频道" }[kind] || kind;
}

function toast(message, isError = false) {
  const node = el("toast");
  node.textContent = message;
  node.className = "toast" + (isError ? " error" : "");
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => {
    node.hidden = true;
  }, 3400);
}

async function api(path, options = {}) {
  const opts = {
    headers: { "Content-Type": "application/json" },
    ...options,
  };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
  }
  // 45 秒请求超时兜底，避免后端偶发挂起导致界面毫无反应
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  let response;
  try {
    response = await fetch(path, { ...opts, signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("请求超时，请稍后重试");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const message =
      data && data.detail ? data.detail : `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return data;
}

function updateAuthBadge() {
  const badge = el("authBadge");
  badge.textContent = state.config.configured ? "授权已配置" : "未配置";
  badge.className =
    "badge " + (state.config.configured ? "badge-ok" : "badge-muted");
}

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  el("apiId").value = config.api_id || "";
  el("apiHash").value = "";
  el("apiHash").placeholder = config.api_hash_configured
    ? "已配置，留空保持不变"
    : "请输入 api_hash";
  el("proxyEnabled").checked = !!config.proxy_enabled;
  el("proxyType").value = config.proxy_type || "socks5";
  el("proxyHost").value = config.proxy_host || "";
  el("proxyPort").value = config.proxy_port || "";
  updateAuthBadge();
}

async function loadAccounts() {
  const data = await api("/api/accounts");
  state.accounts = data.accounts || [];
  renderAccounts();
  renderAccountSelectors();
}

function accountStatusBadge(account) {
  if (!account.has_session) {
    return '<span class="badge badge-muted">无会话</span>';
  }
  if (account.status === "valid") {
    return '<span class="badge badge-ok">正常</span>';
  }
  if (account.status === "invalid") {
    return '<span class="badge badge-danger">失效</span>';
  }
  return '<span class="badge badge-muted">未验证</span>';
}

function renderAccounts() {
  const body = el("accountsBody");
  if (!state.accounts.length) {
    body.innerHTML = '<tr><td colspan="3" class="empty">暂无账号</td></tr>';
    return;
  }
  body.innerHTML = state.accounts
    .map(
      (account) => `
        <tr>
          <td>${escapeHtml(account.phone)}</td>
          <td>${accountStatusBadge(account)}</td>
          <td>
            <div class="row-actions">
              <button class="btn btn-secondary" data-action="verify" data-id="${
                account.id
              }">验证</button>
              <button class="btn btn-danger" data-action="remove" data-id="${
                account.id
              }">移除</button>
            </div>
          </td>
        </tr>`,
    )
    .join("");
}

function renderAccountSelectors() {
  const options = state.accounts
    .map(
      (account) =>
        `<option value="${account.id}" ${String(account.id) === String(state.share.accountId) ? "selected" : ""}>${escapeHtml(account.phone)}</option>`,
    )
    .join("");
  el("shareAccount").innerHTML =
    '<option value="">选择账号</option>' + options;

  const groupOptions = state.accounts
    .map(
      (account) =>
        `<option value="${account.id}" ${String(account.id) === String(state.groups.accountId) ? "selected" : ""}>${escapeHtml(account.phone)}</option>`,
    )
    .join("");
  el("groupsAccount").innerHTML =
    '<option value="">选择账号</option>' + groupOptions;
}

function openCodeDialog() {
  el("codeTitle").textContent = "输入验证码";
  el("codeLabel").textContent = "验证码";
  el("codeHint").textContent = `验证码已发送到 ${state.login.phone}`;
  el("codeInput").type = "text";
  el("codeInput").value = "";
  el("codeError").textContent = "";
  el("codeDialog").showModal();
  el("codeInput").focus();
}

function finishLogin(result) {
  el("codeDialog").close();
  state.login = { phone: null, stage: "code" };
  loadAccounts();
  toast(`账号 ${result.phone} 登录成功`);
}

async function loadDialogs(scope) {
  const accountId =
    scope === "share" ? state.share.accountId : state.groups.accountId;
  const kind =
    scope === "share" ? el("dialogFilter").value : el("groupsFilter").value;
  const search =
    scope === "share" ? el("dialogSearch").value : el("groupsSearch").value;
  const listEl =
    scope === "share" ? el("shareChatList") : el("groupsChatList");

  if (!accountId) {
    listEl.innerHTML = '<div class="empty">请先选择账号</div>';
    return;
  }

  listEl.innerHTML = '<div class="empty">正在拉取会话…</div>';
  try {
    const params = new URLSearchParams({ kind, search });
    const data = await api(
      `/api/accounts/${accountId}/dialogs?${params.toString()}`,
    );
    if (scope === "share") {
      state.share.dialogs = data.dialogs || [];
      state.share.selected = new Set();
      renderShareChats();
    } else {
      state.groups.dialogs = data.dialogs || [];
      renderGroupsChats();
    }
  } catch (error) {
    listEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderShareChats() {
  const box = el("shareChatList");
  const dialogs = state.share.dialogs;
  if (!dialogs.length) {
    box.innerHTML = '<div class="empty">暂无会话，请点击「拉取会话」</div>';
    return;
  }
  box.innerHTML = dialogs
    .map((dialog) => {
      const checked = state.share.selected.has(dialog.key) ? "checked" : "";
      const username = dialog.username
        ? ` · @${escapeHtml(dialog.username)}`
        : "";
      return `
        <label class="chat-item" data-key="${dialog.key}">
          <input type="checkbox" ${checked} data-key="${dialog.key}" />
          <span class="chat-meta">
            <span class="chat-name">${escapeHtml(dialog.name)}</span>
            <span class="chat-kind">${kindLabel(dialog.kind)}${username}</span>
          </span>
        </label>`;
    })
    .join("");

  box.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const key = checkbox.dataset.key;
      if (checkbox.checked) state.share.selected.add(key);
      else state.share.selected.delete(key);
    });
  });
}

function renderGroupsChats() {
  const box = el("groupsChatList");
  const dialogs = state.groups.dialogs;
  if (!dialogs.length) {
    box.innerHTML = '<div class="empty">暂无会话，请点击「拉取会话」</div>';
    return;
  }
  box.innerHTML = dialogs
    .map((dialog) => {
      const username = dialog.username
        ? ` · @${escapeHtml(dialog.username)}`
        : "";
      return `
        <div class="chat-item">
          <span class="chat-meta">
            <span class="chat-name">${escapeHtml(dialog.name)}</span>
            <span class="chat-kind">${kindLabel(dialog.kind)}${username}</span>
          </span>
        </div>`;
    })
    .join("");
}

function selectedTargets() {
  return state.share.dialogs
    .filter((dialog) => state.share.selected.has(dialog.key))
    .map((dialog) => ({
      type: dialog.type,
      id: dialog.id,
      name: dialog.name || "",
      access_hash: dialog.access_hash,
    }));
}

function setRunning(running) {
  el("startShareBtn").disabled = running;
  el("stopShareBtn").disabled = !running;
}

function renderTask(task) {
  const labels = {
    running: "运行中",
    done: "已完成",
    stopped: "已停止",
    error: "异常",
  };
  const percent = task.total
    ? Math.min(100, Math.round((task.done / task.total) * 100))
    : 0;
  const statusColor = {
    running: "var(--accent-cyan)",
    done: "var(--ok)",
    stopped: "var(--warn)",
    error: "var(--danger)",
  }[task.status] || "var(--muted)";
  el("taskStatus").innerHTML = `
    <div class="task-stats">
      <span>状态<strong style="color:${statusColor}">${labels[task.status] || task.status}</strong></span>
      <span>成功<strong style="color:var(--ok)">${task.ok}</strong></span>
      <span>失败<strong style="color:var(--danger)">${task.failed}</strong></span>
      <span>跳过<strong>${task.skipped}</strong></span>
      <span>进度<strong>${percent}%</strong></span>
    </div>
    <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>`;

  el("logBox").innerHTML = (task.logs || [])
    .map(
      (line) =>
        `<div class="log-line ${line.level}"><span>[${line.t}]</span> ${escapeHtml(line.msg)}</div>`,
    )
    .join("");
  el("logBox").scrollTop = el("logBox").scrollHeight;
}

async function pollTask() {
  if (!state.taskId) return;
  try {
    const task = await api(`/api/tasks/${state.taskId}`);
    renderTask(task);
    if (["done", "stopped", "error"].includes(task.status)) {
      setRunning(false);
      state.taskId = null;
      return;
    }
  } catch {
    setRunning(false);
    state.taskId = null;
    return;
  }
  state.pollTimer = setTimeout(pollTask, 1200);
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document
        .querySelectorAll(".tab")
        .forEach((tab) => tab.classList.toggle("active", tab === button));
      document
        .querySelectorAll(".tab-panel")
        .forEach((panel) =>
          panel.classList.toggle("active", panel.id === `tab-${button.dataset.tab}`),
        );
    });
  });

  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog").close());
  });

  el("configBtn").addEventListener("click", () => el("configDialog").showModal());
  el("configDialog")
    .querySelector("form")
    .addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api("/api/config", {
          method: "POST",
          body: {
            api_id: Number(el("apiId").value),
            api_hash: el("apiHash").value.trim(),
            proxy_enabled: el("proxyEnabled").checked,
            proxy_type: el("proxyType").value,
            proxy_host: el("proxyHost").value.trim(),
            proxy_port: el("proxyPort").value.trim(),
          },
        });
        el("configDialog").close();
        await loadConfig();
        toast("API 设置已保存");
      } catch (error) {
        toast(error.message, true);
      }
    });

  el("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const phone = el("loginPhone").value.trim();
    if (!phone) {
      toast("请输入手机号", true);
      return;
    }
    const btn = el("loginForm").querySelector("button[type=submit]");
    const label = btn.querySelector("span");
    const original = label.textContent;
    btn.disabled = true;
    label.textContent = "正在发送…";
    try {
      const result = await api("/api/accounts/login/start", {
        method: "POST",
        body: { phone },
      });
      state.login = { phone: result.phone, stage: "code" };
      openCodeDialog();
    } catch (error) {
      toast(error.message, true);
    } finally {
      btn.disabled = false;
      label.textContent = original;
    }
  });

  el("codeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = el("codeInput").value.trim();
    if (!value) {
      el("codeError").textContent = "请输入验证码";
      return;
    }
    const btn = el("codeForm").querySelector("button[type=submit]");
    const label = btn.querySelector("span");
    const original = label.textContent;
    btn.disabled = true;
    label.textContent = "正在验证…";
    try {
      if (state.login.stage === "2fa") {
        const result = await api("/api/accounts/login/2fa", {
          method: "POST",
          body: { phone: state.login.phone, password: value },
        });
        finishLogin(result);
      } else {
        const result = await api("/api/accounts/login/code", {
          method: "POST",
          body: { phone: state.login.phone, code: value },
        });
        if (result.next === "2fa") {
          state.login.stage = "2fa";
          el("codeTitle").textContent = "两步验证";
          el("codeLabel").textContent = "两步验证密码";
          el("codeHint").textContent = result.hint
            ? `提示：${result.hint}`
            : "该账号需要两步验证密码";
          el("codeInput").type = "password";
          el("codeInput").value = "";
          el("codeError").textContent = "";
          el("codeInput").focus();
        } else {
          finishLogin(result);
        }
      }
    } catch (error) {
      el("codeError").textContent = error.message;
    } finally {
      btn.disabled = false;
      label.textContent = original;
    }
  });

  el("importBtn").addEventListener("click", () => el("importDialog").showModal());
  el("importForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/accounts/import", {
        method: "POST",
        body: {
          phone: el("importPhone").value.trim(),
          session: el("importSession").value.trim(),
        },
      });
      el("importDialog").close();
      el("importPhone").value = "";
      el("importSession").value = "";
      await loadAccounts();
      toast("Session 导入成功");
    } catch (error) {
      el("importError").textContent = error.message;
    }
  });

  el("refreshAccountsBtn").addEventListener("click", () => loadAccounts());

  el("accountsBody").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const accountId = Number(button.dataset.id);
    if (button.dataset.action === "verify") {
      button.disabled = true;
      button.textContent = "验证中…";
      try {
        const result = await api(`/api/accounts/${accountId}/verify`, {
          method: "POST",
        });
        toast(`账号 ${result.phone} 验证成功`);
        await loadAccounts();
      } catch (error) {
        toast(error.message, true);
        await loadAccounts();
      } finally {
        button.disabled = false;
        button.textContent = "验证";
      }
    } else if (button.dataset.action === "remove") {
      if (!window.confirm("确认移除该账号及其本地会话？")) return;
      try {
        await api(`/api/accounts/${accountId}/remove`, { method: "POST" });
        await loadAccounts();
        toast("账号已移除");
      } catch (error) {
        toast(error.message, true);
      }
    }
  });

  el("shareAccount").addEventListener("change", () => {
    state.share.accountId = el("shareAccount").value || null;
    state.share.selected = new Set();
    loadDialogs("share");
  });

  el("groupsAccount").addEventListener("change", () => {
    state.groups.accountId = el("groupsAccount").value || null;
    loadDialogs("groups");
  });

  el("fetchDialogsBtn").addEventListener("click", () => loadDialogs("share"));
  el("fetchGroupsBtn").addEventListener("click", () => loadDialogs("groups"));

  el("dialogFilter").addEventListener("change", () => loadDialogs("share"));
  el("groupsFilter").addEventListener("change", () => loadDialogs("groups"));
  el("dialogSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadDialogs("share");
  });
  el("groupsSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadDialogs("groups");
  });

  el("selectAllBtn").addEventListener("click", () => {
    state.share.dialogs.forEach((dialog) => state.share.selected.add(dialog.key));
    renderShareChats();
  });
  el("invertBtn").addEventListener("click", () => {
    const next = new Set();
    state.share.dialogs.forEach((dialog) => {
      if (!state.share.selected.has(dialog.key)) next.add(dialog.key);
    });
    state.share.selected = next;
    renderShareChats();
  });
  el("clearSelectionBtn").addEventListener("click", () => {
    state.share.selected = new Set();
    renderShareChats();
  });

  el("startShareBtn").addEventListener("click", async () => {
    if (!state.share.accountId) {
      toast("请先选择账号", true);
      return;
    }
    const targets = selectedTargets();
    if (!targets.length) {
      toast("请选择目标会话/群组", true);
      return;
    }
    const numbers = el("numbersInput").value;
    if (!numbers.trim()) {
      toast("请输入要分享的手机号", true);
      return;
    }

    const options = {
      rounds: parseInt(el("rounds").value, 10) || 1,
      interval: parseFloat(el("interval").value) || 0,
      fetch_missing_names: el("fetchNames").checked,
      skip_unresolved: el("skipUnresolved").checked,
      allow_empty_name: el("allowEmpty").checked,
      fallback_first_name: el("fallbackFirst").value.trim(),
      fallback_last_name: el("fallbackLast").value.trim(),
    };

    try {
      const result = await api(`/api/accounts/${state.share.accountId}/share/start`, {
        method: "POST",
        body: { targets, numbers, options },
      });
      state.taskId = result.task_id;
      el("logBox").innerHTML = "";
      setRunning(true);
      pollTask();
      toast("分享任务已启动");
    } catch (error) {
      toast(error.message, true);
    }
  });

  el("stopShareBtn").addEventListener("click", async () => {
    if (!state.taskId) return;
    try {
      await api(`/api/tasks/${state.taskId}/stop`, { method: "POST" });
      toast("已请求停止");
      // 保险:若 10 秒后任务仍未结束,强制解锁界面
      const taskId = state.taskId;
      setTimeout(async () => {
        if (state.taskId !== taskId) return;
        try {
          const task = await api(`/api/tasks/${taskId}`);
          if (task.status === "running") {
            setRunning(false);
            state.taskId = null;
            toast("任务响应超时,已解锁界面(后台任务可能仍在收尾)", true);
          }
        } catch {
          /* 任务已清理则忽略 */
        }
      }, 10000);
    } catch (error) {
      toast(error.message, true);
    }
  });

  el("clearLogBtn").addEventListener("click", () => {
    el("logBox").innerHTML = "";
  });
}

async function init() {
  bindEvents();
  setRunning(false);
  if (window.lucide && typeof window.lucide.createIcons === "function") {
    window.lucide.createIcons();
  }
  try {
    await loadConfig();
    await loadAccounts();
  } catch (error) {
    toast(error.message, true);
  }
}

init();
