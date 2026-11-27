#!/usr/bin/env node
// Runs the free-shipping example's end-to-end checks against the stand-in cart page
// (app/cart.html) in headless Chrome, records one GIF per step, and writes results.json in the
// format of skills/unmaze/references/e2e-results.md. Every step's result comes from reading
// the page after the action, so a broken cart page produces a failed check.
// No npm packages: Chrome over the DevTools protocol with Node's built-in WebSocket (Node 22+),
// and GIFs from ffmpeg.
//
//   node examples/free-shipping/e2e/record.mjs

import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = pathToFileURL(join(HERE, "app", "cart.html")).href;
const MEDIA = join(HERE, "media");
const CHROME = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const VIEW = { width: 1200, height: 720 };
const GIF_WIDTH = 760;
const FRAME_MS = 100;
// The patch this example reviews has no commit, so the run names a fixed demo commit.
const TESTED_SHA = process.env.TESTED_SHA || "4f2c9a1d7e3b";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function launch() {
  const profile = mkdtempSync(join(tmpdir(), "unmaze-e2e-"));
  const proc = spawn(CHROME, [
    "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--font-render-hinting=none",
    "about:blank",
  ], { stdio: "ignore" });
  let port = null;
  for (let i = 0; i < 150 && !port; i++) {
    await sleep(100);
    try { port = readFileSync(join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]; } catch { /* not ready */ }
  }
  if (!port) throw new Error("Chrome did not start");
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) rej(new Error(msg.error.message)); else res(msg.result);
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    const msgId = ++id;
    pending.set(msgId, { res, rej });
    ws.send(JSON.stringify({ id: msgId, method, params }));
  });
  const close = async () => {
    ws.close();
    const exited = new Promise((res) => proc.once("exit", res));
    proc.kill();
    await exited;
    rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  };
  return { send, close };
}

// A visible cursor, click ripple and caption, so a GIF shows what was done, not only the result.
const OVERLAY = `
  (() => {
    if (window.__e2e) return;
    const css = document.createElement("style");
    css.textContent = \`
      #e2e-cursor { position: fixed; z-index: 99999; width: 22px; height: 22px; left: 0; top: 0; pointer-events: none;
        transition: transform .45s cubic-bezier(.3,.7,.3,1); }
      #e2e-cursor svg { filter: drop-shadow(0 1px 2px rgba(0,0,0,.35)); }
      .e2e-ripple { position: fixed; z-index: 99998; width: 34px; height: 34px; margin: -17px 0 0 -17px; border-radius: 50%;
        border: 3px solid #e8590c; pointer-events: none; animation: e2e-ripple .6s ease-out forwards; }
      @keyframes e2e-ripple { from { transform: scale(.4); opacity: 1; } to { transform: scale(1.6); opacity: 0; } }
      #e2e-caption { position: fixed; z-index: 99997; left: 50%; bottom: 18px; transform: translateX(-50%); max-width: 80%;
        padding: 9px 16px; border-radius: 10px; background: rgba(20,20,20,.86); color: #fff; font: 600 16px/1.35 -apple-system, sans-serif; }
    \`;
    document.head.appendChild(css);
    const cursor = document.createElement("div");
    cursor.id = "e2e-cursor";
    cursor.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 2l14 8-6 1.6L8.4 18z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(cursor);
    const caption = document.createElement("div");
    caption.id = "e2e-caption";
    document.body.appendChild(caption);
    window.__e2e = {
      move(x, y) { cursor.style.transform = "translate(" + x + "px," + y + "px)"; },
      ripple(x, y) { const r = document.createElement("div"); r.className = "e2e-ripple"; r.style.left = x + "px"; r.style.top = y + "px"; document.body.appendChild(r); setTimeout(() => r.remove(), 700); },
      say(text) { caption.textContent = text; caption.style.display = text ? "block" : "none"; },
    };
    window.__e2e.move(${VIEW.width - 140}, ${VIEW.height - 120});
  })();
`;

function browser(send) {
  const js = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " in " + expression);
    return r.result.value;
  };
  let frames = null;
  let recorder = null;
  return {
    js,
    async open(hash) {
      await send("Page.navigate", { url: `${APP}#${hash}` });
      await sleep(400);
      await send("Page.reload", {});
      await sleep(500);
      await js(OVERLAY);
    },
    async startRecording(caption) {
      await js(`window.__e2e.say(${JSON.stringify(caption)})`);
      frames = [];
      let running = true;
      recorder = (async () => {
        while (running) {
          const started = Date.now();
          const shot = await send("Page.captureScreenshot", { format: "png" });
          frames.push(shot.data);
          await sleep(Math.max(0, FRAME_MS - (Date.now() - started)));
        }
      })();
      recorder.stop = () => { running = false; };
      await sleep(700);
    },
    async stopRecording(file) {
      await sleep(1100);
      recorder.stop();
      await recorder;
      const dir = mkdtempSync(join(tmpdir(), "unmaze-frames-"));
      frames.forEach((data, i) => writeFileSync(join(dir, `f${String(i).padStart(4, "0")}.png`), Buffer.from(data, "base64")));
      const filters = `fps=10,scale=${GIF_WIDTH}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`;
      execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-framerate", String(1000 / FRAME_MS),
        "-i", join(dir, "f%04d.png"), "-vf", filters, "-loop", "0", join(MEDIA, file)]);
      rmSync(dir, { recursive: true, force: true });
      await js(`window.__e2e.say("")`);
    },
    async click(selector) {
      const box = await js(`(() => { const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return null; el.scrollIntoView({ block: "center" }); const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
      if (!box) throw new Error(`Nothing matches ${selector}`);
      await js(`window.__e2e.move(${box.x - 3}, ${box.y - 2})`);
      await sleep(550);
      await js(`window.__e2e.ripple(${box.x}, ${box.y})`);
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 });
      await sleep(450);
    },
    async type(selector, text) {
      await this.click(selector);
      for (const ch of text) { await send("Input.insertText", { text: ch }); await sleep(90); }
    },
    async choose(selector, value) {
      await this.click(selector);
      await js(`(() => { const el = document.querySelector(${JSON.stringify(selector)});
        el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event("change", { bubbles: true })); })()`);
      await sleep(400);
    },
    // What a shopper sees: the banner text (or none), the shipping line and the total.
    read() {
      return js(`(() => ({
        banner: (document.querySelector("#banner [role=status]") || {}).textContent || null,
        shipping: document.getElementById("shipping").textContent,
        total: document.getElementById("total").textContent,
      }))()`);
    },
  };
}

const FILES = {
  quote: "src/cart/shipping.ts",
  totals: "src/cart/totals.ts",
  banner: "src/components/FreeShippingBanner.tsx",
  page: "src/pages/CartPage.tsx",
};

// Each check: what we do, what a shopper should see, and how the page proves it.
const SCENARIOS = [
  {
    id: "S1", title: "Under $50, the banner says how much is left, and adding an item unlocks free shipping",
    why: "If the amount is wrong, shoppers either chase a goal they already hit or pay shipping they shouldn't.",
    files: [FILES.quote, FILES.banner, FILES.totals, FILES.page], cart: "under",
    steps: [
      { action: "Open a cart with a $22.50 tote and a $15.00 mug", expected: "\"Add $12.50 more to get free shipping.\" and $5.99 shipping",
        run: async () => {}, check: (s) => s.banner === "Add $12.50 more to get free shipping." && s.shipping === "$5.99" },
      { action: "Add a second mug with the + button", expected: "\"You've got free shipping!\", $0.00 shipping, $52.50 total",
        run: (b) => b.click('[data-item="mug"] .plus'),
        check: (s) => s.banner === "You've got free shipping!" && s.shipping === "$0.00" && s.total === "$52.50" },
    ],
  },
  {
    id: "S2", title: "A cart of exactly $50.00 ships free",
    why: "The boundary is where an off-by-one bug would charge shipping on a qualifying order.",
    files: [FILES.quote, FILES.banner], cart: "exact",
    steps: [
      { action: "Open a cart that adds up to exactly $50.00", expected: "\"You've got free shipping!\" and $0.00 shipping",
        run: async () => {}, check: (s) => s.banner === "You've got free shipping!" && s.shipping === "$0.00" && s.total === "$50.00" },
    ],
  },
  {
    id: "S3", title: "A coupon that takes the order under $50 brings shipping back",
    why: "Free shipping counts what the shopper pays after the coupon, not the list price.",
    files: [FILES.quote, FILES.totals, FILES.banner], cart: "coupon",
    steps: [
      { action: "Open a $55.00 cart", expected: "\"You've got free shipping!\"",
        run: async () => {}, check: (s) => s.banner === "You've got free shipping!" && s.shipping === "$0.00" },
      { action: "Apply the $10 coupon SAVE10", expected: "\"Add $5.00 more to get free shipping.\", $5.99 shipping, $50.99 total",
        run: async (b) => { await b.type("#coupon", "SAVE10"); await b.click("#apply"); },
        check: (s) => s.banner === "Add $5.00 more to get free shipping." && s.shipping === "$5.99" && s.total === "$50.99" },
    ],
  },
  {
    id: "S4", title: "Gift cards don't count toward free shipping, and a gift-card-only cart ships nothing",
    why: "Gift cards are emailed, so counting them would hand out free shipping on small physical orders.",
    files: [FILES.quote, FILES.totals, FILES.banner], cart: "giftcard",
    steps: [
      { action: "Open a cart with a mug, a notebook and a $25.00 gift card", expected: "\"Add $22.50 more to get free shipping.\" even though the subtotal is $52.50",
        run: async () => {}, check: (s) => s.banner === "Add $22.50 more to get free shipping." && s.shipping === "$5.99" },
      { action: "Remove the mug and the notebook", expected: "No banner and $0.00 shipping, because nothing ships",
        run: async (b) => { await b.click('[data-item="mug"] .remove'); await b.click('[data-item="notebook"] .remove'); },
        check: (s) => s.banner === null && s.shipping === "$0.00" && s.total === "$25.00" },
    ],
  },
  {
    id: "S5", title: "Bulky items keep their delivery fee when shipping is free",
    why: "Free shipping covers the standard fee only; dropping the bulky fee would lose money on every lamp.",
    files: [FILES.quote, FILES.banner], cart: "bulky",
    steps: [
      { action: "Open a cart with a $54.00 floor lamp that has a $15.00 bulky fee", expected: "\"You've got free shipping! Bulky items still have a delivery fee.\" and $15.00 shipping",
        run: async () => {}, check: (s) => s.banner === "You've got free shipping! Bulky items still have a delivery fee." && s.shipping === "$15.00" },
    ],
  },
  {
    id: "S6", title: "Orders outside the US pay international shipping and see no banner",
    why: "Free shipping is US-only, so other countries must not be promised it.",
    files: [FILES.quote, FILES.banner, FILES.page], cart: "under",
    steps: [
      { action: "Switch \"Ship to\" from United States to Canada", expected: "The banner disappears and shipping is $19.99",
        run: (b) => b.choose("#country", "CA"), check: (s) => s.banner === null && s.shipping === "$19.99" },
    ],
  },
];

const SKIPPED = {
  id: "S7", title: "Checkout charges the same shipping the cart shows", kind: "api", status: "skipped",
  why: "If checkout priced shipping on its own, shoppers would see one number and pay another.",
  note: "This demo has no checkout backend, so there was no order to place.",
  files: [FILES.totals],
  steps: [{ action: "Place an order for the $37.50 cart through the checkout API", expected: "The order charges $5.99 shipping", status: "skipped" }],
};

async function main() {
  mkdirSync(MEDIA, { recursive: true });
  const { send, close } = await launch();
  const b = browser(send);
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: VIEW.width, height: VIEW.height, deviceScaleFactor: 1, mobile: false });
  const scenarios = [];
  try {
    for (const sc of SCENARIOS) {
      await b.open(`cart=${sc.cart}`);
      const steps = [];
      for (const [i, step] of sc.steps.entries()) {
        const gif = `${sc.id}-step${i + 1}.gif`;
        await b.startRecording(step.action);
        await step.run(b);
        const seen = await b.read();
        await b.stopRecording(gif);
        const ok = step.check(seen);
        if (!ok) console.error(`${sc.id} step ${i + 1} saw ${JSON.stringify(seen)}`);
        steps.push({ action: step.action, expected: step.expected, status: ok ? "pass" : "fail", gif: `media/${gif}` });
      }
      const failed = steps.filter((s) => s.status === "fail").length;
      scenarios.push({
        id: sc.id, title: sc.title, why: sc.why, kind: "browser", status: failed ? "fail" : "pass",
        note: failed ? `${failed} step(s) showed something other than expected; see the GIF.` : "",
        files: sc.files, steps,
      });
      console.log(`${sc.id} ${failed ? "FAIL" : "pass"}`);
    }
  } finally {
    await close();
  }
  scenarios.push(SKIPPED);
  const ran = scenarios.filter((s) => s.status !== "skipped");
  const passed = ran.filter((s) => s.status === "pass").length;
  const results = {
    tested_sha: TESTED_SHA,
    tested_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    environment: "Stand-in cart page with the example's pricing, headless Chrome at 1200×720",
    summary: `We clicked through the cart like a shopper would: ${passed} of ${ran.length} checks passed, and the checkout check was skipped because the demo has no backend.`,
    scenarios,
  };
  writeFileSync(join(HERE, "results.json"), JSON.stringify(results, null, 2) + "\n");
  if (passed !== ran.length) process.exitCode = 1;
}

main().catch((err) => { console.error(err); process.exit(1); });
