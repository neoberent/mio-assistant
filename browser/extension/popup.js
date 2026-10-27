const $ = (id) => document.getElementById(id);
const DEFAULTS = { readPages: true, allowActions: true, blocked: [], log: [] };

async function load() {
  const s = { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
  $("readPages").checked = s.readPages;
  $("allowActions").checked = s.allowActions;
  renderBlocked(s.blocked);
  renderLog(s.log);
}

function renderBlocked(list) {
  const box = $("blocked");
  box.replaceChildren(...list.map((b) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = b;
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = `Let Mio use sites matching “${b}” again`;
    x.onclick = async () => {
      const { blocked = [] } = await chrome.storage.local.get("blocked");
      await chrome.storage.local.set({ blocked: blocked.filter((v) => v !== b) });
      load();
    };
    chip.append(x);
    return chip;
  }));
}

function renderLog(log) {
  if (!log.length) return;
  $("log").replaceChildren(...log.slice(0, 12).map((e) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = e.text;
    const time = document.createElement("time");
    time.textContent = new Date(e.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    li.append(span, time);
    return li;
  }));
}

async function status() {
  const pill = $("status");
  const s = await chrome.runtime.sendMessage({ type: "status" }).catch(() => ({ bridge: false }));
  const help = $("help");
  help.hidden = true;
  if (!s.bridge) {
    pill.textContent = "bridge not installed";
    pill.className = "pill bad";
    help.hidden = false;
    help.innerHTML = "Run <code>scripts/install_browser.sh</code> in Mio's folder, then restart Brave.";
  } else if (!s.mio) {
    pill.textContent = "Mio is offline";
    pill.className = "pill bad";
    help.hidden = false;
    help.innerHTML = "Start her with <code>systemctl --user start assistantd</code>.";
  } else {
    pill.textContent = s.state && s.state !== "idle" ? `connected · ${s.state}` : "connected";
    pill.className = s.state && s.state !== "idle" ? "pill busy" : "pill ok";
  }
  for (const id of ["summarize", "video"]) $(id).disabled = !(s.bridge && s.mio);
}

async function ask(text) {
  const r = await chrome.runtime.sendMessage({ type: "ask", text });
  $("sent").hidden = !r?.ok;
  setTimeout(() => { $("sent").hidden = true; }, 4000);
}

$("summarize").onclick = () => ask("Summarize the web page I'm looking at in my browser.");
$("video").onclick = () => ask("What is the YouTube video I'm watching about?");
$("ask").onsubmit = (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (!q) return;
  ask(`${q} (about the web page I'm looking at in my browser)`);
  $("question").value = "";
};
for (const id of ["readPages", "allowActions"]) {
  $(id).onchange = () => chrome.storage.local.set({ [id]: $(id).checked });
}
$("blockHere").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let host = "";
  try { host = new URL(tab.url).host.replace(/^www\./, ""); } catch { return; }
  if (!host) return;
  const { blocked = [] } = await chrome.storage.local.get("blocked");
  if (!blocked.includes(host)) await chrome.storage.local.set({ blocked: [...blocked, host] });
  load();
};

chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
  $("video").hidden = !/youtube\.com\/(watch|shorts)|youtu\.be\//.test(tab?.url || "");
});
chrome.storage.onChanged.addListener(load);
load();
status();
setInterval(status, 3000);
