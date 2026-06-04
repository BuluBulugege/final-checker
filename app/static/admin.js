/* final-checker · Long-Term Monitor Admin Panel Controller */
"use strict";

const API_BASE = "/api/long-term";
const $ = (id) => document.getElementById(id);

// State
let authToken = null;
let currentPage = 1;
let pageSize = 50;
let filters = {
  platform: "",
  status: "",
  search: "",
};
let selectedKeys = new Set();

// Elements
const els = {
  // Login
  loginScreen: $("loginScreen"),
  loginForm: $("loginForm"),
  password: $("password"),
  loginError: $("loginError"),

  // Main app
  mainApp: $("mainApp"),
  logoutBtn: $("logoutBtn"),

  // Filters
  filterPlatform: $("filterPlatform"),
  filterStatus: $("filterStatus"),
  filterSearch: $("filterSearch"),
  applyFilters: $("applyFilters"),
  clearFilters: $("clearFilters"),

  // Batch actions
  selectAll: $("selectAll"),
  deselectAll: $("deselectAll"),
  batchCheck: $("batchCheck"),
  batchDelete: $("batchDelete"),
  checkAll: $("checkAll"),

  // Move keys
  movePlatform: $("movePlatform"),
  moveKeys: $("moveKeys"),
  moveNotes: $("moveNotes"),
  moveKeysBtn: $("moveKeysBtn"),

  // Keys table
  keysBody: $("keysBody"),
  totalCount: $("totalCount"),
  refreshBtn: $("refreshBtn"),
  selectAllCheckbox: $("selectAllCheckbox"),

  // Pagination
  prevPage: $("prevPage"),
  nextPage: $("nextPage"),
  currentPage: $("currentPage"),
  totalPages: $("totalPages"),
};

// ============================================================
// INIT
// ============================================================

init();

function init() {
  // Check for existing token
  authToken = localStorage.getItem("adminToken");
  if (authToken) {
    showMainApp();
    loadKeys();
  }

  // Login
  els.loginForm.addEventListener("submit", handleLogin);
  els.logoutBtn.addEventListener("click", handleLogout);

  // Filters
  els.applyFilters.addEventListener("click", applyFilters);
  els.clearFilters.addEventListener("click", clearFilters);

  // Batch actions
  els.selectAll.addEventListener("click", selectAllKeys);
  els.deselectAll.addEventListener("click", deselectAllKeys);
  els.batchCheck.addEventListener("click", batchCheckKeys);
  els.batchDelete.addEventListener("click", batchDeleteKeys);
  els.checkAll.addEventListener("click", checkAllKeys);

  // Move keys
  els.moveKeysBtn.addEventListener("click", moveKeys);

  // Refresh
  els.refreshBtn.addEventListener("click", () => loadKeys());

  // Pagination
  els.prevPage.addEventListener("click", () => changePage(-1));
  els.nextPage.addEventListener("click", () => changePage(1));

  // Select all checkbox
  els.selectAllCheckbox.addEventListener("change", (e) => {
    if (e.target.checked) {
      selectAllKeys();
    } else {
      deselectAllKeys();
    }
  });
}

// ============================================================
// AUTH
// ============================================================

async function handleLogin(e) {
  e.preventDefault();
  const password = els.password.value;

  console.log('[DEBUG] handleLogin 开始');
  console.log('[DEBUG] 密码长度:', password.length);

  if (!password) {
    showLoginError("请输入密码");
    return;
  }

  try {
    console.log('[DEBUG] 发送认证请求...');
    const res = await fetch(`${API_BASE}/auth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });

    console.log('[DEBUG] 响应状态:', res.status, res.ok);

    if (!res.ok) {
      const err = await res.json();
      console.log('[DEBUG] 认证失败:', err);
      throw new Error(err.detail || "登录失败");
    }

    const data = await res.json();
    console.log('[DEBUG] 认证成功，token 长度:', data.token?.length);

    authToken = data.token;
    localStorage.setItem("adminToken", authToken);
    console.log('[DEBUG] Token 已保存到 localStorage');

    console.log('[DEBUG] 准备显示主界面...');
    console.log('[DEBUG] loginScreen 元素:', els.loginScreen);
    console.log('[DEBUG] mainApp 元素:', els.mainApp);

    showMainApp();
    console.log('[DEBUG] showMainApp 已调用');

    loadKeys();
    console.log('[DEBUG] loadKeys 已调用');
  } catch (err) {
    console.error('[DEBUG] 登录异常:', err);
    showLoginError(err.message);
  }
}

function handleLogout() {
  authToken = null;
  localStorage.removeItem("adminToken");
  els.loginScreen.style.display = 'flex';
  els.mainApp.style.display = 'none';
  els.password.value = "";
  els.loginError.textContent = "";
}

function showLoginError(msg) {
  els.loginError.textContent = msg;
}

function showMainApp() {
  console.log('[DEBUG] showMainApp 开始');
  console.log('[DEBUG] loginScreen.hidden 设置前:', els.loginScreen.hidden);
  console.log('[DEBUG] mainApp.hidden 设置前:', els.mainApp.hidden);

  // 使用 display style 而不是 hidden 属性，更可靠
  els.loginScreen.style.display = 'none';
  els.mainApp.style.display = 'block';

  console.log('[DEBUG] loginScreen.style.display:', els.loginScreen.style.display);
  console.log('[DEBUG] mainApp.style.display:', els.mainApp.style.display);
  console.log('[DEBUG] showMainApp 完成');
}

// ============================================================
// API HELPERS
// ============================================================

async function apiRequest(endpoint, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...options.headers,
  };

  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }

  const res = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
  });

  if (res.status === 401) {
    handleLogout();
    throw new Error("认证失败，请重新登录");
  }

  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || `请求失败: ${res.status}`);
  }

  return res.json();
}

// ============================================================
// KEYS LOADING
// ============================================================

async function loadKeys() {
  try {
    const offset = (currentPage - 1) * pageSize;
    const params = new URLSearchParams({
      limit: pageSize,
      offset: offset,
    });

    if (filters.platform) params.append("platform", filters.platform);
    if (filters.status) params.append("status", filters.status);
    if (filters.search) params.append("search", filters.search);

    const data = await apiRequest(`/keys?${params}`);

    renderKeys(data.keys);
    updatePagination(data.total);
  } catch (err) {
    showError(err.message);
  }
}

function renderKeys(keys) {
  selectedKeys.clear();
  els.selectAllCheckbox.checked = false;

  if (keys.length === 0) {
    els.keysBody.innerHTML = `
      <tr class="empty-row">
        <td colspan="10">暂无数据</td>
      </tr>
    `;
    return;
  }

  els.keysBody.innerHTML = keys
    .map(
      (key) => `
    <tr data-key-id="${key.id}">
      <td class="col-checkbox">
        <input type="checkbox" class="key-checkbox" data-key-id="${key.id}" />
      </td>
      <td class="col-id">${key.id}</td>
      <td class="col-platform">${provBadge(key.platform)}</td>
      <td class="col-key" title="${escapeHtml(key.masked_key)}">${escapeHtml(
        key.masked_key
      )}</td>
      <td class="col-status">${statusBadge(key.status)}</td>
      <td class="col-lastcheck">${formatTime(key.last_check)}</td>
      <td class="col-error">${escapeHtml(key.error_code || "—")}</td>
      <td class="col-nextcheck">${formatTime(key.next_check_time)}</td>
      <td class="col-notes" title="${escapeHtml(key.notes || "")}">${escapeHtml(
        key.notes || "—"
      )}</td>
      <td class="col-actions">
        <button class="table-action-btn table-action-btn--check" onclick="checkSingleKey(${
          key.id
        })">探活</button>
        <button class="table-action-btn table-action-btn--delete" onclick="deleteSingleKey(${
          key.id
        })">删除</button>
      </td>
    </tr>
  `
    )
    .join("");

  // Add checkbox listeners
  document.querySelectorAll(".key-checkbox").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const keyId = parseInt(e.target.dataset.keyId);
      if (e.target.checked) {
        selectedKeys.add(keyId);
      } else {
        selectedKeys.delete(keyId);
      }
    });
  });
}

function updatePagination(total) {
  els.totalCount.textContent = total;

  const totalPages = Math.ceil(total / pageSize);
  els.currentPage.textContent = currentPage;
  els.totalPages.textContent = totalPages || 1;

  els.prevPage.disabled = currentPage <= 1;
  els.nextPage.disabled = currentPage >= totalPages;
}

function changePage(delta) {
  currentPage += delta;
  loadKeys();
}

// ============================================================
// FILTERS
// ============================================================

function applyFilters() {
  filters.platform = els.filterPlatform.value;
  filters.status = els.filterStatus.value;
  filters.search = els.filterSearch.value.trim();

  currentPage = 1;
  loadKeys();
}

function clearFilters() {
  els.filterPlatform.value = "";
  els.filterStatus.value = "";
  els.filterSearch.value = "";

  filters = { platform: "", status: "", search: "" };
  currentPage = 1;
  loadKeys();
}

// ============================================================
// BATCH ACTIONS
// ============================================================

function selectAllKeys() {
  document.querySelectorAll(".key-checkbox").forEach((cb) => {
    cb.checked = true;
    selectedKeys.add(parseInt(cb.dataset.keyId));
  });
  els.selectAllCheckbox.checked = true;
}

function deselectAllKeys() {
  document.querySelectorAll(".key-checkbox").forEach((cb) => {
    cb.checked = false;
  });
  selectedKeys.clear();
  els.selectAllCheckbox.checked = false;
}

async function batchCheckKeys() {
  if (selectedKeys.size === 0) {
    alert("请先选择要探活的密钥");
    return;
  }

  if (!confirm(`确定要探活选中的 ${selectedKeys.size} 个密钥吗？`)) {
    return;
  }

  try {
    disableBatchButtons();
    const keyIds = Array.from(selectedKeys);

    const data = await apiRequest(`/keys/check`, {
      method: "POST",
      body: JSON.stringify({ key_ids: keyIds }),
    });

    alert(`探活完成：已检查 ${data.checked} 个密钥`);
    loadKeys();
  } catch (err) {
    showError(err.message);
  } finally {
    enableBatchButtons();
  }
}

async function batchDeleteKeys() {
  if (selectedKeys.size === 0) {
    alert("请先选择要删除的密钥");
    return;
  }

  if (
    !confirm(
      `确定要删除选中的 ${selectedKeys.size} 个密钥吗？此操作不可撤销！`
    )
  ) {
    return;
  }

  try {
    disableBatchButtons();
    const keyIds = Array.from(selectedKeys);

    for (const keyId of keyIds) {
      await apiRequest(`/keys/${keyId}`, { method: "DELETE" });
    }

    alert(`删除完成：已删除 ${keyIds.length} 个密钥`);
    loadKeys();
  } catch (err) {
    showError(err.message);
  } finally {
    enableBatchButtons();
  }
}

async function checkAllKeys() {
  if (
    !confirm("确定要探活所有 active 和 dead 状态的密钥吗？这可能需要较长时间。")
  ) {
    return;
  }

  try {
    disableBatchButtons();

    const data = await apiRequest(`/keys/check?all=true`, {
      method: "POST",
      body: JSON.stringify({}),
    });

    alert(`探活完成：已检查 ${data.checked} 个密钥`);
    loadKeys();
  } catch (err) {
    showError(err.message);
  } finally {
    enableBatchButtons();
  }
}

function disableBatchButtons() {
  els.batchCheck.disabled = true;
  els.batchDelete.disabled = true;
  els.checkAll.disabled = true;
}

function enableBatchButtons() {
  els.batchCheck.disabled = false;
  els.batchDelete.disabled = false;
  els.checkAll.disabled = false;
}

// ============================================================
// SINGLE KEY ACTIONS
// ============================================================

window.checkSingleKey = async function (keyId) {
  try {
    const btn = event.target;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';

    await apiRequest(`/keys/${keyId}/check`, { method: "POST" });

    alert(`探活完成：密钥 #${keyId}`);
    loadKeys();
  } catch (err) {
    showError(err.message);
  }
};

window.deleteSingleKey = async function (keyId) {
  if (!confirm(`确定要删除密钥 #${keyId} 吗？此操作不可撤销！`)) {
    return;
  }

  try {
    await apiRequest(`/keys/${keyId}`, { method: "DELETE" });
    alert(`删除完成：密钥 #${keyId}`);
    loadKeys();
  } catch (err) {
    showError(err.message);
  }
};

// ============================================================
// MOVE KEYS
// ============================================================

async function moveKeys() {
  const keysText = els.moveKeys.value.trim();
  const platform = els.movePlatform.value;
  const notes = els.moveNotes.value.trim();

  if (!keysText) {
    alert("请输入要移入的密钥");
    return;
  }

  const keys = keysText
    .split("\n")
    .map((k) => k.trim())
    .filter(Boolean);

  if (keys.length === 0) {
    alert("请输入有效的密钥");
    return;
  }

  if (!confirm(`确定要将 ${keys.length} 个密钥移入长期监控吗？`)) {
    return;
  }

  try {
    els.moveKeysBtn.disabled = true;
    els.moveKeysBtn.textContent = "移入中...";

    const data = await apiRequest(`/keys/move`, {
      method: "POST",
      body: JSON.stringify({ keys, platform, notes: notes || null }),
    });

    alert(
      `移入完成：\n新增 ${data.added} 个\n跳过重复 ${data.duplicates} 个`
    );

    els.moveKeys.value = "";
    els.moveNotes.value = "";
    loadKeys();
  } catch (err) {
    showError(err.message);
  } finally {
    els.moveKeysBtn.disabled = false;
    els.moveKeysBtn.textContent = "移入监控";
  }
}

// ============================================================
// RENDERING HELPERS
// ============================================================

function provBadge(platform) {
  const map = {
    gemini: "Gemini",
    openai: "OpenAI",
    anthropic: "Anthropic",
    gcp: "GCP",
  };
  const label = map[platform] || platform;
  return `<span class="prov-badge prov-${platform}">${label}</span>`;
}

function statusBadge(status) {
  const map = {
    active: { label: "Active", class: "active" },
    dead: { label: "Dead", class: "dead" },
    abandoned: { label: "Abandoned", class: "abandoned" },
  };
  const info = map[status] || { label: status, class: "unknown" };
  return `<span class="status-badge status-${info.class}">${info.label}</span>`;
}

function formatTime(timestamp) {
  if (!timestamp) return "—";

  const date = new Date(timestamp * 1000);
  const now = new Date();
  const diff = now - date;

  // Less than 1 minute
  if (diff < 60000) return "刚刚";

  // Less than 1 hour
  if (diff < 3600000) {
    const mins = Math.floor(diff / 60000);
    return `${mins}分钟前`;
  }

  // Less than 1 day
  if (diff < 86400000) {
    const hours = Math.floor(diff / 3600000);
    return `${hours}小时前`;
  }

  // Less than 7 days
  if (diff < 604800000) {
    const days = Math.floor(diff / 86400000);
    return `${days}天前`;
  }

  // Format as date
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const h = String(date.getHours()).padStart(2, "0");
  const min = String(date.getMinutes()).padStart(2, "0");

  return `${y}-${m}-${d} ${h}:${min}`;
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[
        c
      ])
  );
}

function showError(msg) {
  alert(`错误: ${msg}`);
}
