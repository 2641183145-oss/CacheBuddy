/* ECG cell — the small heartbeat glyph in Cache Buddy's header.
 *
 * A single 44x44 cell where a bright "current" sweeps left to right on a
 * loop, like a monitor. It traces a flat line, except at one hotspot where
 * it spikes into a QRS complex — that spike is the beat.
 *
 *   guarding / renewing : the sweep fires the QRS as it crosses the hotspot
 *   a real renewal lands: an extra glow, so you see *that* beat
 *   busy                : same loop, amber
 *   waiting (armed, no message yet / cache gone): "standby" — a slow,
 *     low double ripple on a dotted trace, the head breathing: alive and
 *     listening, not beating yet
 *   stopped / not guarded: the line stays flat, but the current still sweeps
 *
 * Preview: ui/ecg_demo.html */
(function (global) {
  "use strict";

  const DEF = 44;              // css px, square
  const SWEEP = 1.7;           // seconds for one left -> right pass
  const REST = .95;            // pause at the edge before the next pass
  const SPOT = .64;            // where the beat sits across the cell
  const QRS = .085;            // half-width of the spike, fraction of width

  function Ecg(canvas, opts) {
    this.canvas = canvas;
    this.opts = Object.assign({
      size: DEF,
      color: "#3FA46A",
      flat: false,             // dead flat line (stopped / not guarded)
      beats: [],               // epoch seconds of real renewals
      speed: SWEEP,
      rest: REST,              // idle gap between passes
      wave: "qrs",             // "qrs" (a real turn) or "wide" (busy)
      glow: .55,               // seconds a landed beat stays lit
    }, opts || {});
    this.size = this.opts.size;
    this.dpr = global.devicePixelRatio || 1;
    canvas.width = Math.round(this.size * this.dpr);
    canvas.height = Math.round(this.size * this.dpr);
    this.ctx = canvas.getContext("2d");
    this._flash = 0;           // epoch of the last beat we lit up
  }

  /* waveform height at u (0..1 across the cell), 1 = baseline.
     "qrs" is the sharp heartbeat; "wide" is the busy shape — a lower,
     broader beat with a lead-in bump, so the two read differently at a
     glance even in the 44px cell. */
  Ecg.prototype.wave = function (u) {
    if (this.opts.flat) return 0;              // no spike at all
    const g = x => Math.exp(-x * x);
    if (this.opts.wave === "wait") {
      const d = (u - SPOT) / (QRS * 1.6);
      if (d < -3.2 || d > 4.2) return 0;
      return 0.24 * g(d / .9) + 0.13 * g((d - 2.1) / .75);   // soft lub-dub
    }
    if (this.opts.wave === "wide") {
      const d = (u - SPOT) / (QRS * 1.7);
      if (d < -3.4 || d > 3.4) return 0;
      return 0.30 * g((d + 1.9) / .85)         // P bump
        + 0.74 * g(d / 1.25)                   // broad R
        + 0.34 * g((d - 1.85) / .95);          // T bump
    }
    const d = (u - SPOT) / QRS;
    if (d < -3 || d > 3) return 0;
    return -0.18 * g((d + 1.9) / .7) + 1 * g(d / .85)
      - 0.34 * g((d - 1.7) / .8);
  };

  Ecg.prototype.draw = function (now) {
    const o = this.opts, ctx = this.ctx, S = this.size;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, S, S);
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.strokeStyle = o.color;

    const pad = 3;
    const w = S - pad * 2;
    const mid = S * .62;                       // the flat baseline
    const wide = o.wave === "wide";
    const wait = o.wave === "wait";
    const amp = (mid - pad - 3) * (wide ? .92 : 1);
    const yAt = u => mid - this.wave(u) * amp;

    // a real renewal just landed? light the whole cell up for a moment
    const last = o.beats.length ? o.beats[o.beats.length - 1] : 0;
    const lit = last > this._flash && now - last < 2.5;
    if (lit) this._flash = last;
    const streak = lit ? Math.max(0, 1 - (now - last) / o.glow) : 0;

    // the sweep head — it crosses, then waits at the edge before the
    // next pass, so the beat doesn't restart the instant it finishes
    const cycle = o.speed + o.rest;
    const t = ((now % cycle) + cycle) % cycle;
    const p = Math.min(1, t / o.speed);
    const x = pad + p * w;

    // faint full trace first (what a monitor leaves behind)
    ctx.globalAlpha = (wait ? .32 : .18) + .5 * streak;
    ctx.lineWidth = 1.3;
    ctx.setLineDash(wait ? [1.6, 2.6] : []);    // standby: a dotted trace
    ctx.beginPath();
    for (let i = 0; i <= w; i++) {
      const u = i / w, px = pad + i, py = yAt(u);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.stroke();

    // the bright part the current has already passed, this pass
    ctx.setLineDash([]);
    ctx.globalAlpha = wait ? .75 : .95;
    ctx.lineWidth = 1.7;
    ctx.beginPath();
    for (let i = 0; i <= x - pad; i++) {
      const u = i / w, px = pad + i, py = yAt(u);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.stroke();

    // the head itself: a solid dot, a touch bigger as it crosses the beat
    // (no halo — flat colour only)
    const uHead = (x - pad) / w;
    const near = Math.abs(uHead - SPOT) < QRS * 2.2;
    ctx.globalAlpha = 1;
    ctx.fillStyle = o.color;
    ctx.beginPath();
    // standby: the head breathes (slowly swells and settles)
    const rad = wait ? 1.6 + .7 * (.5 + .5 * Math.sin(now * 2.4))
      : near || streak ? 2.1 : 1.5;
    ctx.arc(x, yAt(uHead), rad, 0, Math.PI * 2);
    ctx.fill();
  };

  Ecg.SIZE = DEF;
  global.Ecg = Ecg;
})(window);
