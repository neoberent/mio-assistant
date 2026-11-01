// Live2D avatar page. Talks to avatar.py through window.webkit.messageHandlers.avatar.
(() => {
  const post = (m) => { try { window.webkit.messageHandlers.avatar.postMessage(m); } catch (_) {} };
  const params = new URLSearchParams(location.search);
  window.addEventListener("error", (e) => post({ t: "log", text: "JSERR " + e.message + " @" + e.lineno }));
  window.addEventListener("unhandledrejection", (e) => post({ t: "log", text: "JSREJ " + e.reason }));
  const modelUrl = params.get("model");
  const zoom = parseFloat(params.get("zoom")) || 1;
  const focus = params.has("focus") ? parseFloat(params.get("focus")) : 1;
  const bubble = document.getElementById("bubble");
  const stateDot = document.getElementById("state");

  const app = new PIXI.Application({
    resizeTo: window, backgroundAlpha: 0, antialias: true,
    resolution: window.devicePixelRatio || 1, autoDensity: true,
  });
  document.body.appendChild(app.view);
  app.ticker.maxFPS = 60; // stage: smooth; during a game (compact layout) layout() drops it to 30

  let model = null;
  let mouth = 0, env = [], envFps = 30, envStart = 0, mouthLast = 0;
  let bubbleTimer = null;

  // Fit by what is actually drawn, not the model's canvas: some models (yuri) have feet hanging below it.
  let art = null;  // drawn area in canvas pixels at scale 1, measured once in the rest pose
  function measureArt() {
    // same mapping pixi-live2d-display draws with: model origin at the canvas centre (ignores CanvasOrigin)
    const im = model.internalModel, d = im.coreModel.getModel().drawables;
    const ppu = im.pixelsPerUnit, ox = im.originalWidth / 2, oy = im.originalHeight / 2;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i < d.count; i++) {
      if (d.opacities[i] < 0.05) continue;
      const v = d.vertexPositions[i];
      for (let k = 0; k < v.length; k += 2) {
        const px = ox + v[k] * ppu, py = oy - v[k + 1] * ppu;
        if (px < x0) x0 = px; if (px > x1) x1 = px; if (py < y0) y0 = py; if (py > y1) y1 = py;
      }
    }
    if (!isFinite(x0)) return null;
    const pad = 0.03 * (y1 - y0);  // room for hair and skirt swinging
    return { x: x0 - pad, y: y0 - pad, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad };
  }

  // While dragging, the window covers the whole monitor and `frame` is where her normal window would be.
  let frame = null;
  let lift = 0, quiet = false;  // lift: vertical offset for drop-ins and hops; quiet: don't spam bounds mid-animation
  function fit() {
    if (!model) return;
    const W = window.innerWidth, H = window.innerHeight;
    const { x: fx, y: fy, w, h } = frame || { x: 0, y: 0, w: W, h: H };
    const a = art || { x: 0, y: 0, w: model.internalModel.width, h: model.internalModel.height };
    const s = Math.min(w / a.w, h / a.h) * 0.98 * zoom;
    model.scale.set(s);
    model.anchor.set(0, 0);
    model.position.set(fx + w / 2 - (a.x + a.w / 2) * s, fy + lift + h * focus - (a.y + a.h * focus) * s);
    drawn = { x: model.position.x + a.x * s, y: model.position.y + a.y * s, w: a.w * s, h: a.h * s };
    if (drag || quiet) return;  // input region only when she's standing still
    const x = model.position.x + a.x * s, y = model.position.y + a.y * s;
    if (x + a.w * s <= 0 || y + a.h * s <= 0 || x >= W || y >= H) return;
    post({ t: "bounds", x: Math.max(0, x), y: Math.max(0, y),
           w: Math.min(W, x + a.w * s) - Math.max(0, x), h: Math.min(H, y + a.h * s) - Math.max(0, y) });
  }

  // ── recolor: tint parts of the texture atlas (e.g. hair) to match the desktop theme ──
  // rules: [{parts: "Hair", color: "#f5c2e7", hue_min, hue_max, sat_min, sat_max, strength}] — "parts" is a regex
  // matched against the part a drawable belongs to (or any of its ancestors) and the drawable id.
  function hexToHsl(hex) {
    const n = parseInt(hex.replace("#", ""), 16);
    return rgbToHsl((n >> 16) & 255, (n >> 8) & 255, n & 255);
  }
  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    if (mx === mn) return [0, 0, l];
    const d = mx - mn, s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    let h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [h * 60, s, l];
  }
  function hslToRgb(h, s, l) {
    const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [f(0) * 255, f(8) * 255, f(4) * 255];
  }

  function recolor(rules) {
    const m = model.internalModel.coreModel.getModel();
    const d = m.drawables, parts = m.parts;
    const partMatches = (pi, re) => {
      for (; pi >= 0; pi = parts.parentIndices[pi]) if (re.test(parts.ids[pi])) return true;
      return false;
    };
    const textures = model.textures;
    const canvases = {};
    for (const rule of rules) {
      const re = new RegExp(rule.parts, "i");
      const [th, ts, tl] = hexToHsl(rule.color);
      const byTex = {};
      for (let i = 0; i < d.count; i++) {
        if (!re.test(d.ids[i]) && !partMatches(d.parentPartIndices[i], re)) continue;
        (byTex[d.textureIndices[i]] ||= []).push(i);
      }
      for (const [ti, list] of Object.entries(byTex)) {
        const src = textures[ti].baseTexture.resource.source;
        let c = canvases[ti];
        if (!c) {
          c = canvases[ti] = document.createElement("canvas");
          c.width = src.width; c.height = src.height;
          c.getContext("2d").drawImage(src, 0, 0);
        }
        const W = c.width, H = c.height;
        // rasterize the selected drawables' UV triangles into a mask
        const mask = document.createElement("canvas");
        mask.width = W; mask.height = H;
        const mc = mask.getContext("2d");
        mc.fillStyle = "#fff";
        for (const i of list) {
          const uv = d.vertexUvs[i], idx = d.indices[i];
          mc.beginPath();
          for (let t = 0; t < idx.length; t += 3) {
            for (let k = 0; k < 3; k++) {
              const v = idx[t + k], x = uv[v * 2] * W, y = (1 - uv[v * 2 + 1]) * H;
              k ? mc.lineTo(x, y) : mc.moveTo(x, y);
            }
            mc.closePath();
          }
          mc.fill();
        }
        const ctx = c.getContext("2d");
        const img = ctx.getImageData(0, 0, W, H), px = img.data;
        const mk = mc.getImageData(0, 0, W, H).data;
        const sel = [];
        let lsum = 0;
        for (let p = 0; p < px.length; p += 4) {
          if (mk[p + 3] < 128 || px[p + 3] < 8) continue;
          const [h, s, l] = rgbToHsl(px[p], px[p + 1], px[p + 2]);
          if (rule.hue_min !== undefined) {  // a range like 330..15 wraps around red
            const inside = rule.hue_min <= rule.hue_max ? h >= rule.hue_min && h <= rule.hue_max
                                                        : h >= rule.hue_min || h <= rule.hue_max;
            if (!inside) continue;
          }
          if (rule.sat_max !== undefined && s > rule.sat_max) continue;
          if (rule.sat_min !== undefined && s < rule.sat_min) continue;
          if (l > 0.97 || l < 0.03) continue; // keep pure highlights / line art
          sel.push(p, l); lsum += l;
        }
        const lshift = sel.length ? tl - lsum / (sel.length / 2) : 0;
        const strength = rule.strength ?? 0.85;
        for (let j = 0; j < sel.length; j += 2) {
          const p = sel[j], l = Math.min(0.98, Math.max(0.02, sel[j + 1] + lshift * strength));
          const [r, g, b] = hslToRgb(th, ts, l);
          px[p] = r; px[p + 1] = g; px[p + 2] = b;
        }
        ctx.putImageData(img, 0, 0);
      }
    }
    for (const [ti, c] of Object.entries(canvases)) {
      const old = textures[ti];
      textures[ti] = PIXI.Texture.from(c, { mipmap: old.baseTexture.mipmap, alphaMode: old.baseTexture.alphaMode });
    }
  }

  function dumpDrawables(partRe) {  // debug: AVATAR_DEBUG=1 bin/assistant-avatar --show --dump Body
    const m = model.internalModel.coreModel.getModel(), d = m.drawables, parts = m.parts;
    const re = new RegExp(partRe, "i");
    for (let i = 0; i < d.count; i++) {
      let pi = d.parentPartIndices[i], chain = [];
      for (; pi >= 0; pi = parts.parentIndices[pi]) chain.push(parts.ids[pi]);
      if (!chain.some((p) => re.test(p))) continue;
      const v = d.vertexPositions[i], uv = d.vertexUvs[i];
      let ys = [], us = [], vs = [];
      for (let k = 0; k < v.length; k += 2) { ys.push(v[k + 1]); us.push(uv[k]); vs.push(uv[k + 1]); }
      const r = (a) => `${Math.min(...a).toFixed(2)}..${Math.max(...a).toFixed(2)}`;
      post({ t: "log", text: `${d.ids[i]} part=${chain[0]} y=${r(ys)} u=${r(us)} v=${r(vs)} tex=${d.textureIndices[i]}` });
    }
  }

  function dumpParams() {  // debug: AVATAR_DEBUG=1 bin/assistant-avatar --show --dump params
    const p = model.internalModel.coreModel.getModel().parameters;
    for (let i = 0; i < p.count; i++)
      post({ t: "log", text: `${p.ids[i]} min=${p.minimumValues[i]} max=${p.maximumValues[i]} default=${p.defaultValues[i]}` });
  }

  // ── emotions: target parameter values blended over the idle motion ──
  // Standard Cubism parameter ids, so most models react; unknown ids are simply ignored.
  // Per-model tuning: [avatar.emotions.<name>] tables in the config replace these presets.
  // Head/body angles are offsets on top of the idle sway, eye openness is a factor (so blinking keeps
  // working), the mouth only ever opens further than lip-sync, everything else is an absolute target.
  const EMOTIONS = {
    neutral: {},
    happy: { ParamEyeLSmile: 1, ParamEyeRSmile: 1, ParamEyeLOpen: 0.1, ParamEyeROpen: 0.1, ParamMouthForm: 1,
             ParamCheek: 0.8, ParamBrowLY: 0.5, ParamBrowRY: 0.5, ParamAngleZ: 10, ParamBodyAngleZ: 4 },
    sad: { ParamMouthForm: -1, ParamBrowLForm: -1, ParamBrowRForm: -1, ParamBrowLY: -0.6, ParamBrowRY: -0.6,
           ParamBrowLAngle: 1, ParamBrowRAngle: 1, ParamEyeLOpen: 0.55, ParamEyeROpen: 0.55, ParamAngleY: -18,
           ParamAngleZ: -6, ParamEyeBallY: -0.8, ParamShoulder: -0.6 },
    worried: { ParamMouthForm: -0.6, ParamBrowLForm: -1, ParamBrowRForm: -1, ParamBrowLY: 0.3, ParamBrowRY: 0.3,
               ParamBrowLAngle: 1, ParamBrowRAngle: 1, ParamEyeBallX: -0.5, ParamAngleZ: -5, ParamAngleY: -6 },
    surprised: { ParamEyeLOpen: 1.4, ParamEyeROpen: 1.4, ParamBrowLY: 1, ParamBrowRY: 1, ParamMouthForm: -0.3,
                 ParamMouthOpenY: 0.5, ParamAngleY: 10, ParamBodyAngleX: -5, ParamShoulder: 0.8 },
    shy: { ParamCheek: 1, ParamMouthForm: 0.6, ParamAngleY: -14, ParamAngleX: -14, ParamAngleZ: -8,
           ParamEyeBallY: -0.7, ParamEyeBallX: -0.6, ParamEyeLSmile: 0.6, ParamEyeRSmile: 0.6, ParamEyeLOpen: 0.6,
           ParamEyeROpen: 0.6 },
    curious: { ParamAngleZ: 18, ParamAngleX: 10, ParamBrowLY: 0.8, ParamBrowRY: 0, ParamMouthForm: 0.3,
               ParamEyeLOpen: 1.15, ParamEyeROpen: 1.15 },
    thinking: { ParamAngleX: 22, ParamAngleY: 16, ParamAngleZ: 8, ParamEyeBallX: 0.9, ParamEyeBallY: 0.9,
                ParamBrowLY: 0.7, ParamBrowRY: -0.3, ParamMouthForm: -0.4, ParamBodyAngleX: 6 },
    attentive: { ParamAngleY: 5, ParamAngleZ: -4, ParamEyeLOpen: 1.1, ParamEyeROpen: 1.1, ParamMouthForm: 0.4,
                 ParamBrowLY: 0.3, ParamBrowRY: 0.3 },
  };
  try { Object.assign(EMOTIONS, JSON.parse(params.get("emotions") || "{}")); } catch (e) { console.error(e); }
  const modeOf = (id) => /^Param(Body)?Angle[XYZ]$/.test(id) ? "add" : /^ParamEye[LR]Open$/.test(id) ? "mul"
                       : id === "ParamMouthOpenY" ? "max" : "set";

  // Every parameter eases on its own (value + weight), so one emotion crossfades straight into the next
  // instead of snapping, and parameters the new face doesn't use fade back to the idle motion.
  let emotion = {}, emoTimer = null, emoLast = 0;
  const blend = {};  // id -> { v: blended target value, w: how strongly it applies, mode }
  function setEmotion(name, holdMs) {
    const preset = EMOTIONS[name];
    if (!preset) return;
    emotion = preset;
    clearTimeout(emoTimer);
    if (holdMs && name !== "neutral") emoTimer = setTimeout(() => (emotion = {}), holdMs);
  }
  function applyEmotion(core) {
    const now = performance.now(), dt = Math.min(0.1, (now - (emoLast || now)) / 1000);
    emoLast = now;
    const k = 1 - Math.exp(-dt / 0.22);  // time-based easing: ~0.6 s to settle, frame-rate independent
    for (const id of Object.keys(emotion)) {
      if (blend[id]) continue;
      const mode = modeOf(id);
      blend[id] = { mode, w: 0, v: mode === "add" || mode === "max" ? 0 : mode === "mul" ? 1 : core.getParameterValueById(id) };
    }
    for (const [id, b] of Object.entries(blend)) {
      const on = id in emotion;
      b.w += ((on ? 1 : 0) - b.w) * k;
      if (on) b.v += (emotion[id] - b.v) * k;
      if (!on && b.w < 0.01) { delete blend[id]; continue; }
      const cur = core.getParameterValueById(id);
      const val = b.mode === "add" ? cur + b.v * b.w
                : b.mode === "mul" ? cur * (1 + (b.v - 1) * b.w)
                : b.mode === "max" ? Math.max(cur, b.v * b.w)
                : cur + (b.v - cur) * b.w;
      core.setParameterValueById(id, val);
    }
  }

  async function load() {
    model = await PIXI.live2d.Live2DModel.from(modelUrl, { autoInteract: false });
    if (params.get("dump") === "params") dumpParams();
    else if (params.get("dump")) dumpDrawables(params.get("dump"));
    const rules = params.get("recolor");
    if (rules) {
      try { recolor(JSON.parse(rules)); } catch (e) { console.error("recolor failed", e); say("recolor failed: " + e); }
    }
    app.stage.addChild(model);
    fit();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));  // vertices exist after a frame
    art = measureArt();
    fit();
    model.internalModel.on("beforeModelUpdate", () => {
      const now = performance.now(), dt = Math.min(0.1, (now - (mouthLast || now)) / 1000);
      mouthLast = now;
      let target = 0;
      if (env.length) {  // interpolate between envelope samples instead of stepping at 30 fps
        const f = (now - envStart) / 1000 * envFps, i = Math.floor(f), t = f - i;
        if (i >= env.length) env = [];
        else target = env[i] + ((i + 1 < env.length ? env[i + 1] : 0) - env[i]) * t;
      }
      mouth += (target - mouth) * (1 - Math.exp(-dt / 0.045));  // time-based, same feel at any frame rate
      const core = model.internalModel.coreModel;
      core.setParameterValueById("ParamMouthOpenY", mouth);
      applyEmotion(core);
    });
    if (pendingArrive) { pendingArrive = false; arrive(); }
  }

  // ── drag / scale / clicks (only reach us inside the model's input region) ──
  // Moving a layer-shell window while measuring the pointer inside it feeds back on itself (the pointer offset
  // stays stale until the compositor moves the window, so it gets applied again and again). Instead avatar.py
  // makes the window cover the whole monitor during the drag: pointer coordinates are then screen coordinates,
  // she follows the cursor inside it, and on release the window shrinks back at her new spot.
  let drag = null;
  window.addEventListener("pointerdown", (e) => {
    if (e.button === 0) drag = { x: e.clientX, y: e.clientY, moved: false, start: null, ending: false };
  });
  window.addEventListener("pointermove", (e) => {
    if (stage && drag) {
      if (!drag.moved) {
        if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return;
        drag.moved = true; drag.fx = frame.x; drag.fy = frame.y;
        post({ t: "stage_drag" });
      }
      frame.x = Math.max(0, Math.min(innerWidth - frame.w, drag.fx + e.clientX - drag.x));
      frame.y = Math.max(0, Math.min(innerHeight - frame.h, drag.fy + e.clientY - drag.y));
      fit(); placeUi();
      return;
    }
    if (!drag || drag.ending) return;
    if (!drag.moved) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return;
      drag.moved = true;
      post({ t: "drag_start" });
      return;
    }
    if (!drag.start) return;  // avatar.py hasn't answered yet
    // Until the compositor has applied the monitor-sized window, events still come relative to the old one.
    // Read each event both ways and keep whichever lands nearer the last known cursor position.
    let X = e.clientX, Y = e.clientY;
    if (!drag.switched) {
      const ox = X + drag.start.x, oy = Y + drag.start.y;
      if (Math.hypot(X - drag.cur.x, Y - drag.cur.y) < Math.hypot(ox - drag.cur.x, oy - drag.cur.y)) drag.switched = true;
      else { X = ox; Y = oy; }
    }
    drag.cur = { x: X, y: Y };
    drag.pos = { x: drag.start.x + X - drag.grab.x, y: drag.start.y + Y - drag.grab.y };
    if (frame) { frame.x = drag.pos.x; frame.y = drag.pos.y; fit(); }
  });
  window.addEventListener("pointerup", () => {
    if (stage && drag) {
      const moved = drag.moved; drag = null;
      if (moved) {
        home = [frame.x / Math.max(1, innerWidth - frame.w), frame.y / Math.max(1, innerHeight - frame.h)];
        post({ t: "stage_drop", fx: home[0], fy: home[1], mr: innerWidth - frame.x - frame.w, mb: innerHeight - frame.y - frame.h });
        margins = [innerWidth - frame.x - frame.w, innerHeight - frame.y - frame.h];
        lastSpot = "home";
        fit();
      }
      return;
    }
    if (!drag) return;
    if (drag.moved) {
      const dx = drag.start ? drag.pos.x - drag.start.x : 0, dy = drag.start ? drag.pos.y - drag.start.y : 0;
      post({ t: "drag_end", dx: Math.round(dx), dy: Math.round(dy) });
      drag.ending = true;  // keep `frame` until the window has shrunk back, so she doesn't jump
      if (!frame) drag = null;
    } else drag = null;
  });
  window.addEventListener("resize", () => {
    if (stage) {
      if (frame) {
        frame.x = Math.max(0, Math.min(frame.x, innerWidth - frame.w));
        frame.y = Math.max(0, Math.min(frame.y, innerHeight - frame.h));
      }
      fit(); placeUi();
      return;
    }
    if (drag && drag.pending && window.innerWidth >= drag.pending.mw) {  // window now covers the monitor
      frame = { x: drag.pos.x, y: drag.pos.y, w: drag.pending.w, h: drag.pending.h };
      drag.pending = null;
    } else if (frame && drag && drag.ending && window.innerWidth < drag.mw) {  // back to her normal size
      frame = null; drag = null;
    }
    fit();
  });
  window.addEventListener("dblclick", () => post({ t: "daemon", cmd: "ptt" }));
  window.addEventListener("contextmenu", (e) => { e.preventDefault(); post({ t: "daemon", cmd: "hide" }); });
  window.addEventListener("wheel", (e) => post({ t: "scale", f: e.deltaY < 0 ? 1.08 : 1 / 1.08 }), { passive: true });

  let swapTok = 0;
  function say(text) {
    if (!text) return;
    const tok = ++swapTok;
    if (bubble.classList.contains("on") && bubble.textContent !== text) {  // next sentence: quick crossfade
      bubble.classList.add("swap");
      setTimeout(() => {
        if (tok !== swapTok) return;
        bubble.textContent = text; placeUi();
        requestAnimationFrame(() => bubble.classList.remove("swap"));
      }, 150);
    } else {
      bubble.classList.remove("swap");
      bubble.textContent = text;
      placeUi();
    }
    bubble.classList.add("on");
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(() => bubble.classList.remove("on"), 4000 + text.length * 40);
  }

  function gesture() {
    const groups = Object.keys(model?.internalModel?.settings?.motions || {}).filter((g) => !/idle/i.test(g));
    if (groups.length && Math.random() < 0.4) model.motion(groups[Math.floor(Math.random() * groups.length)]);
  }

  // ── stage: fresh spot per arrival, screen glow, working capsule, sparkles ──
  // Stage layout = the surface covers her monitor and `frame` is her box inside it (avatar.py picks the layout).
  let stage = false, glowEnabled = true, home = null, box = { w: 360, h: 560 }, lastSpot = null, pendingArrive = false;
  let monitor = { w: 0, h: 0 }, fixed = true, margins = [40, 0];
  let drawn = null;  // where her drawn body is right now (window pixels), set by fit()
  const glow = document.getElementById("glow");
  const statusEl = document.getElementById("status"), badge = document.getElementById("badge"), stext = document.getElementById("stext");
  const trail1 = document.getElementById("t1"), trail2 = document.getElementById("t2");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const anim = (ms, f) => new Promise((r) => {
    const st = performance.now();
    const step = () => { const k = Math.min(1, (performance.now() - st) / ms); f(k); k < 1 ? requestAnimationFrame(step) : r(); };
    step();
  });

  function spots() {
    const W = innerWidth, H = innerHeight, w = box.w, h = box.h, m = 40;
    const s = { bottomRight: [W - w - m, H - h], bottomLeft: [m, H - h], right: [W - w - m, (H - h) * 0.45], top: [(W - w) / 2, 24] };
    if (home) s.home = [home[0] * (W - w), home[1] * (H - h)];
    return s;
  }
  function pickSpot() {  // never the same spot twice in a row; her favourite (dragged) spot comes up more often
    if (fixed) return [innerWidth - margins[0] - box.w, innerHeight - margins[1] - box.h];  // her corner
    const s = spots();
    let keys = Object.keys(s).filter((k) => k !== lastSpot);
    if (s.home && lastSpot !== "home" && Math.random() < 0.4) keys = ["home"];
    const k = keys[Math.floor(Math.random() * keys.length)];
    lastSpot = k;
    return s[k];
  }

  const easeOut = (k) => 1 - Math.pow(1 - k, 3);
  const easeInOut = (k) => k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
  let fadeTok = 0;  // a newer arrive/hide/unhide cancels the running one
  // Live2D models ignore PIXI's partial alpha (only 0 = not drawn works), so fades run on the canvas opacity
  let shown = 1;
  function setShown(a) {
    shown = Math.max(0, Math.min(1, a));
    if (model) model.alpha = shown > 0.002 ? 1 : 0;
    app.view.style.opacity = shown;
  }

  async function arrive() {
    const tok = ++fadeTok;
    if (!stage) {  // compact (game) window: a quick fade, no stage effects
      quiet = true; lift = 0;
      await anim(300, (k) => { if (tok === fadeTok) { setShown(easeOut(k)); fit(); } });
      if (tok === fadeTok) { setShown(1); quiet = false; fit(); }
      return;
    }
    setShown(0); quiet = true;
    // the surface grows to the monitor only after it's mapped: wait for that before choosing a spot
    for (let i = 0; i < 60 && monitor.w && (innerWidth < monitor.w - 2 || innerHeight < monitor.h - 2); i++) await sleep(16);
    const [x, y] = pickSpot();
    frame = { x: Math.max(0, Math.min(innerWidth - box.w, x)), y: Math.max(0, Math.min(innerHeight - box.h, y)), w: box.w, h: box.h };
    if (tok !== fadeTok) return;
    setShown(0); quiet = true; lift = 0; fit(); placeUi();
    if (glowEnabled) { glowArrive = true; glowTo("pink", 0.9, 550, true); await sleep(260); }
    if (tok !== fadeTok) return;
    await anim(650, (k) => { if (tok === fadeTok) setShown(easeInOut(k)); });  // just a fade, she doesn't move
    if (tok !== fadeTok) return;
    setShown(1); lift = 0; quiet = false; fit(); placeUi();
    if (drawn) burst(drawn.x + drawn.w / 2, drawn.y + drawn.h * 0.92, 14, 2.4);
    await sleep(250);
    if (tok === fadeTok && glowArrive) { glowArrive = false; setGlow(curState, 1400); }  // the swell eases into her state
  }

  // ── leaving: everything fades together, then avatar.py unmaps the window (FADE_MS there must match) ──
  const LEAVE_MS = 480;
  async function leave() {
    const tok = ++fadeTok;
    quiet = true; glowArrive = false;
    hideStatus(); bubble.classList.remove("on"); glowTo(null, 0, LEAVE_MS);
    if (model) {
      const a0 = shown, l0 = lift;
      await anim(LEAVE_MS, (k) => {
        if (tok !== fadeTok) return;
        const e = easeInOut(k); setShown(a0 * (1 - e)); lift = l0 + (stage ? 14 * e : 0); fit();
      });
    }
    if (tok !== fadeTok) return;
    // the surface keeps its last frame and shows it again on the next map: leave it empty
    if (model) { setShown(0); lift = 0; }
    app.renderer.render(app.stage);
    app.ticker.stop();
  }
  async function unhide() {  // shown again while still fading out: turn around instead of popping
    const tok = ++fadeTok;
    app.ticker.start();
    setGlow(curState, 700);
    if (!model) return;
    const a0 = shown, l0 = lift;
    await anim(380, (k) => {
      if (tok !== fadeTok) return;
      const e = easeOut(k); setShown(a0 + (1 - a0) * e); lift = l0 * (1 - e); fit();
    });
    if (tok === fadeTok) { setShown(1); lift = 0; quiet = false; fit(); placeUi(); }
  }

  // ── screen glow by state: one layer per colour, crossfaded ──
  let curState = "idle", glowArrive = false, errorTimer = null;
  const layers = Object.fromEntries([...glow.children].map((g) => [g.classList[1], g]));
  const GLOW = { listening: ["pink", 0.5], thinking: ["blue", 0.75], thinking_hard: ["blue", 0.75], confirming: ["amber", 0.65] };
  function glowTo(name, level, ms, still = false) {
    glow.classList.toggle("still", still);
    for (const [n, g] of Object.entries(layers)) {
      g.style.transitionDuration = ms + "ms";
      g.style.opacity = n === name ? level : 0;
    }
  }
  function setGlow(st, ms = 900) {
    if (st !== "error") curState = st;
    if (!stage || !glowEnabled) { glowTo(null, 0, 0); return; }
    if (glowArrive && st !== "error") return;  // the arrival swell eases into the current state by itself
    clearTimeout(errorTimer);
    layers.red.classList.remove("flick");
    if (st === "error") {
      void layers.red.offsetWidth; layers.red.classList.add("flick");
      glowTo("red", 1, 200);
      errorTimer = setTimeout(() => setGlow(curState, 1200), 1600);
      return;
    }
    const [name, level] = GLOW[st] || [null, 0];
    glowTo(name, level, ms);
  }

  // ── where the bubble and capsule go: beside her head, on the side facing the middle of the screen ──
  let statusRight = false;
  function placeUi() {
    if (!stage || !drawn) {
      bubble.style.left = bubble.style.top = ""; bubble.classList.remove("side", "left");
      return;
    }
    const W = innerWidth, cx = drawn.x + drawn.w / 2, headY = drawn.y + drawn.h * 0.17;
    statusRight = cx < W / 2;
    statusEl.classList.toggle("right", statusRight);
    statusEl.style.left = (statusRight ? drawn.x + drawn.w + 20 : drawn.x - 20) + "px";
    statusEl.style.top = headY + "px";
    const tx = statusRight ? drawn.x + drawn.w : drawn.x;
    trail1.style.cssText = `left:${statusRight ? tx + 4 : tx - 14}px;top:${headY + 30}px;width:10px;height:10px`;
    trail2.style.cssText = `left:${statusRight ? tx - 4 : tx - 2}px;top:${headY + 46}px;width:6px;height:6px`;
    if (drawn.y > 120) {  // room above her head
      bubble.classList.remove("side", "left");
      bubble.style.left = Math.max(170, Math.min(W - 170, cx)) + "px";
      bubble.style.top = (drawn.y - 14) + "px";
    } else {  // at the top of the screen: beside her, opposite the capsule
      bubble.classList.add("side"); bubble.classList.toggle("left", statusRight);
      bubble.style.left = (statusRight ? drawn.x - 16 : drawn.x + drawn.w + 16) + "px";
      bubble.style.top = headY + "px";
    }
  }

  // ── working capsule ──
  document.body.insertAdjacentHTML("beforeend", `<svg width="0" height="0" style="position:absolute"><defs>
    <linearGradient id="mioGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f5c2e7"/><stop offset="1" stop-color="#cba6f7"/></linearGradient></defs></svg>`);
  const svg = (body) => `<svg viewBox="0 0 24 24" fill="none" stroke="url(#mioGrad)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const ICON = {
    web: svg('<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18"/>'),
    music: svg('<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'),
    files: svg('<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/><path d="M3 10h18"/>'),
    timer: svg('<circle cx="12" cy="13.5" r="7.5"/><path d="M12 13.5V9.5"/><path d="M9.5 2.5h5"/><path d="M18.5 6.5l1.5-1.5"/>'),
    system: svg('<rect x="3" y="4" width="18" height="12" rx="2.5"/><path d="M8 20h8"/><path d="M12 16v4"/><path d="M7 12l2.5-3 2.5 2 3-4 2 2"/>'),
    shell: svg('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 9l3 2.5L7 14"/><path d="M12.5 14H17"/>'),
    screen: svg('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>'),
    app: svg('<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><path d="M17 13.5v7M13.5 17h7"/>'),
    game: svg('<path d="M7 8h10a4 4 0 0 1 4 4.5l-.6 3.4a2.3 2.3 0 0 1-4 1L15 15H9l-1.4 1.9a2.3 2.3 0 0 1-4-1L3 12.5A4 4 0 0 1 7 8z"/><path d="M8 11v3M6.5 12.5h3"/><circle cx="16" cy="11.5" r=".6"/><circle cx="17.5" cy="13.5" r=".6"/>'),
    browser: svg('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/><circle cx="6.5" cy="6.5" r=".5"/><circle cx="9" cy="6.5" r=".5"/>'),
    video: svg('<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>'),
    package: svg('<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5"/><path d="M12 12v9"/>'),
    news: svg('<rect x="3" y="4" width="15" height="16" rx="2"/><path d="M18 8h3v10a2 2 0 0 1-2 2"/><path d="M7 8h7M7 12h7M7 16h4"/>'),
    memory: svg('<path d="M12 20s-7-4.3-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.7-7 10-7 10z"/>'),
    clip: svg('<rect x="3" y="8" width="18" height="12" rx="2"/><path d="M3 8l3-4h3l-3 4M9 8l3-4h3l-3 4M15 8l3-4h3"/>'),
    phone: svg('<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>'),
    weather: svg('<path d="M7 18a4 4 0 0 1-.5-8 5.5 5.5 0 0 1 10.6 1.5A3.3 3.3 0 0 1 17 18z"/>'),
    keys: svg('<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M7.5 14h9"/>'),
    volume: svg('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>'),
    chat: svg('<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 3.5V17A2.5 2.5 0 0 1 4 14.5z"/>'),
    shield: svg('<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.2-4.4"/>'),
    sparkle: svg('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>'),
  };
  const LOOKS = [  // tool name → icon + title (first match wins)
    [/^wikipedia$/, "web", "Checking Wikipedia"], [/^arch_wiki$/, "web", "Checking the ArchWiki"],
    [/^(read_link|read_clipboard_link|browser_page)$/, "web", "Reading the page"],
    [/^(web_research|search_site|open_best_match|web)$/, "web", "Searching the web"],
    [/^youtube/, "video", "Finding the video"], [/^browser_video$/, "video", "Controlling the video"],
    [/^browser_history$/, "browser", "Searching your history"], [/^browser/, "browser", "In your browser"],
    [/^(riff|music)/, "music", "Getting your music"], [/^media$/, "music", "Controlling playback"],
    [/^tidy_downloads$/, "files", "Tidying your Downloads"], [/^trash$/, "files", "Moving to the trash"],
    [/^move_file$/, "files", "Moving files"], [/^(find_files|list_files|files)$/, "files", "Looking through files"],
    [/^world_clock$/, "timer", "Checking the clock"], [/^timers?$/, "timer", "Setting a timer"],
    [/^install_updates$/, "package", "Installing updates"], [/^install_packages$/, "package", "Installing packages"],
    [/^(system_updates|check_install|package_info|packages)$/, "package", "Checking packages"],
    [/^virus_scan$/, "shield", "Checking for viruses"], [/^quarantine$/, "shield", "Handling quarantine"],
    [/^arch_news$/, "news", "Reading the news"], [/^game_news$/, "news", "Reading the patch notes"],
    [/^game_price$/, "game", "Checking Steam prices"], [/^(launch_game|games|steam_library)$/, "game", "Starting your game"],
    [/^(system_health|system_info|system)$/, "system", "Checking your PC"],
    [/^(run_shell|shell|shell_panel)$/, "shell", "Running a command"],
    [/^screen|look_at_screen|read_screen/, "screen", "Looking at your screen"],
    [/^(launch_app|open|focus_window|list_windows|niri_action|apps|desktop)$/, "app", "Opening things"],
    [/^clip$/, "clip", "Saving a clip"], [/^phone$/, "phone", "Talking to your phone"], [/^weather$/, "weather", "Checking the weather"],
    [/^(press_keys|type_text|keys)$/, "keys", "Typing for you"], [/^volume$/, "volume", "Changing the volume"],
    [/^discord$/, "chat", "On Discord"], [/^(remember|memory)$/, "memory", "Remembering that"],
    [/^(knowledge|search_my_setup|setup)$/, "memory", "Checking my notes"], [/^routines?$/, "sparkle", "Running your routine"],
  ];
  const toolLook = (tool) => { for (const [re, icon, title] of LOOKS) if (re.test(tool || "")) return [icon, title]; return ["sparkle", "Working on it"]; };

  let statusOn = false, curIcon = null, curLine = null, lastTool = null, lastDetail = null;
  function showStatus(tool, text, detail) {
    if (!stage) return;
    const [icon, title] = toolLook(tool);
    detail = (detail || (text || "").replace(/…$/, "") || "").trim();
    if (detail.toLowerCase().startsWith(title.toLowerCase())) detail = "";  // don't repeat the title
    if (statusOn && tool === lastTool && detail === lastDetail) return;
    lastTool = tool; lastDetail = detail;
    for (const old of [curIcon, curLine]) if (old) { old.classList.add("out"); setTimeout(() => old.remove(), 400); }
    badge.insertAdjacentHTML("beforeend", ICON[icon]);
    curIcon = badge.lastElementChild; curIcon.classList.add("out");
    const line = document.createElement("div");
    line.className = "sline out";
    line.innerHTML = '<div class="stitle"></div><div class="sdetail"><span class="d"></span><span class="dots"></span></div>';
    line.querySelector(".stitle").textContent = title;
    line.querySelector(".d").textContent = detail;
    stext.appendChild(line); curLine = line;
    const ic = curIcon;
    requestAnimationFrame(() => requestAnimationFrame(() => { ic.classList.remove("out"); line.classList.remove("out"); }));
    if (!statusOn) {
      statusOn = true; placeUi();
      statusEl.classList.add("on"); trail1.classList.add("on"); trail2.classList.add("on");
    }
    badge.insertAdjacentHTML("beforeend", '<span class="ring"></span>');
    const ring = badge.lastElementChild; setTimeout(() => ring.remove(), 800);
    setTimeout(() => { const b = badge.getBoundingClientRect(); burst(b.left + b.width / 2, b.top + b.height / 2, 18, 3.2); }, 60);
  }
  function hideStatus() {
    if (!statusOn) return;
    statusOn = false; lastTool = lastDetail = null;
    statusEl.classList.remove("on"); trail1.classList.remove("on"); trail2.classList.remove("on");
  }

  // ── sparkles (own canvas above her; only animates while there is something to draw) ──
  const fxc = document.createElement("canvas"); fxc.id = "fx"; document.body.appendChild(fxc);
  const g = fxc.getContext("2d"), parts = [];
  const COL = ["#f5c2e7", "#cba6f7", "#b4befe", "#f9e2af", "#ffffff"];
  function sizeFx() { const d = devicePixelRatio || 1; fxc.width = innerWidth * d; fxc.height = innerHeight * d; g.setTransform(d, 0, 0, d, 0, 0); }
  sizeFx(); window.addEventListener("resize", sizeFx);
  const star = (x, y, r) => {
    g.beginPath();
    for (let i = 0; i < 8; i++) { const a = i * Math.PI / 4, rr = i % 2 ? r * .35 : r; g.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr); }
    g.closePath(); g.fill();
  };
  let fxRunning = false;
  function fxTick() {
    g.clearRect(0, 0, innerWidth, innerHeight);
    if (statusOn && Math.random() < 0.12) {  // the odd sparkle drifting off the capsule
      const c = statusEl.getBoundingClientRect();
      parts.push({ x: c.left + Math.random() * c.width, y: Math.random() < .5 ? c.top : c.bottom, vx: (Math.random() - .5) * .6,
                   vy: -.4 - Math.random() * .6, life: 0, max: 70, r: 2 + Math.random() * 2.5, c: COL[Math.floor(Math.random() * 4)], star: Math.random() < .6 });
    }
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      if (++p.life > p.max) { parts.splice(i, 1); continue; }
      p.x += p.vx; p.y += p.vy; p.vx *= .95; p.vy = p.vy * .95 - .02;
      const k = p.life / p.max;
      g.globalAlpha = Math.sin(Math.PI * k); g.fillStyle = g.shadowColor = p.c; g.shadowBlur = 12;
      if (p.star) star(p.x, p.y, p.r * (1 - k * .4)); else { g.beginPath(); g.arc(p.x, p.y, p.r * (1 - k), 0, 7); g.fill(); }
    }
    g.globalAlpha = 1;
    if (parts.length || statusOn) requestAnimationFrame(fxTick); else fxRunning = false;
  }
  function burst(x, y, n, speed) {
    if (!stage) return;
    for (let i = 0; i < n; i++) {
      const a = Math.random() * 7, v = speed * (.4 + Math.random());
      parts.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 0, max: 40 + Math.random() * 30, r: 2.5 + Math.random() * 4,
                   c: COL[Math.floor(Math.random() * COL.length)], star: Math.random() < .5 });
    }
    if (!fxRunning) { fxRunning = true; requestAnimationFrame(fxTick); }
  }

  window.avatar = {
    dragFrame(x, y, w, h, mw) {  // avatar.py made the window cover the monitor; (x, y) = her old window
      if (!drag) return;
      drag.start = { x, y };
      drag.grab = drag.cur = { x: drag.x + x, y: drag.y + y };  // in screen coordinates
      drag.pos = { x, y };
      drag.pending = { w, h, mw }; drag.mw = mw;
      if (window.innerWidth >= mw) window.dispatchEvent(new Event("resize"));
    },
    setActive(on) { on ? app.ticker.start() : app.ticker.stop(); if (on) fit(); },
    // avatar.py, before it unmaps the window: fade everything out and end on an empty frame (the surface shows
    // its last frame again on the next map; otherwise she'd flash at her old spot before arrive() fades her in).
    hide() { leave(); },
    unhide() { unhide(); },
    layout(o) {  // avatar.py, before she appears: stage (full monitor) or compact (corner window)
      stage = !!o.stage; glowEnabled = o.glow !== false; home = o.home || null;
      fixed = o.fixed !== false; margins = o.margins || [40, 0];
      box = { w: o.w, h: o.h }; monitor = { w: o.mw || 0, h: o.mh || 0 };
      document.body.classList.toggle("stage", stage);
      frame = stage ? { x: 0, y: 0, w: o.w, h: o.h } : null;
      app.ticker.maxFPS = stage ? 60 : 30;  // the compact window runs next to a game: keep it light
      ++fadeTok; glowArrive = false;
      if (model) { setShown(0); lift = 0; quiet = true; }  // stays invisible until arrive() fades her in
      hideStatus(); glowTo(null, 0, 0); bubble.classList.remove("on", "swap");
      placeUi();
    },
    arrive() { if (model) arrive(); else pendingArrive = true; },
    resize(w, h) {  // stage: the mouse wheel scaled her; keep her feet where they were
      if (!stage || !frame) return;
      frame.x += (frame.w - w) / 2; frame.y += frame.h - h; frame.w = w; frame.h = h;
      frame.x = Math.max(0, Math.min(innerWidth - w, frame.x)); frame.y = Math.max(0, Math.min(innerHeight - h, frame.y));
      box = { w, h }; fit(); placeUi();
    },
    command(msg) {
      switch (msg.cmd) {
        case "state":
          stateDot.className = msg.state === "idle" || msg.state === "speaking" ? "" : msg.state;
          setGlow(msg.state);
          if (msg.state === "idle" || msg.state === "listening") hideStatus();
          break;
        case "speak":
          env = msg.env || []; envFps = msg.fps || 30; envStart = performance.now();
          if (!bubble.classList.contains("on")) gesture();
          hideStatus();
          say(msg.text);
          break;
        case "emotion":  // speech emotions linger a little after she finishes; state faces last until changed
          setEmotion(msg.name, ["happy", "sad", "worried", "surprised", "shy"].includes(msg.name) ? 6000 : 0);
          if (msg.name === "happy" && Math.random() < 0.5) gesture();
          break;
        case "status":  // what she's doing right now; stays until she speaks
          if (stage) { showStatus(msg.tool, msg.text, msg.detail); break; }
          if (!msg.text) break;
          bubble.textContent = msg.text;
          bubble.classList.add("on");
          clearTimeout(bubbleTimer);
          bubbleTimer = setTimeout(() => bubble.classList.remove("on"), 60000);
          break;
        case "speak_end":
          env = [];
          break;
        case "error":
          setEmotion("worried", 6000);
          hideStatus();
          setGlow("error");
          say("⚠ " + msg.text);
          break;
      }
    },
  };

  load().catch((e) => { bubble.textContent = "Model failed to load: " + e; bubble.classList.add("on"); });
})();
