(function () {
  const view = document.getElementById("view");
  const errorEl = document.getElementById("error");
  const tabs = document.getElementById("tabs");
  const state = {
    screen: "home",
    tab: "home",
    data: null,
    chart: null,
    activity: null,
    coin: null,
    coinMint: null,
    range: "LIVE",
    query: "",
    offline: false,
    retry: 15,
    timer: null,
    seen: Number(localStorage.getItem(localStorage.getItem("deskSlot") === "desk2" ? "deskSeen2" : "deskSeen") || 0),
  };

  function deskSlot() {
    return localStorage.getItem("deskSlot") === "desk2" ? "desk2" : "desk1";
  }

  function deskPrefix() {
    return deskSlot() === "desk2" ? "desk2/" : "";
  }

  function capturePath() {
    const match = location.hash.match(/(?:^#|[&#])d=([^&]+)/);
    if (match && decodeURIComponent(match[1]).length >= 32) {
      localStorage.setItem("deskDataPath", decodeURIComponent(match[1]));
    }
    return localStorage.getItem("deskDataPath") || "";
  }

  async function pull(file) {
    const dataPath = capturePath();
    if (!dataPath) throw new Error("missing");
    return globalThis.DeskLive.loadDeskFile(deskPrefix() + file, { dataPath: dataPath, pageUrl: location.href });
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function money(value) {
    const number = Number(value || 0);
    return (number < 0 ? "-" : "") + "$" + Math.abs(number).toFixed(2);
  }

  function liq(value) {
    const number = Number(value || 0);
    if (number >= 1000000) return "$" + (number / 1000000).toFixed(1) + "M";
    if (number >= 1000) return "$" + (number / 1000).toFixed(1) + "K";
    return "$" + number.toFixed(0);
  }

  function odo(value) {
    return money(value).split("").map((ch) => `<span>${esc(ch)}</span>`).join("");
  }

  function timeLabel(ts) {
    if (!ts) return "";
    const date = new Date(ts);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  async function poll() {
    if (!capturePath()) {
      render();
      return;
    }
    try {
      const [data, chart, activity] = await Promise.all([
        pull("state.json"),
        pull("chart.json"),
        pull("activity.json"),
      ]);
      state.data = data;
      state.chart = chart;
      state.activity = activity;
      state.offline = false;
      state.retry = 15;
      if ((state.screen === "coin" || state.screen === "auditor") && state.coinMint) {
        try {
          state.coin = await pull("coin/" + encodeURIComponent(state.coinMint) + ".json");
        } catch (err) {
          const known = ((state.data && state.data.coins) || []).find((coin) => coin.mint === state.coinMint);
          if (!state.coin || state.coin.mint !== state.coinMint) {
            state.coin = known
              ? { mint: known.mint, name: known.name, symbol: known.symbol, candles: [], layers: [], holders: null, mode: state.data.mode }
              : null;
          }
        }
      }
    } catch (err) {
      state.offline = true;
      state.retry = 15;
    }
    render();
  }

  function showError() {
    if (!state.offline) {
      errorEl.hidden = true;
      return;
    }
    const when = state.data && state.data.ts ? timeLabel(state.data.ts) : "never";
    errorEl.hidden = false;
    errorEl.textContent = `Desk offline · retrying in ${state.retry}s · last update ${when}`;
  }

  function setScreen(screen, mint) {
    state.screen = screen;
    if (mint) state.coinMint = mint;
    if (screen === "home" || screen === "agents" || screen === "activity" || screen === "account") {
      state.tab = screen;
    }
    document.querySelectorAll("#tabs button").forEach((button) => {
      button.classList.toggle("on", button.dataset.tab === state.tab);
    });
    tabs.hidden = screen === "coin" || screen === "auditor";
    render();
    if ((screen === "coin" || screen === "auditor") && mint) poll();
  }

  function filteredCoins() {
    const q = state.query.trim().toLowerCase();
    const coins = (state.data && state.data.coins) || [];
    if (!q) return [];
    return coins.filter((coin) => {
      return [coin.name, coin.symbol, coin.mint].join(" ").toLowerCase().includes(q);
    }).slice(0, 8);
  }

  function unread() {
    const rows = (state.activity && state.activity.rows) || [];
    return rows.some((row) => Number(row.id) > state.seen);
  }

  function markSeen() {
    const rows = (state.activity && state.activity.rows) || [];
    const max = rows.reduce((best, row) => Math.max(best, Number(row.id) || 0), state.seen);
    state.seen = max;
    localStorage.setItem(deskSlot() === "desk2" ? "deskSeen2" : "deskSeen", String(max));
  }

  function home() {
    if (!state.data) return loading();
    const data = state.data;
    const change = Number(data.dayChangePct || 0);
    const hits = filteredCoins();
    return `
      <div class="desks ${deskSlot()}" role="tablist" aria-label="Desk">
        <div class="desks-track">
          <span class="desks-thumb" aria-hidden="true"></span>
          <button type="button" role="tab" data-desk="desk1" aria-selected="${deskSlot() === "desk1"}" class="${deskSlot() === "desk1" ? "on" : ""}">Desk 1</button>
          <button type="button" role="tab" data-desk="desk2" aria-selected="${deskSlot() === "desk2"}" class="${deskSlot() === "desk2" ? "on" : ""}">Desk 2</button>
        </div>
      </div>
      <div class="nav">
        <button class="avatar" id="go-account" aria-label="Account">•</button>
        <button class="brand" id="go-agents">Grok Bot ›</button>
        <input class="search" id="search" placeholder="Search" value="${esc(state.query)}">
        <button class="bell" id="bell" aria-label="Activity">${unread() ? '<i class="dot"></i>' : ""}</button>
      </div>
      ${data.paper ? '<div class="paper">PAPER</div>' : ""}
      <div class="balance">
        <div class="odo">${odo(data.balance)}</div>
        <div class="sub">
          <span class="${change >= 0 ? "up" : "down"}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</span>
          <span class="pill">${Number(data.multiple || 1).toFixed(2)}×</span>
        </div>
      </div>
      ${data.ticketNote ? `<p class="note">${esc(data.ticketNote)}</p>` : ""}
      <div class="chart-wrap"><canvas class="chart" id="home-chart"></canvas></div>
      <div class="ranges">
        ${["LIVE", "1H", "4H", "1D", "1W", "ALL"].map((item) => `<button type="button" data-range="${item}" class="${state.range === item ? "on" : ""}">${item}</button>`).join("")}
      </div>
      ${hits.length ? `<div class="hits">${hits.map((coin) => `<button type="button" data-coin="${esc(coin.mint)}">${esc(coin.name || coin.symbol || coin.mint)}</button>`).join("")}</div>` : ""}
      <h2>Agents · 5 running</h2>
      <div class="carousel">
        ${agentCard("scanner", true)}${agentCard("auditor", true)}${agentCard("analyst", true)}${agentCard("executor", true)}${agentCard("risk", true)}
      </div>
      <h2>Activity <button type="button" id="see-all">See all</button></h2>
      <div class="activity card">${activityRows((data.activity || []).slice(0, 5))}</div>
    `;
  }

  function loading() {
    if (!capturePath()) {
      return `<div class="screen"><h2>Grok Bot</h2><p class="note">Open the desk link once so this phone can find the night.</p></div>`;
    }
    return `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
  }

  function agentCard(name) {
    const data = state.data;
    const card = data.agents[name];
    const title = name[0].toUpperCase() + name.slice(1);
    let body = "";
    if (name === "scanner") {
      body = card.count ? scannerBody(card) : `<p class="empty">Listening for graduations</p><p>${card.count} pools</p>`;
      if (card.count) body = `<p>${card.count} pools</p>` + scannerBody(card);
    } else if (name === "auditor") {
      body = card.count
        ? `<p>${card.count} taken apart</p><p>${card.rejected} rejected</p>`
        : `<p class="empty">No coin taken apart yet</p>`;
    } else if (name === "analyst") {
      const score = card.lastScore == null ? 0 : Number(card.lastScore);
      body = `<p>Scoring</p><div class="ring ${score >= 70 ? "good" : ""}" style="--p:${Math.max(0, Math.min(100, score))}"><span>${score || "–"}</span></div>`;
    } else if (name === "executor") {
      const last = card.last;
      body = `<p>${card.count} trades</p><p>${last ? money(last.size) : money(0)} · ${esc(data.stackPct)}% of stack</p>`;
    } else {
      const last = card.last;
      const label = last && last.reason === "trail" ? "Trailed out" : last && last.reason === "stop" ? "Stopped" : "Guarding";
      body = `<p>${label}</p><div class="pills"><span class="tag stop">stop -${esc(data.stopPct)}%</span><span class="tag trail">trail ${esc(data.trailPct)}%</span></div>`;
    }
    return `<article class="card" data-open="${name}"><header><span class="agent-name ${name}">${title}</span><i class="status ${card.running ? "on" : ""}"></i></header>${body}</article>`;
  }

  function scannerBody(card) {
    if (!card.rows.length) return `<p class="empty">Listening for graduations</p>`;
    return card.rows.slice(0, 4).map((row) => {
      if (row.verdict === "kill") {
        return `<div class="feed kill"><b>${esc(row.name)}</b><span>${esc(row.reason || "")}</span></div>`;
      }
      return `<div class="feed">${esc(row.name)} · liq ${liq(row.liquidity)} · → auditor</div>`;
    }).join("");
  }

  function activityRows(rows) {
    if (!rows.length) return `<p class="empty">Nothing yet tonight</p>`;
    return rows.map((row) => `
      <button class="row" data-coin="${esc(row.mint)}">
        <span class="stack"><b>${esc(row.title)}</b><span class="muted">${esc(row.subtitle || "")}</span></span>
        <span>${esc(row.value || "")}</span>
      </button>
    `).join("");
  }

  function agents() {
    if (!state.data) return loading();
    const order = ["scanner", "auditor", "analyst", "executor", "risk"];
    return `<div class="screen-head"><h1>Agents</h1></div>` + order.map((name) => {
      const card = state.data.agents[name];
      const locked = name === "risk" && card.locked;
      const decisions = (name === "scanner" ? card.rows : name === "analyst" ? card.rows : []).slice(0, 20);
      return `<article class="card">
        <header><span class="agent-name ${name}">${name[0].toUpperCase() + name.slice(1)}</span><i class="status ${card.running ? "on" : ""}"></i></header>
        <p>${card.count || 0} today ${card.paused ? "· paused" : ""} ${locked ? '<span class="lock">· locked</span>' : ""}</p>
        <button type="button" disabled>${card.paused ? "Resume in Telegram" : "Pause in Telegram"}</button>
        ${decisions.map((row) => `<div class="feed">${esc(row.name || row.mint)} ${esc(row.reason || row.score || "")}</div>`).join("")}
      </article>`;
    }).join("") + `<p class="note">Settings change in Telegram.</p>`;
  }

  function activityScreen() {
    const rows = (state.activity && state.activity.rows) || (state.data && state.data.activityMore) || [];
    return `<div class="screen-head"><h1>Activity</h1></div><div class="card activity">${activityRows(rows)}</div>`;
  }

  function account() {
    if (!state.data) return loading();
    const data = state.data;
    const groups = Object.entries(data.settings || {}).map(([group, rows]) => {
      const fields = rows.map((row) => {
        if (row.kind === "float" || row.kind === "int") {
          return `<label class="field">${esc(row.key)} <b>${esc(row.value)}</b><input type="range" disabled min="${esc(row.min)}" max="${esc(row.max)}" value="${esc(row.value)}"></label>`;
        }
        return `<label class="field">${esc(row.key)} <b>${esc(row.value)}</b></label>`;
      }).join("");
      return `<section class="card"><h3>${esc(group)}</h3>${fields}</section>`;
    }).join("");
    const profile = data.profileUrl ? `<a class="btn" href="${esc(data.profileUrl)}">Your FOMO profile</a>` : `<p class="note">Your FOMO profile appears after the handle is set in Telegram.</p>`;
    return `
      <div class="screen-head"><h1>Account</h1></div>
      <div class="paper">${esc(data.mode)}</div>
      <p class="note">Stack ${money(data.stack)}. LIVE asks you to type LIVE in Telegram.</p>
      ${profile}
      <div class="toggle"><span>Telegram ${data.telegram ? "connected" : "not connected"}</span></div>
      <button class="card" id="test-push" type="button">Send test push</button>
      <p class="note" id="test-note" hidden>Send test in Telegram.</p>
      ${groups}
    `;
  }

  function auditor() {
    const coin = state.coin;
    if (!coin) {
      return `<div class="screen-head"><button class="back" id="back">Back</button><h1>Auditor</h1></div><p class="note">No coin taken apart yet</p>`;
    }
    const layers = coin.layers || [];
    const discs = Array.from({ length: 8 }, (_, index) => {
      const layer = layers[index];
      const cls = !layer || layer.value === "not checked" ? "" : layer.pass ? "ok" : layer.pass === false ? "bad" : "wait";
      return `<i class="disc ${cls}"></i>`;
    }).join("");
    const rows = layers.map((layer) => `<div class="layer-row"><span>${layer.n} ${esc(layer.name)}</span><span>${esc(layer.value)}</span></div>`).join("");
    const done = layers.filter((layer) => layer.pass).length;
    const failed = layers.find((layer) => layer.pass === false && layer.value !== "not checked");
    const copy = failed ? esc(failed.value) : done === 8 ? "8/8 → Executor" : "taking apart...";
    const enabled = state.data && state.data.auditorEnabled;
    const link = coin.link ? `<a class="btn" href="${esc(coin.link)}">Open on FOMO</a>` : "";
    return `
      <div class="screen-head"><button class="back" id="back">Back</button><h1>${esc(coin.name || "Auditor")}</h1></div>
      <div class="discs"><i class="beam"></i>${discs}</div>
      <p class="note">${copy}</p>
      <div class="card">${rows || '<p class="empty">No coin taken apart yet</p>'}</div>
      <p class="note">${failed ? "Rejected. " + esc(failed.value) : done === 8 ? "Cleared." : ""}</p>
      <div class="toggle"><span>Take apart before buying</span><i class="switch ${enabled ? "on" : ""}"><i></i></i></div>
      <p class="note">On in Telegram. Turning it off asks twice.</p>
      <div class="actions">${link}</div>
    `;
  }

  function coinScreen() {
    const coin = state.coin;
    if (!coin) return `<div class="screen-head"><button class="back" id="back">Back</button><h1>Coin</h1></div>` + loading();
    const change = coin.changeSinceEntry;
    const changeHtml = change == null ? "" : `<div class="${change >= 0 ? "up" : "down"}">${change >= 0 ? "▲" : "▼"} ${Math.abs(change).toFixed(2)}% since entry</div>`;
    const position = coin.position && coin.position.status !== "closed" ? coin.position : null;
    const exitCard = position ? `<article class="card"><header>Position</header><p>${money(position.size)} · stop -${esc(position.stopPct)}%</p><p>${position.trailActive ? "Risk trailing stop" : "Hard stop"}</p></article>` : "";
    const holders = coin.holders == null ? "n/a" : esc(coin.holders);
    const link = coin.link || "";
    const buy = link ? `<a class="btn" href="${esc(link)}">Buy on FOMO</a>` : "";
    const sell = link && position ? `<a class="btn sell" href="${esc(link)}">Sell on FOMO</a>` : "";
    return `
      <div class="screen-head"><button class="back" id="back">Back</button><h1>${esc(coin.name || coin.mint)}</h1></div>
      ${coin.mode === "PAPER" ? '<div class="paper">PAPER</div>' : ""}
      <p class="note">${esc(coin.symbol || "")} · Solana · ${esc(coin.age || "")} ${coin.shield ? "· " + esc(coin.shield) : ""}</p>
      ${coin.scan ? `<p class="note">Scanner ${esc(coin.scan.verdict || "")}${coin.scan.reason ? " · " + esc(coin.scan.reason) : ""}</p>` : ""}
      <div class="big">${coin.price == null ? "—" : esc(Number(coin.price))}</div>
      <div class="screen">${changeHtml}</div>
      <div class="chart-wrap"><canvas class="chart" id="coin-chart"></canvas></div>
      <div class="grid">
        <div><span>Market cap</span>${coin.marketCap == null ? "—" : liq(coin.marketCap)}</div>
        <div><span>Liquidity</span>${coin.liquidity == null ? "—" : liq(coin.liquidity)}</div>
        <div><span>24h volume</span>${coin.volume24 == null ? "—" : liq(coin.volume24)}</div>
        <div><span>Holders</span>${holders}</div>
      </div>
      ${exitCard}
      <div class="actions">${buy}${sell}</div>
    `;
  }

  function render() {
    showError();
    const screen = state.screen;
    if (screen === "agents") view.innerHTML = agents();
    else if (screen === "activity") view.innerHTML = activityScreen();
    else if (screen === "account") view.innerHTML = account();
    else if (screen === "auditor") view.innerHTML = auditor();
    else if (screen === "coin") view.innerHTML = coinScreen();
    else view.innerHTML = home();
    bind();
    draw();
  }

  function filterPoints(points) {
    if (!points) return [];
    if (state.range === "ALL" || state.range === "LIVE") return points;
    const hours = { "1H": 1, "4H": 4, "1D": 24, "1W": 24 * 7 }[state.range] || 24;
    const cutoff = Date.now() - hours * 3600 * 1000;
    return points.filter((point) => Date.parse(point.t) >= cutoff);
  }

  function draw() {
    const homeCanvas = document.getElementById("home-chart");
    if (homeCanvas && window.DeskCharts && state.chart) {
      const points = filterPoints(state.chart.points || []);
      const marks = state.range === "ALL" || state.range === "LIVE" ? state.chart.marks || [] : [];
      window.DeskCharts.line(homeCanvas, points, marks, state.chart.startValue);
    }
    const coinCanvas = document.getElementById("coin-chart");
    if (coinCanvas && window.DeskCharts && state.coin) {
      window.DeskCharts.candles(coinCanvas, state.coin.candles || [], state.coin.riskLine);
    }
  }

  function bind() {
    const search = document.getElementById("search");
    if (search) {
      search.addEventListener("input", () => {
        state.query = search.value;
        render();
        const again = document.getElementById("search");
        if (again) {
          again.focus();
          again.setSelectionRange(again.value.length, again.value.length);
        }
      });
    }
    document.querySelectorAll("[data-desk]").forEach((button) => {
      button.addEventListener("click", () => {
        localStorage.setItem("deskSlot", button.dataset.desk === "desk2" ? "desk2" : "desk1");
        state.seen = Number(localStorage.getItem(deskSlot() === "desk2" ? "deskSeen2" : "deskSeen") || 0);
        state.data = null;
        state.chart = null;
        state.activity = null;
        state.coin = null;
        poll();
      });
    });
    document.querySelectorAll("[data-range]").forEach((button) => {
      button.addEventListener("click", () => {
        state.range = button.dataset.range;
        render();
      });
    });
    document.querySelectorAll("[data-coin]").forEach((button) => {
      button.addEventListener("click", () => setScreen("coin", button.dataset.coin));
    });
    const accountBtn = document.getElementById("go-account");
    if (accountBtn) accountBtn.addEventListener("click", () => setScreen("account"));
    const agentsBtn = document.getElementById("go-agents");
    if (agentsBtn) agentsBtn.addEventListener("click", () => setScreen("agents"));
    const bell = document.getElementById("bell");
    if (bell) bell.addEventListener("click", () => { markSeen(); setScreen("activity"); });
    const see = document.getElementById("see-all");
    if (see) see.addEventListener("click", () => setScreen("activity"));
    const back = document.getElementById("back");
    if (back) back.addEventListener("click", () => setScreen("home"));
    document.querySelectorAll("[data-open]").forEach((card) => {
      card.addEventListener("click", () => {
        if (card.dataset.open === "auditor") {
          const mint = state.data && state.data.agents.auditor.current;
          setScreen("auditor", mint || "");
        } else setScreen("agents");
      });
    });
    const test = document.getElementById("test-push");
    if (test) test.addEventListener("click", () => {
      const note = document.getElementById("test-note");
      if (note) note.hidden = false;
    });
  }

  document.querySelectorAll("#tabs button").forEach((button) => {
    button.addEventListener("click", () => setScreen(button.dataset.tab));
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }

  capturePath();
  poll();
  window.addEventListener("hashchange", () => {
    capturePath();
    poll();
  });
  setInterval(poll, 15000);
  setInterval(() => {
    if (!state.offline) return;
    state.retry = Math.max(0, state.retry - 1);
    showError();
  }, 1000);
})();
