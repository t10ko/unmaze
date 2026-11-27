#!/usr/bin/env node
// Captures the README screenshots and GIFs from the demo board with headless Chrome.
// No npm packages: it drives Chrome over the DevTools protocol with Node's built-in WebSocket
// (Node 22+), and turns frames into GIFs with ffmpeg and ImageMagick.
//
// Usage (after building the demo board, see README "Try the demo"):
//   python3 -m http.server 8765 --directory .unmaze &
//   node tools/capture-readme-media.mjs http://localhost:8765/demo/unmaze-review.html assets

import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const CHROME = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BOARD_URL = process.argv[2];
const OUT_DIR = resolve(process.argv[3] || "assets");
const ONLY = (process.env.ONLY || "").split(",").filter(Boolean);
if (!BOARD_URL) {
  console.error("Usage: node tools/capture-readme-media.mjs <board-url> [out-dir]");
  process.exit(1);
}
mkdirSync(OUT_DIR, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function launch() {
  const profile = mkdtempSync(join(tmpdir(), "unmaze-capture-"));
  const proc = spawn(
    CHROME,
    [
      "--headless=new",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--hide-scrollbars",
      "--font-render-hinting=none",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  let port = null;
  for (let i = 0; i < 150 && !port; i++) {
    await sleep(100);
    try { port = readFileSync(join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]; } catch { /* not ready */ }
  }
  if (!port) throw new Error("Chrome did not start");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) rej(new Error(msg.error.message)); else res(msg.result);
    } else if (msg.method) listeners.slice().forEach((l) => l(msg));
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    const i = ++id;
    pending.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  const once = (method) => new Promise((res) => {
    const l = (msg) => { if (msg.method === method) { listeners.splice(listeners.indexOf(l), 1); res(msg.params); } };
    listeners.push(l);
  });
  await send("Page.enable");
  await send("Runtime.enable");
  return { proc, profile, ws, send, once };
}

const CURSOR_JS = `(() => {
  if (document.getElementById("__cur")) return;
  const d = document.createElement("div");
  d.id = "__cur";
  d.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M4 2.5l6.8 18 2.4-7.3 7.3-2.4z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  Object.assign(d.style, { position: "fixed", left: "-60px", top: "-60px", zIndex: "2147483647", pointerEvents: "none",
    filter: "drop-shadow(0 1px 2px rgba(0,0,0,.55))" });
  document.documentElement.appendChild(d);
})()`;

async function js(c, expression) {
  const r = await c.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function viewport(c, width, height, { mobile = false, scheme = "dark" } = {}) {
  await c.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
  await c.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
}
async function go(c, url) {
  const loaded = c.once("Page.loadEventFired");
  await c.send("Page.navigate", { url });
  await loaded;
  await sleep(500);
  await js(c, CURSOR_JS);
}
async function shot(c, file, clip) {
  const params = { format: "png" };
  if (clip) { params.clip = { ...clip, scale: 1 }; params.captureBeyondViewport = true; }
  const r = await c.send("Page.captureScreenshot", params);
  writeFileSync(file, Buffer.from(r.data, "base64"));
}
async function moveTo(c, x, y) {
  await js(c, `(() => { const d = document.getElementById("__cur"); if (!d) return;
    const host = document.querySelector("dialog[open]") || document.documentElement;
    if (d.parentNode !== host) host.appendChild(d);
    d.style.left = "${x - 3}px"; d.style.top = "${y - 2}px"; })()`);
  await c.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await sleep(120);
}
async function click(c, x, y) {
  await moveTo(c, x, y);
  await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await c.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  await sleep(350);
}
// Center of the first element matching `selector` (optionally whose text matches), scrolled into view.
async function spot(c, selector, { text = null, block = "center", offsetY = 0, scroll = true, index = 0 } = {}) {
  const r = await js(c, `(() => {
    let els = Array.from(document.querySelectorAll(${JSON.stringify(selector)}));
    ${text ? `els = els.filter((e) => e.textContent.includes(${JSON.stringify(text)}));` : ""}
    els = els.filter((e) => e.getClientRects().length);
    const e = els[${index}];
    if (!e) return null;
    ${scroll ? `e.scrollIntoView({ block: ${JSON.stringify(block)} }); if (${offsetY}) window.scrollBy(0, ${offsetY});` : ""}
    const b = e.getBoundingClientRect();
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2), left: b.left, top: b.top, width: b.width, height: b.height };
  })()`);
  if (!r) throw new Error(`Not found: ${selector}${text ? ` (${text})` : ""}`);
  await sleep(250);
  return r;
}
async function clipOf(c, selector, pad = 12, opts = {}) {
  const r = await js(c, `(() => {
    let els = Array.from(document.querySelectorAll(${JSON.stringify(selector)})).filter((e) => e.getClientRects().length);
    const e = els[${opts.index || 0}];
    if (!e) return null;
    e.scrollIntoView({ block: "start" });
    window.scrollBy(0, -80);
    const b = e.getBoundingClientRect();
    return { x: b.left + window.scrollX, y: b.top + window.scrollY, width: b.width, height: b.height };
  })()`);
  if (!r) throw new Error(`Not found: ${selector}`);
  await sleep(300);
  return {
    x: Math.max(0, r.x - pad),
    y: Math.max(0, r.y - pad),
    width: r.width + pad * 2,
    height: Math.min(r.height + pad * 2, opts.maxHeight || 4000),
  };
}
async function scrollBy(c, dy) { await js(c, `window.scrollBy(0, ${dy})`); await sleep(200); }
async function panel(c, index) {
  await js(c, `(() => { const b = document.querySelector('.arc-subtab[data-panel-index="${index}"]') ||
    document.querySelector('.arc-tab[data-panel-index="${index}"]'); b.click(); window.scrollTo(0, 0); })()`);
  await sleep(450);
}

class Gif {
  constructor(name) {
    this.name = name;
    this.dir = mkdtempSync(join(tmpdir(), `unmaze-gif-${name}-`));
    this.frames = [];
  }
  async frame(c, hold, clip) {
    const f = join(this.dir, `f${String(this.frames.length).padStart(3, "0")}.png`);
    await shot(c, f, clip);
    this.frames.push([f, hold]);
  }
  async scrollFrames(c, dy, steps = 4, hold = 0.09) {
    for (let i = 0; i < steps; i++) { await scrollBy(c, dy / steps); await this.frame(c, hold); }
  }
  build(width = 1100) {
    const list = this.frames.map(([f, d]) => `file '${f}'\nduration ${d}`).join("\n") + `\nfile '${this.frames.at(-1)[0]}'\n`;
    const listFile = join(this.dir, "list.txt");
    writeFileSync(listFile, list);
    const out = join(OUT_DIR, `${this.name}.gif`);
    execFileSync("ffmpeg", [
      "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listFile,
      "-vf", `fps=10,scale=${width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=200:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`,
      "-loop", "0", out,
    ]);
    rmSync(this.dir, { recursive: true, force: true });
    console.log(`  wrote ${out}`);
  }
}

function png(name) { return join(OUT_DIR, `${name}.png`); }
function optimizePng(file, width) {
  const args = [file];
  if (width) args.push("-resize", `${width}x>`);
  args.push("-strip", "-define", "png:compression-level=9", file);
  execFileSync("magick", args);
}

const W = 1440, H = 860;
const scenes = {
  async walkthrough(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    const g = new Gif("unmaze-walkthrough");
    await moveTo(c, 700, 300);
    await g.frame(c, 2.4);
    await g.scrollFrames(c, 520, 5);
    await g.frame(c, 2.2);
    const ch = await spot(c, ".arc-chapter-btn", { index: 0, scroll: false });
    await moveTo(c, ch.x, ch.y); await g.frame(c, 0.6);
    await click(c, ch.x, ch.y); await g.frame(c, 2.0);
    const s2 = await spot(c, ".arc-subtab", { index: 1, scroll: false });
    await moveTo(c, s2.x, s2.y); await g.frame(c, 0.5);
    await click(c, s2.x, s2.y); await g.frame(c, 1.8);
    const wn = await spot(c, ".step-panel:not([hidden]) .hl-note-title", { block: "start", offsetY: -110 });
    await moveTo(c, wn.x, wn.y); await g.frame(c, 0.8);
    await click(c, wn.x, wn.y); await g.frame(c, 2.6);
    await g.scrollFrames(c, 380, 4);
    await g.frame(c, 2.6);
    const ch2 = await spot(c, ".arc-chapter-btn", { index: 1, scroll: false });
    await moveTo(c, ch2.x, ch2.y); await g.frame(c, 0.5);
    await click(c, ch2.x, ch2.y); await g.frame(c, 2.4);
    g.build(1100);
  },

  async overview(c) {
    await viewport(c, W, 1000); await go(c, BOARD_URL);
    const clip = await clipOf(c, ".step-panel:not([hidden]) .this-pr-block", 14, { maxHeight: 1500 });
    await shot(c, png("overview-this-pr"), clip);
    optimizePng(png("overview-this-pr"), 1100);
    console.log("  wrote overview-this-pr.png");
  },

  async diagram(c) {
    await viewport(c, W, 1000); await go(c, BOARD_URL);
    const clip = await clipOf(c, ".dg-block", 14);
    await shot(c, png("parts-diagram"), clip);
    optimizePng(png("parts-diagram"), 1000);
    console.log("  wrote parts-diagram.png");
  },

  async visuals(c) {
    await viewport(c, W, 1000); await go(c, BOARD_URL);
    const clip = await clipOf(c, ".step-panel:not([hidden]) .visual-card", 10, { maxHeight: 900 });
    await shot(c, png("red-marked-screenshots"), clip);
    optimizePng(png("red-marked-screenshots"), 1000);
    console.log("  wrote red-marked-screenshots.png");
  },

  async notes(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    await panel(c, 2);
    const g = new Gif("code-notes");
    await spot(c, ".step-panel:not([hidden]) .hl-note", { block: "start", offsetY: -120 });
    await moveTo(c, 1300, 200);
    await g.frame(c, 2.0);
    for (const i of [0, 1]) {
      const n = await spot(c, ".step-panel:not([hidden]) .hl-note-title", { index: i, scroll: false });
      await moveTo(c, n.x, n.y); await g.frame(c, 0.9);
      await click(c, n.x, n.y); await g.frame(c, 2.4);
    }
    const row = await spot(c, '.step-panel:not([hidden]) .dl.dl-has-hl[data-hl-id$="hl1"]', { scroll: false });
    await moveTo(c, row.x - 150, row.y); await g.frame(c, 1.8);
    const tog = await spot(c, ".step-panel:not([hidden]) .notes-toggle-btn", { block: "center" });
    await moveTo(c, tog.x, tog.y); await g.frame(c, 0.8);
    await click(c, tog.x, tog.y); await g.frame(c, 2.0);
    await click(c, tog.x, tog.y); await g.frame(c, 1.6);
    g.build(1100);
  },

  async timeline(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    await panel(c, 2);
    const g = new Gif("file-timeline");
    const card = await spot(c, ".step-panel:not([hidden]) .hunk-card .timeline-badge", { block: "start", offsetY: -140 });
    await moveTo(c, card.x, card.y); await g.frame(c, 2.4);
    const btn = await spot(c, ".step-panel:not([hidden]) .snapshot-toggle-btn", { scroll: false });
    await moveTo(c, btn.x, btn.y); await g.frame(c, 0.8);
    await click(c, btn.x, btn.y); await g.frame(c, 2.6);
    await g.scrollFrames(c, 300, 3);
    await g.frame(c, 1.6);
    await spot(c, ".step-panel:not([hidden]) .snapshot-toggle-btn", { block: "center" });
    const b2 = await spot(c, ".step-panel:not([hidden]) .snapshot-toggle-btn", { scroll: false });
    await click(c, b2.x, b2.y); await g.frame(c, 1.8);
    g.build(1100);
  },

  async concepts(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    const g = new Gif("concept-links");
    const l = await spot(c, ".step-panel:not([hidden]) .concept-link", { text: "Postgres lock table", block: "center" });
    await moveTo(c, l.x - 120, l.y + 60); await g.frame(c, 1.4);
    await moveTo(c, l.x, l.y); await g.frame(c, 0.8);
    await click(c, l.x, l.y); await g.frame(c, 3.4);
    await c.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await c.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await sleep(300);
    const l2 = await spot(c, ".step-panel:not([hidden]) .concept-link", { text: "active locks", scroll: false });
    await moveTo(c, l2.x, l2.y); await g.frame(c, 0.6);
    await click(c, l2.x, l2.y); await g.frame(c, 3.2);
    g.build(1100);
  },

  async filter(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    const g = new Gif("area-filter");
    await moveTo(c, 700, 400); await g.frame(c, 1.4);
    const b = await spot(c, "#scope-filter-btn", { scroll: false });
    await moveTo(c, b.x, b.y); await g.frame(c, 0.6);
    await click(c, b.x, b.y); await g.frame(c, 2.2);
    const only = await spot(c, "#scope-filter-popover .scope-only-btn[data-only-surface]", { index: 1, scroll: false });
    await js(c, `document.querySelectorAll('#scope-filter-popover .scope-only-btn').forEach((e) => e.style.opacity = '1')`);
    await moveTo(c, only.x, only.y); await g.frame(c, 1.0);
    await click(c, only.x, only.y); await g.frame(c, 2.6);
    await click(c, 1100, 600); await g.frame(c, 2.6);
    const reset = await spot(c, "#scope-reset-top", { scroll: false });
    await moveTo(c, reset.x, reset.y); await g.frame(c, 0.6);
    await click(c, reset.x, reset.y); await g.frame(c, 1.6);
    g.build(1100);
  },

  async github(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    const g = new Gif("github-review-status");
    const pills = await spot(c, "#gh-status .gh-pill", { index: 0, scroll: false });
    await moveTo(c, pills.x, pills.y + 120); await g.frame(c, 1.2);
    const checks = await spot(c, '#gh-status .gh-pill[data-gh-open="checks"]', { scroll: false });
    await moveTo(c, checks.x, checks.y); await g.frame(c, 0.8);
    await click(c, checks.x, checks.y); await g.frame(c, 2.8);
    await js(c, `document.getElementById("gh-sec-threads").scrollIntoView({ block: "start" })`); await sleep(200);
    const show = await spot(c, "#gh-dialog [data-gh-jump]", { scroll: false });
    await moveTo(c, show.x, show.y); await g.frame(c, 1.8);
    await click(c, show.x, show.y); await sleep(900); await g.frame(c, 2.6);
    const row = await spot(c, '.step-panel:not([hidden]) .hunk-card[data-path$="store.ts"] .diff-view .dl[data-pn="27"]', { scroll: false });
    await moveTo(c, row.x - 300, row.y); await g.frame(c, 0.8);
    const plus = await spot(c, ".gh-add-btn", { scroll: false });
    await moveTo(c, plus.x, plus.y); await g.frame(c, 0.8);
    await click(c, plus.x, plus.y); await g.frame(c, 0.8);
    const text = "Should PROCESS also return the lease expiry so the caller can log it?";
    for (const n of [18, 40, text.length]) {
      await js(c, `document.querySelector(".gh-composer textarea").value = ${JSON.stringify(text)}.slice(0, ${n})`);
      await g.frame(c, n === text.length ? 1.0 : 0.25);
    }
    const copy = await spot(c, '.gh-composer [data-gh-action="copy-line"]', { scroll: false });
    await moveTo(c, copy.x, copy.y); await g.frame(c, 2.6);
    g.build(1100);
  },

  async context(c) {
    await viewport(c, W, H); await go(c, BOARD_URL);
    await panel(c, 4);
    const g = new Gif("expand-context");
    const more = await spot(c, ".step-panel:not([hidden]) .ctx-more-btn", { block: "center", offsetY: 120 });
    await moveTo(c, more.x + 200, more.y + 80); await g.frame(c, 1.6);
    await moveTo(c, more.x, more.y); await g.frame(c, 0.7);
    await click(c, more.x, more.y); await g.frame(c, 2.2);
    const row = await spot(c, ".step-panel:not([hidden]) .dl.dl-add", { index: 2, scroll: false });
    await moveTo(c, row.x + 300, row.y); await g.frame(c, 1.4);
    const jump = await spot(c, ".step-panel:not([hidden]) .dl:hover .row-pr-jump", { scroll: false });
    await moveTo(c, jump.x, jump.y); await g.frame(c, 2.4);
    g.build(1100);
  },

  async mobile(c) {
    const shots = [];
    await viewport(c, 390, 844, { mobile: true }); await go(c, BOARD_URL);
    await js(c, `document.getElementById("__cur").remove()`);
    await js(c, `window.scrollTo(0, 0)`); await sleep(200);
    shots.push(join(OUT_DIR, "_m1.png")); await shot(c, shots[0]);
    await panel(c, 2);
    await js(c, `document.querySelector(".step-panel:not([hidden]) .hl-note-head").click()`);
    await spot(c, ".step-panel:not([hidden]) .hl-note", { block: "start", offsetY: -80 });
    shots.push(join(OUT_DIR, "_m2.png")); await shot(c, shots[1]);
    await spot(c, ".step-panel:not([hidden]) .gh-thread", { block: "start", offsetY: -80 });
    shots.push(join(OUT_DIR, "_m3.png")); await shot(c, shots[2]);
    execFileSync("magick", [
      ...shots, "-crop", "390x844+0+0", "+repage", "-bordercolor", "#0b0f16", "-border", "14",
      "-background", "#0b0f16", "-gravity", "north", "+append", "-resize", "1100x>", "-strip", png("mobile"),
    ]);
    shots.forEach((s) => rmSync(s, { force: true }));
    console.log("  wrote mobile.png");
  },
};

const c = await launch();
try {
  for (const [name, fn] of Object.entries(scenes)) {
    if (ONLY.length && !ONLY.includes(name)) continue;
    console.log(`scene: ${name}`);
    await fn(c);
  }
} finally {
  c.ws.close();
  const exited = new Promise((r) => c.proc.once("exit", r));
  c.proc.kill();
  await Promise.race([exited, sleep(3000)]);
  try { rmSync(c.profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch { /* temp dir */ }
}
