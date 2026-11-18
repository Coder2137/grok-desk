/* Three read-only desk layouts. They share one book from state.json. */
(function (root) {
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function money(value) {
    if (value == null || value === "") return "unknown";
    const number = Number(value);
    if (!Number.isFinite(number)) return "unknown";
    return (number < 0 ? "-" : "") + "$" + Math.abs(number).toFixed(2);
  }

  function signed(value) {
    if (value == null || value === "") return "unknown";
    const number = Number(value);
    if (!Number.isFinite(number)) return "unknown";
    return (number > 0 ? "+" : number < 0 ? "-" : "") + "$" + Math.abs(number).toFixed(2);
  }

  function pct(value) {
    if (value == null || value === "") return "unknown";
    const number = Number(value);
    if (!Number.isFinite(number)) return "unknown";
    return (number > 0 ? "+" : "") + number.toFixed(2) + "%";
  }

  function price(value) {
    if (value == null || value === "") return "unknown";
    const number = Number(value);
    if (!Number.isFinite(number)) return "unknown";
    if (Math.abs(number) >= 1) return "$" + number.toFixed(4);
    return "$" + number.toPrecision(4);
  }

  function tokens(value) {
    if (value == null || value === "") return "unknown";
    const number = Number(value);
    if (!Number.isFinite(number)) return "unknown";
    if (number >= 1000) return number.toLocaleString(undefined, { maximumFractionDigits: 0 });
    return number.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function held(min) {
    if (min == null || min === "") return "unknown";
    const number = Number(min);
    if (!Number.isFinite(number) || number < 0) return "unknown";
    if (number < 60) return number + "m";
    const hours = Math.floor(number / 60);
    return hours + "h " + (number % 60) + "m";
  }

  function clock(ts) {
    if (!ts) return "unknown";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime())) return "unknown";
    return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function ago(ts) {
    if (!ts) return "unknown";
    const ms = Date.now() - Date.parse(ts);
    if (!Number.isFinite(ms) || ms < 0) return "unknown";
    const min = Math.floor(ms / 60000);
    if (min < 1) return "just now";
    if (min < 60) return min + "m ago";
    return Math.floor(min / 60) + "h ago";
  }

  function coinName(row) {
    const name = row.name || row.symbol || "Unnamed coin";
    const symbol = row.symbol ? " · " + row.symbol : "";
    return esc(name) + (row.name && row.symbol ? symbol : "");
  }

  function link(row) {
    if (!row.link) return "";
    return `<a class="desk-link" href="${esc(row.link)}">FOMO</a>`;
  }

  function tone(value) {
    if (value == null || value === "") return "";
    const number = Number(value);
    if (!Number.isFinite(number) || number === 0) return "";
    return number > 0 ? "up" : "down";
  }

  function summary(model) {
    const sum = model.summary || {};
    return `
      <header class="desk-head">
        <div class="desk-title">
          <h1>${esc(model.name || "Desk")}</h1>
          ${model.paper ? '<span class="desk-badge">PAPER</span>' : ""}
          ${model.example ? '<span class="desk-example">Example data</span>' : ""}
        </div>
        <p class="desk-age">Updated ${esc(ago(model.updatedAt))}</p>
        <div class="desk-stats">
          <div><span>Cash</span><b>${money(sum.cash)}</b></div>
          <div><span>Holdings</span><b>${money(sum.holdingsValue)}</b></div>
          <div><span>Equity</span><b>${money(sum.equity)}</b></div>
          <div><span>Today</span><b class="${tone(sum.realizedToday)}">${signed(sum.realizedToday)}</b></div>
          <div><span>Total</span><b class="${tone(sum.realizedTotal)}">${signed(sum.realizedTotal)}</b></div>
          <div><span>Wins / losses</span><b>${esc(sum.wins == null ? "unknown" : sum.wins)} / ${esc(sum.losses == null ? "unknown" : sum.losses)}</b></div>
        </div>
      </header>`;
  }

  function chartBlock() {
    return `<section class="desk-chart"><h2>Equity</h2><div class="chart-wrap"><canvas class="chart" id="home-chart"></canvas></div></section>`;
  }

  function holdingRow(row, dense) {
    const pnl = `<span class="${tone(row.pnlUsd)}">${signed(row.pnlUsd)} · ${pct(row.pnlPct)}</span>`;
    if (dense) {
      return `<tr>
        <td>${coinName(row)}</td>
        <td>${clock(row.boughtAt)}</td>
        <td>${price(row.entry)}</td>
        <td>${tokens(row.tokens)}<br><span class="muted">${money(row.spent)}</span></td>
        <td>${price(row.price)}<br><span class="muted">${money(row.value)}</span></td>
        <td>${pnl}</td>
        <td>${price(row.stop)}<br><span class="muted">${price(row.takeProfit)}</span></td>
        <td>${held(row.heldMin)} ${link(row)}</td>
      </tr>`;
    }
    return `<article class="desk-card">
      <header><b>${coinName(row)}</b>${link(row)}</header>
      <p class="muted">Bought ${clock(row.boughtAt)} · held ${held(row.heldMin)}</p>
      <div class="desk-grid">
        <div><span>Entry</span>${price(row.entry)}</div>
        <div><span>Now</span>${price(row.price)}</div>
        <div><span>Spent</span>${money(row.spent)}</div>
        <div><span>Value</span>${money(row.value)}</div>
        <div><span>Tokens</span>${tokens(row.tokens)}</div>
        <div><span>P&amp;L</span><b class="${tone(row.pnlUsd)}">${signed(row.pnlUsd)} · ${pct(row.pnlPct)}</b></div>
        <div><span>Stop</span>${price(row.stop)}</div>
        <div><span>Take profit</span>${price(row.takeProfit)}</div>
      </div>
    </article>`;
  }

  function holdings(model, look) {
    const rows = model.holdings || [];
    if (!rows.length) return `<section><h2>Holdings</h2><p class="desk-empty">No coins held right now</p></section>`;
    if (look === "a") {
      return `<section><h2>Holdings · ${rows.length}</h2>
        <div class="desk-table-wrap"><table class="desk-table">
          <thead><tr><th>Coin</th><th>Bought</th><th>Entry</th><th>Amount</th><th>Now</th><th>P&amp;L</th><th>Stop / TP</th><th>Held</th></tr></thead>
          <tbody>${rows.map((row) => holdingRow(row, true)).join("")}</tbody>
        </table></div></section>`;
    }
    if (look === "c") {
      return `<section><h2>Holdings · ${rows.length}</h2>${rows.slice(0, 3).map((row) => `
        <p class="desk-line"><b>${coinName(row)}</b> ${signed(row.pnlUsd)} · ${held(row.heldMin)} · stop ${price(row.stop)}</p>`).join("")}
        ${rows.length > 3 ? `<p class="muted">${rows.length - 3} more</p>` : ""}</section>`;
    }
    return `<section><h2>Holdings</h2>${rows.map((row) => holdingRow(row, false)).join("")}</section>`;
  }

  function soldRow(row, dense) {
    if (dense) {
      return `<tr>
        <td>${coinName(row)}</td>
        <td>${clock(row.boughtAt)}<br><span class="muted">${clock(row.soldAt)}</span></td>
        <td>${held(row.heldMin)}</td>
        <td>${price(row.entry)} → ${price(row.exit)}</td>
        <td>${esc(row.reasonText || "Sold")}</td>
        <td class="${tone(row.pnlUsd)}">${signed(row.pnlUsd)} · ${pct(row.pnlPct)}<br><span class="muted">fees ${money(row.fees)}</span></td>
      </tr>`;
    }
    return `<article class="desk-card">
      <header><b>${coinName(row)}</b><span class="${tone(row.pnlUsd)}">${signed(row.pnlUsd)}</span></header>
      <p>${esc(row.reasonText || "Sold")} · held ${held(row.heldMin)} · ${pct(row.pnlPct)}</p>
      <p class="muted">${clock(row.boughtAt)} → ${clock(row.soldAt)} · ${price(row.entry)} → ${price(row.exit)} · fees ${money(row.fees)}</p>
    </article>`;
  }

  function sold(model, look) {
    const rows = model.sold || [];
    const count = model.summary && model.summary.soldCount != null ? model.summary.soldCount : rows.length;
    if (!count) return `<section><h2>Sold</h2><p class="desk-empty">Nothing sold yet</p></section>`;
    const shown = look === "c" ? rows.slice(0, 3) : rows;
    if (look === "a") {
      return `<section><h2>Sold · ${count}</h2>
        <div class="desk-table-wrap"><table class="desk-table">
          <thead><tr><th>Coin</th><th>Bought / sold</th><th>Held</th><th>Prices</th><th>Why</th><th>P&amp;L</th></tr></thead>
          <tbody>${shown.map((row) => soldRow(row, true)).join("")}</tbody>
        </table></div></section>`;
    }
    return `<section><h2>Sold · ${count}</h2>${shown.map((row) => soldRow(row, false)).join("")}</section>`;
  }

  function activity(model) {
    const rows = model.activity || [];
    if (!rows.length) return `<section><h2>Trades</h2><p class="desk-empty">No trades yet</p></section>`;
    return `<section><h2>Trades</h2><ol class="desk-feed">${rows.slice(0, 12).map((row) => `
      <li><span>${esc(clock(row.ts))}</span> <b>${esc(row.title || row.kind || "Trade")}</b> <span class="muted">${esc(row.subtitle || "")} ${esc(row.value || "")}</span></li>`).join("")}</ol></section>`;
  }

  function pipeline(model) {
    const rows = model.pipeline || [];
    if (!rows.length) return `<section><h2>Pipeline</h2><p class="desk-empty">No coins seen tonight</p></section>`;
    return `<section><h2>Pipeline</h2><ul class="desk-pipe">${rows.map((row) => {
      const audit = row.audit === "fail"
        ? "Audit failed · " + (row.failedLayer || "a layer")
        : row.audit === "pass"
          ? "Audit passed"
          : row.audit === "retrying"
            ? "Audit retrying"
            : "Audit not run";
      const analyst = row.analyst === "reject"
        ? "Analyst rejected" + (row.analystReason ? " · " + row.analystReason : "")
        : row.analyst === "pass"
          ? "Analyst passed"
          : "Analyst not run";
      return `<li><b>${coinName(row)}</b> <span>Scan ${esc(row.scan || "unknown")}${row.scanReason ? " · " + esc(row.scanReason) : ""}</span> <span>${esc(audit)}</span> <span>${esc(analyst)}</span></li>`;
    }).join("")}</ul></section>`;
  }

  function health(model) {
    const rows = model.health || [];
    if (!rows.length) return "";
    return `<section><h2>Desk health</h2><p class="desk-health">${rows.map((row) => `<span class="${row.up ? "up" : "down"}">${esc(row.name)} ${row.up ? "up" : "down"}</span>`).join("")}</p></section>`;
  }

  function renderDesk(model, look) {
    const which = look === "b" || look === "c" ? look : "a";
    return `<div class="desk look-${which}">
      ${summary(model)}
      ${chartBlock()}
      ${holdings(model, which)}
      ${sold(model, which)}
      ${activity(model)}
      ${pipeline(model)}
      ${health(model)}
    </div>`;
  }

  root.DeskView = { renderDesk: renderDesk };
})(typeof globalThis !== "undefined" ? globalThis : this);
