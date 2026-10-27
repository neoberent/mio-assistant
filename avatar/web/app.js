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
  app.ticker.maxFPS = 30; // plenty for an idle character; halves GPU use vs 60

  let model = null;
  let mouth = 0, env = [], envFps = 30, envStart = 0;
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
  function fit() {
    if (!model) return;
    const W = window.innerWidth, H = window.innerHeight;
    const { x: fx, y: fy, w, h } = frame || { x: 0, y: 0, w: W, h: H };
    const a = art || { x: 0, y: 0, w: model.internalModel.width, h: model.internalModel.height };
    const s = Math.min(w / a.w, h / a.h) * 0.98 * zoom;
    model.scale.set(s);
    model.anchor.set(0, 0);
    model.position.set(fx + w / 2 - (a.x + a.w / 2) * s, fy + h * focus - (a.y + a.h * focus) * s);
    if (frame) return;
    const x = model.position.x + a.x * s, y = model.position.y + a.y * s;
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
      let target = 0;
      if (env.length) {
        const i = Math.floor((performance.now() - envStart) / 1000 * envFps);
        target = i < env.length ? env[i] : 0;
        if (i >= env.length) env = [];
      }
      mouth += (target - mouth) * 0.6;
      const core = model.internalModel.coreModel;
      core.setParameterValueById("ParamMouthOpenY", mouth);
      applyEmotion(core);
    });
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
    if (!drag) return;
    if (drag.moved) {
      const dx = drag.start ? drag.pos.x - drag.start.x : 0, dy = drag.start ? drag.pos.y - drag.start.y : 0;
      post({ t: "drag_end", dx: Math.round(dx), dy: Math.round(dy) });
      drag.ending = true;  // keep `frame` until the window has shrunk back, so she doesn't jump
      if (!frame) drag = null;
    } else drag = null;
  });
  window.addEventListener("resize", () => {
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

  function say(text) {
    if (!text) return;
    bubble.textContent = text;
    bubble.classList.add("on");
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(() => bubble.classList.remove("on"), 4000 + text.length * 40);
  }

  function gesture() {
    const groups = Object.keys(model?.internalModel?.settings?.motions || {}).filter((g) => !/idle/i.test(g));
    if (groups.length && Math.random() < 0.4) model.motion(groups[Math.floor(Math.random() * groups.length)]);
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
    command(msg) {
      switch (msg.cmd) {
        case "state":
          stateDot.className = msg.state === "idle" || msg.state === "speaking" ? "" : msg.state;
          break;
        case "speak":
          env = msg.env || []; envFps = msg.fps || 30; envStart = performance.now();
          if (!bubble.classList.contains("on")) gesture();
          say(msg.text);
          break;
        case "emotion":  // speech emotions linger a little after she finishes; state faces last until changed
          setEmotion(msg.name, ["happy", "sad", "worried", "surprised", "shy"].includes(msg.name) ? 6000 : 0);
          if (msg.name === "happy" && Math.random() < 0.5) gesture();
          break;
        case "status":  // what she's doing right now; stays until she speaks
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
          say("⚠ " + msg.text);
          break;
      }
    },
  };

  load().catch((e) => { bubble.textContent = "Model failed to load: " + e; bubble.classList.add("on"); });
})();
