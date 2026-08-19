function el(id) {
  return document.getElementById(id);
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

function formatDate(iso) {
  if (!iso || iso === "-") return "-";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
      d.getHours()
    )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch {
    return iso;
  }
}

function setLoading(button, loading) {
  const original = button.dataset.originalText || button.innerHTML;
  if (loading) {
    button.dataset.originalText = original;
    button.disabled = true;
    button.innerHTML = '<span class="loading"></span><span>处理中…</span>';
  } else {
    button.disabled = false;
    button.innerHTML = original;
  }
}

async function loadStatus() {
  const statusText = el("statusText");
  const expireText = el("expireText");
  const modeText = el("modeText");
  const enterBtn = el("enterBtn");

  statusText.textContent = "检查中…";
  statusText.className = "value warn";

  try {
    const data = await api("/api/license/status");
    const status = data.status;

    if (status === "valid") {
      statusText.textContent = "授权有效";
      statusText.className = "value ok";
      expireText.textContent = formatDate(data.expires_at);
      modeText.textContent = "已授权";
      enterBtn.disabled = false;
    } else if (status === "expired") {
      statusText.textContent = "授权已到期,请联系管理员续期";
      statusText.className = "value danger";
      expireText.textContent = formatDate(data.expires_at) || "-";
      modeText.textContent = "未授权";
      enterBtn.disabled = true;
    } else if (status === "invalid_device") {
      statusText.textContent = "授权与当前设备不匹配";
      statusText.className = "value danger";
      expireText.textContent = "-";
      modeText.textContent = "未授权";
      enterBtn.disabled = true;
    } else {
      statusText.textContent = data.message || "未授权";
      statusText.className = "value danger";
      expireText.textContent = "-";
      modeText.textContent = "未授权";
      enterBtn.disabled = true;
    }
  } catch (error) {
    statusText.textContent = "无法获取授权状态";
    statusText.className = "value danger";
    expireText.textContent = "-";
    modeText.textContent = "未授权";
    enterBtn.disabled = true;
    console.error(error);
  }
}

async function activate() {
  const code = el("licenseCode").value.trim();
  const errorEl = el("formError");
  const btn = el("activateBtn");

  errorEl.textContent = "";
  if (!code) {
    errorEl.textContent = "请输入授权码";
    return;
  }

  setLoading(btn, true);
  try {
    await api("/api/license/activate", {
      method: "POST",
      body: { code },
    });
    toast("激活成功");
    await loadStatus();
    enterApp();
  } catch (error) {
    errorEl.textContent = error.message;
    toast(error.message, true);
  } finally {
    setLoading(btn, false);
  }
}

async function refreshLicense() {
  const btn = el("recheckBtn");
  setLoading(btn, true);
  try {
    await api("/api/license/refresh", { method: "POST" });
    toast("授权状态已刷新");
    await loadStatus();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setLoading(btn, false);
  }
}

function enterApp() {
  window.location.href = "/";
}

function bindEvents() {
  el("activateForm").addEventListener("submit", (event) => {
    event.preventDefault();
    activate();
  });

  el("showCode").addEventListener("change", (event) => {
    el("licenseCode").type = event.target.checked ? "text" : "password";
  });

  el("recheckBtn").addEventListener("click", refreshLicense);
  el("enterBtn").addEventListener("click", enterApp);
  el("closeBtn").addEventListener("click", () => {
    if (window.close) window.close();
    toast("请激活后使用本软件");
  });
}

async function init() {
  bindEvents();
  await loadStatus();
}

init();
