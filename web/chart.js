(function () {
  function fit(canvas) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 358;
    const h = canvas.clientHeight || 180;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function line(canvas, points, marks, startValue) {
    const { ctx, w, h } = fit(canvas);
    ctx.clearRect(0, 0, w, h);
    const pad = 16;
    if (!points || points.length < 2) {
      ctx.setLineDash([4, 5]);
      ctx.strokeStyle = "#3a3a3c";
      ctx.beginPath();
      ctx.moveTo(pad, h * 0.62);
      ctx.lineTo(w - pad, h * 0.62);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#8e8e93";
      ctx.font = "13px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText("Waiting for the first trade", pad, h * 0.62 - 12);
      return;
    }
    const values = points.map((point) => Number(point.v));
    let min = Math.min(startValue, ...values);
    let max = Math.max(startValue, ...values);
    if (min === max) {
      min -= 1;
      max += 1;
    }
    const t0 = Date.parse(points[0].t);
    const t1 = Date.parse(points[points.length - 1].t) || t0 + 1;
    const xOf = (ts) => pad + ((Date.parse(ts) - t0) / (t1 - t0 || 1)) * (w - pad * 2);
    const yOf = (value) => pad + (1 - (value - min) / (max - min)) * (h - pad * 2 - 18);
    const startY = yOf(startValue);
    ctx.setLineDash([4, 5]);
    ctx.strokeStyle = "#3a3a3c";
    ctx.beginPath();
    ctx.moveTo(pad, startY);
    ctx.lineTo(w - pad, startY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xOf(point.t);
      const y = yOf(Number(point.v));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#00d35a";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.lineTo(xOf(points[points.length - 1].t), h - pad);
    ctx.lineTo(xOf(points[0].t), h - pad);
    ctx.closePath();
    const fill = ctx.createLinearGradient(0, pad, 0, h);
    fill.addColorStop(0, "rgba(0,211,90,0.28)");
    fill.addColorStop(1, "rgba(0,211,90,0)");
    ctx.fillStyle = fill;
    ctx.fill();
    const rows = [];
    (marks || []).forEach((mark) => {
      const x = xOf(mark.t);
      let row = 0;
      while (rows[row] && Math.abs(rows[row] - x) < 72) row += 1;
      rows[row] = x;
      ctx.fillStyle = mark.kind === "buy" ? "#0a84ff" : mark.kind === "down" ? "#ff453a" : "#00d35a";
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText(mark.text || "", Math.min(x, w - 90), 14 + row * 14);
    });
  }

  function candles(canvas, bars, riskPrice) {
    const { ctx, w, h } = fit(canvas);
    ctx.clearRect(0, 0, w, h);
    if (!bars || !bars.length) {
      ctx.fillStyle = "#8e8e93";
      ctx.font = "13px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText("No prints yet", 12, h / 2);
      return;
    }
    const highs = bars.map((bar) => bar.h);
    const lows = bars.map((bar) => bar.l);
    if (riskPrice) {
      highs.push(riskPrice);
      lows.push(riskPrice);
    }
    let min = Math.min(...lows);
    let max = Math.max(...highs);
    if (min === max) {
      min -= 1;
      max += 1;
    }
    const yOf = (value) => 12 + (1 - (value - min) / (max - min)) * (h - 36);
    const slot = (w - 8) / bars.length;
    bars.forEach((bar, index) => {
      const x = 8 + index * slot + slot / 2;
      const up = bar.c >= bar.o;
      ctx.strokeStyle = up ? "#00d35a" : "#ff453a";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(x, yOf(bar.h));
      ctx.lineTo(x, yOf(bar.l));
      ctx.stroke();
      const top = yOf(Math.max(bar.o, bar.c));
      const bottom = yOf(Math.min(bar.o, bar.c));
      ctx.fillRect(x - Math.max(1, slot * 0.28), top, Math.max(2, slot * 0.56), Math.max(1, bottom - top));
    });
    const maxVol = Math.max(...bars.map((bar) => Math.abs(bar.v || 0)), 1);
    bars.forEach((bar, index) => {
      const x = 8 + index * slot;
      const height = (Math.abs(bar.v || 0) / maxVol) * 18;
      ctx.fillStyle = "rgba(142,142,147,0.45)";
      ctx.fillRect(x, h - height, Math.max(1, slot * 0.6), height);
    });
    if (riskPrice) {
      const y = yOf(riskPrice);
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "#ff9f0a";
      ctx.beginPath();
      ctx.moveTo(8, y);
      ctx.lineTo(w - 8, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#ff9f0a";
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText("Risk trailing stop", 8, Math.max(12, y - 4));
    }
  }

  window.DeskCharts = { line, candles };
})();
