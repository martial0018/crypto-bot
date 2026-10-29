const loginPanel = document.querySelector("#login-panel");
const dashboardPanel = document.querySelector("#dashboard-panel");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const actionError = document.querySelector("#action-error");
const logoutButton = document.querySelector("#logout");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showLogin() {
  loginPanel.classList.remove("hidden");
  dashboardPanel.classList.add("hidden");
  logoutButton.classList.add("hidden");
}

function showDashboard() {
  loginPanel.classList.add("hidden");
  dashboardPanel.classList.remove("hidden");
  logoutButton.classList.remove("hidden");
}

function addPosition(container, position) {
  const item = document.createElement("article");
  item.className = "position";
  const title = document.createElement("strong");
  title.textContent = position.market;
  const details = document.createElement("span");
  details.textContent = `Entry ${Number(position.entry_price).toPrecision(7)} · Mark ${Number(position.last_price).toPrecision(7)} · ${position.leverage}x · Margin ${Number(position.margin_usdt).toFixed(2)} USDT`;
  item.append(title, details);
  container.append(item);
}

async function refresh() {
  try {
    const status = await request("/api/status");
    showDashboard();
    document.querySelector("#entry-status").textContent = status.accept_entries ? "Active" : "Paused";
    document.querySelector("#entry-status").className = `status ${status.accept_entries ? "active" : "paused"}`;
    document.querySelector("#runner-status").textContent = status.runner_running ? "Running" : "Stopped";
    document.querySelector("#runner-status").className = `status ${status.runner_running ? "active" : "offline"}`;
    document.querySelector("#cash").textContent = `${Number(status.cash_usdt).toFixed(2)} USDT`;
    document.querySelector("#position-count").textContent = String(status.open_positions.length);
    const positions = document.querySelector("#positions");
    positions.replaceChildren();
    if (status.open_positions.length) {
      status.open_positions.forEach((position) => addPosition(positions, position));
    } else {
      positions.textContent = "No open paper positions.";
    }
    const scanDate = status.last_scan ? new Date(status.last_scan) : null;
    const lastScan = scanDate && !Number.isNaN(scanDate.getTime())
      ? scanDate.toLocaleString()
      : status.last_scan ? "unknown" : "not yet scanned";
    document.querySelector("#updated").textContent = `Last scan: ${lastScan} · Refreshed ${new Date().toLocaleTimeString()}`;
    actionError.textContent = "";
    startButton.disabled = status.accept_entries && status.runner_running;
    stopButton.disabled = !status.accept_entries;
  } catch (error) {
    if (error.status === 401) {
      showLogin();
      return;
    }
    actionError.textContent = error.message;
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";
  const token = document.querySelector("#token").value;
  try {
    await request("/api/login", { method: "POST", body: JSON.stringify({ token }) });
    document.querySelector("#token").value = "";
    await refresh();
  } catch (error) {
    loginError.textContent = error.message;
  }
});

async function setEntries(enabled) {
  startButton.disabled = true;
  stopButton.disabled = true;
  actionError.textContent = "";
  try {
    await request("/api/control", {
      method: "POST",
      body: JSON.stringify({ accept_entries: enabled }),
    });
    await refresh();
  } catch (error) {
    actionError.textContent = error.message;
    await refresh();
  }
}

startButton.addEventListener("click", () => setEntries(true));
stopButton.addEventListener("click", () => setEntries(false));
logoutButton.addEventListener("click", async () => {
  try {
    await request("/api/logout", { method: "POST", body: "{}" });
  } finally {
    showLogin();
  }
});

refresh();
window.setInterval(refresh, 5000);
