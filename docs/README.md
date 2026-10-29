# Mio — desktop voice assistant for CachyOS + niri + Noctalia

Press **Mod+A**, talk, and a Live2D character pops up in the corner, answers out loud and
actually does things on your desktop. When she's done she disappears again.

## Architecture

```
 Mod+A (niri) / tray click / double-click on her
        │  ~/.local/bin/assistant ptt  →  $XDG_RUNTIME_DIR/assistant.sock
 ┌──────▼──────────────── assistantd (Python 3.12 venv, systemd --user) ─────────────────┐
 │ mic → Silero VAD (stops when you pause) → faster-whisper large-v3-turbo (CUDA)       │
 │ ROUTER  1. fast-path regexes (open X, volume, workspace, media, time…)   0 tokens     │
 │         2. Ollama qwen3.5:9b with tools; ask_claude when out of depth      local     │
 │         3. `claude -p` (your login, haiku, 4 turns max, daily cap)         ~1k tokens│
 │ ToolBox: apps, niri, Noctalia, volume, media, files, clipboard, typing, shell        │
 │          → permission gate (allow-list / voice "yes" or notification button) → audit │
 │ Kokoro-82M TTS (CPU) per sentence → PipeWire;  RMS envelope → avatar lip-sync        │
 └──────────────┬─────────────────────────────────────────────────────────────────────────┘
                │ $XDG_RUNTIME_DIR/assistant-avatar.sock
 ┌──────────────▼──── assistant-avatar (system Python, GTK4 + gtk4-layer-shell + WebKitGTK) ┐
 │ layer-shell OVERLAY surface: above every window on every workspace, transparent,        │
 │ input region = model bounds (click-through elsewhere), unmapped when hidden              │
 │ pixi.js 6 + pixi-live2d-display (Cubism 4/5 models), 30 fps cap, ticker stops when hidden│
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

Claude gets its tools through `assistant/mcp_server.py`, a tiny stdio MCP server that forwards
every call back to assistantd, so the same permission gate and audit log apply to all three brains.

**Why not Open-LLM-VTuber / NyarchAssistant / AIRI?** Their pet modes are Electron (OLV: Win/Mac
only) or a normal GTK window (Nyarch). On Wayland an ordinary window can't be "always on top on
every workspace" or click-through; only a layer-shell surface can. They also have no
local-first routing, which is where most of the token savings come from.

## Install

1. System packages (as you, needs sudo):
   ```
   sudo snapper -c root create --description "pre-assistant"
   sudo pacman -S --needed ollama-cuda uv gtk4-layer-shell webkitgtk-6.0 wtype wl-clipboard playerctl grim
   sudo systemctl enable --now ollama
   ```
2. Everything else (no sudo):
   ```
   ./scripts/install.sh
   ```
   This creates `.venv` (Python 3.12 via uv), downloads the avatar libraries, the sample model,
   Kokoro/Piper voices and the Whisper model, pulls the Ollama model, copies the config to
   `~/.config/assistant/`, links `~/.local/bin/assistant`, enables both user services and adds
   `include "assistant.kdl"` to your niri config.
3. Make sure `claude` is logged in (`claude` → `/login`) — the Claude brain uses that login.

## Daily use

| Action | How |
|---|---|
| Talk | **Mod+A**, speak, pause. Press again to stop early, or to interrupt her while she talks |
| Pin / unpin character | **Mod+Shift+A**, or middle-click the tray icon |
| Stop talking/listening | **Mod+Ctrl+A**, or say "stop" |
| Hide her | right-click her, say "bye", or wait — she hides `hide_after_s` after speaking |
| Move / resize | drag her; scroll wheel over her to resize (remembered) |
| Talk without hotkey | double-click her, or left-click the tray icon |
| Type instead of talk | `assistant ask "what's using my GPU?"` |
| Force Claude | start with "ask Claude …" |
| Status / usage today | `assistant status` |
| Start / stop everything | `systemctl --user start\|stop assistantd assistant-avatar` |
| Disable at login | `systemctl --user disable assistantd assistant-avatar` |
| Logs | `journalctl --user -fu assistantd` (and `-u assistant-avatar`) |
| Audit log of every action | `~/.local/state/assistant/audit.log` |

Things she can do: open apps, focus/close/move windows, switch workspaces, overview, fullscreen,
screenshots, volume/mute, media control, Noctalia panels/night light/DND/wallpaper/theme, notifications,
open URLs and files, type text, clipboard, list/read/write/move/trash files in your normal folders,
system info, run shell commands, remember facts about you.

Free lookups (no API keys, handled by the local model, zero Claude tokens):

| Ask | Tool / source |
|---|---|
| "any Arch news?", "what's new in CachyOS?", "new AUR packages?" | `arch_news`: archlinux.org, cachyos.org, aur.archlinux.org feeds |
| "do I have updates?" / "check my PC for updates" | fast path → `system_updates` (`checkupdates` + `paru -Qua`); "yes" then opens `paru -Syu` in a terminal |
| "is the AUR package X trustworthy?" | `package_info`: pacman + AUR RPC (votes, maintainer, last update, out-of-date) |
| "how do I fix NVIDIA suspend?" | `arch_wiki`: ArchWiki intro, sections on demand |
| "who/what is …?" | `wikipedia`: REST summary / sections |
| "weather tomorrow?" | `weather`: wttr.in (set `[lookup] weather_location`, empty = by IP) |
| "is my PC healthy?" | `system_health`: failed units, boot errors, temps, disks, memory |
| "is this safe to install?" (with `paru -S …` typed/pasted in kitty) | `check_install`: reads the focused terminal, checks repo vs AUR, votes/age/maintainer, scans PKGBUILD + .install for red flags |
| "what's new in …?", any current question | `web_research`: DuckDuckGo → top 4 sites → main text (trafilatura) → ~900 words of extracts; local model answers, Claude gets only the extracts for harder questions (~0.2 ¢) |
| "read the link I copied" | `read_clipboard_link` (only when you mention the clipboard) / `read_link` |
| "what's on my screen?", "explain this page" | `read_screen`: niri screenshot of the focused window → tesseract OCR (kitty: exact text); clipboard is restored afterwards |
| "look at this chart/picture" | `look_at_screen`: Claude only, ~1280 px image (~0.1–0.3 ¢) |
| "what does this error mean?" | `terminal_text`: reads the focused terminal (local model, or Claude if it's complicated) |

### Letting her read kitty (optional, one-time)

Without this she falls back to the clipboard (which usually still holds what you pasted).
Add to `~/.config/kitty/kitty.conf`, pick any password, then open a new kitty window:

```
allow_remote_control password
remote_control_password "PICK-A-PASSWORD" get-text ls
listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}
```

The password only permits `get-text` and `ls`, so nothing that connects to the socket can type
into or control your terminal. The assistant reads the password from `kitty.conf` itself.
Terminal text only leaves your PC if the request escalates to Claude.

## Startup

Both services are `WantedBy=graphical-session.target`, which niri starts at login, so she's ready
a few seconds after you log in (`assistant status` → `idle`). Ollama runs as a system service and
only loads the model into VRAM when you press Mod+A (it warms up while you're still talking). The
tray icon re-registers by itself if Noctalia restarts. Nothing speaks or shows up at login.

## Configuration — `~/.config/assistant/config.toml`

Restart after editing: `systemctl --user restart assistantd assistant-avatar`.

**Change voice**
- Kokoro voices: `kokoro_voice = "af_heart"` (also `af_bella`, `af_nicole`, `af_sky`, `af_sarah`,
  `bf_emma` British, `jf_alpha` Japanese …). Speed: `kokoro_speed`.
- Piper instead (lighter, more robotic): `engine = "piper"`, `piper_voice = "en_US-amy-medium"`.
  New Piper voices: `PIPER_VOICE=en_US-lessac-medium scripts/fetch_assets.sh` then set `piper_voice`.
  Browse voices at https://rhasspy.github.io/piper-samples/

**Change the character**
1. Put a Cubism 3/4/5 model folder (the one containing `*.model3.json`) into `avatar/web/models/`.
   Free models: Live2D's sample collection (https://www.live2d.com/en/learn/sample/), Booth.pm
   (filter "free", check the license), or your own from Cubism Editor.
2. Set `[avatar] model = "FolderName/File.model3.json"`. Restart `assistant-avatar`.
   Lip-sync drives `ParamMouthOpenY`, which almost every model has. If she never blinks, add
   `ParamEyeLOpen`/`ParamEyeROpen` to the model3.json `EyeBlink` group (VTube Studio models often leave it empty).
3. Tune her faces for the new model: `[avatar.emotions.<name>]` tables replace the built-in presets
   (neutral, happy, sad, worried, surprised, shy, curious, thinking, attentive). List the model's parameters
   and ranges with `AVATAR_DEBUG=1 bin/assistant-avatar --show --dump params`; preview a face with
   `echo '{"cmd":"emotion","name":"shy"}' | socat - UNIX:$XDG_RUNTIME_DIR/assistant-avatar.sock`.
   Angles add onto the idle sway, `ParamEye*Open` is a factor (blinking keeps working), the rest are targets.
   Faces crossfade into each other (~0.6 s).
   Wide props (umbrellas, wings) make a model tiny: `zoom = 1.5` + `focus = 0.3` (0 = keep the top in view).
- Mio currently wears **yuri** (creape, free: https://booth.pm/ja/items/4798949 — download needs a Booth login,
  so `fetch_assets.sh` can't get her; run `scripts/install_yuri.sh ~/Downloads/live2d_yuri.zip`). Her faces live in the
  personal config (`~/.config/assistant/config.toml`); set `recolor = []` there, otherwise the repo's Hiyori recolor applies.
- Recolor without new art: `[[avatar.recolor]]` rules in the config tint parts of the model
  (the Hiyori default gets pink hair and a mauve cardigan this way). Find part/mesh ids with
  `systemctl --user stop assistant-avatar; AVATAR_DEBUG=1 bin/assistant-avatar --show --dump 'PartBody'`,
  try rules live with `--recolor '[{"parts":"Hair","color":"#f5c2e7"}]'`, then restart the service.
  Other bundled samples to try: `--model Haru/Haru.model3.json`, `Mao/Mao.model3.json`, `Rice/Rice.model3.json`.
- Monitor: `output = "DP-1"` (right) or `"DP-2"` (left). Size: `width`/`height`. Delete
  `~/.local/state/assistant/avatar.json` to reset the dragged position and size.

**Change the brain**
- Local model: `ollama_model` (default `qwen3.5:9b`: thinks, sees, ~5.9 GB VRAM at her context size).
  `qwen3:8b` still works (then set `vision_model = "qwen3-vl:4b-instruct"` for her eyes).
  `local_enabled = false` sends everything that isn't a fast-path command to Claude.
- Claude model: `claude_model = "haiku" | "sonnet" | "opus"`. Haiku is plenty for desktop tasks.
- `claude_backend = "api"` uses `ANTHROPIC_API_KEY` from `~/.config/assistant/env`
  (`uv sync --extra api` first). `"off"` makes the assistant fully local.
- Persona: `[persona] name` and `style` (keep it short — it's sent with every Claude request).

**Speech recognition**: requests are transcribed with beam search (`beam_size`, default 5, ~0.1 s on the
GPU) and a vocabulary prompt (`vocabulary`, default in `assistant/stt.py`: Mio, Roblox, paru, niri, Hatsune
Miku…), then common mishearings are fixed ("Miu" → Mio, "Patsune" → Hatsune). Wake checks stay greedy and
unprompted so the prompt can't make her hear her name in noise. `model = "small.en"` (+ `device = "cpu"`, `compute_type = "int8"`) frees the GPU.
`end_silence_ms` controls how long a pause ends your sentence.

## How she learns (`assistant/learning.py`, `assistant/dream.py`)

All of this runs on the CPU (or, for dreaming, only while you're away), so games aren't affected.

- **Names she knows.** App names, games, YouTube channels from Brave's history ("Jordan & Semih Live" also
  teaches "Jordan & Semih") and names her tools find (the channel of a video she opened). The most used ones
  are added to Whisper's vocabulary prompt.
- **Sound-alike fixes.** After Whisper, words it doesn't know as whole words are matched against those names
  by spelling and sound: "Jordan and Sammy" → "Jordan & Semih", "Nearie" → "niri". Common English words are
  never rewritten; an unsure match ("John and Sammy") only becomes a hint for the model. "No, I said
  Rohaze" teaches a permanent fix (`rohat → Rohaze`).
- **Typos and shorthand.** Every request, typed or spoken, is spell-checked first (~5 ms): "hows the wether",
  "cancle my timmer", "ring my fone", "skip the add", "turn of wifi", "rn", "pls". Her own vocabulary comes
  first (the words of her tools' descriptions plus the words you say to her), then the dictionary (Whisper's
  24k words), and only if the fix sounds the same or is two swapped letters and clearly wins. Capitalized
  words mid-sentence and words after "message / with / friend…" are treated as names and left alone ("alex"
  stays alex). The log shows `spelling: wether→weather`.
- **Names from your world.** Besides apps, games and YouTube channels: the sites you visit most (host names
  only, never searches or pages) and your Steam games. Her `open`/`search_site` also know your favourites
  (TikTok, Modrinth, Pinterest, YouTube Music, Proton Mail, Roblox…).
- **Her vocabulary.** The words you say to her are counted in `lexicon.json` (`words`). Spoken words are
  spelled by Whisper, so new ones join her vocabulary after you've said them twice; typed words only count if
  they're real words, so a typo never becomes one. `assistant learned` lists the most used.
- **Learned requests (skills).** Every request that worked, locally or through Claude, is kept with the
  tool calls that did it. Similar requests (matched by a 23M-parameter embedding model, `all-minilm`, on the
  CPU) get those as examples and tools. A near-identical one is replayed directly: no thinking, about 0.2 s
  instead of 3–5 s. If Claude solved something once, she has it as an example from then on.
- **Unlearning.** "No / that's wrong / not that" right after a request forgets that skill. ("No" as an
  answer to one of her questions doesn't count.)
- **Dreaming.** Once she has ≥80 wins (then ≥40 new ones, at most every 3 days), she fine-tunes a LoRA
  adapter for her model on them while you're away: no input for 30 min (needs `swayidle`, else Mio unused +
  GPU quiet + nothing playing), never during games. It takes about 30–40 min of GPU time, at the lowest
  CPU/IO priority, and stops the moment you talk to her, come back or start a game. Training peaks at ~9.5 GB VRAM
  with Whisper still loaded, so "Hey Mio" keeps working. The adapter is merged into Ollama's file of her model
  and quantized back to Q4_K_M (Ollama no longer loads LoRA adapters), so `mio-qwen35` is the same size and uses
  no more VRAM. Afterwards she takes an exam on wins she didn't train on, against the plain model, and only
  switches if it does better (a dream of another model is never used). Install it with
  `scripts/install_dream.sh [model]` (~25 GB for qwen3.5:9b). Turn it off with `[learning] train = false`;
  `use_trained = false` goes back to the plain model.
- `assistant learned` shows what she has learned and how far she is from the next dream; `assistant dream
  --dry` runs a test dream now. Data is in `~/.local/state/assistant/{skills,lexicon,dream}.json`. Delete those
  files to make her forget everything she learned.

## Thinking before Claude (`assistant/brain/local.py`, `assistant/awareness.py`)

- **One clear tool per job.** The local model gets one `screen` tool instead of read_screen / see_screen /
  terminal_text / look_at_screen: it reads the focused window's text (exact text for kitty, OCR otherwise) and,
  for visual questions or windows with little text, also asks her local eyes. `type_text` types into the
  browser's field when Brave is focused, else with the keyboard, and `submit=true` presses Enter. `launch_app`
  and `launch_game` hand over to each other ("open roblox"). Claude still sees every original tool. Old
  learned requests are read with the new names (`ALIASES` in learning.py).
- **Knowing what's going on.** Each request gets a "Right now" line (~10 ms, two local commands): focused
  window, the browser tab, the open Discord chat, what's playing, a running game, timers. "Tell them gg",
  "skip this", "what game is this" stop being guesses. People in the open Discord chat are learned as names.
- **Checking her own work.** A request with several parts ("put on lofi and set a timer") is split into its
  parts and each part needs a call that did it (by its words, or a tool its words point to; failed calls
  don't count). A missing part is done right away. No model call, so it's free and instant; it also runs
  after the planner and the fast path, which do one thing at a time.
- **The ladder before Claude.** First try → second, deeper try → for a question about the world, she
  researches it herself (web_research + answer) → otherwise she asks you one short question ("Close which
  window: the YouTube tab or Discord?"). Your answer is learned for the original request too. Only if that
  fails as well, or she already did part of the job, does Claude get it. Short vague requests ("close it")
  never go to Claude: it can't know either. `[brain] ask_before_claude = false` turns the asking off.
- **Asking instead of guessing.** When two learned requests match equally well, did different things, and
  one of them changes something, she asks which one you mean. Read-only guesses she just tries.
- **Small talk stays with her.** If both tries fail on something that isn't a request at all ("I don't mind",
  "you're so cute"), she just answers like a friend. No tools, no Claude.
- **Again, more, undo** (`assistant/redo.py`, no model): "do that again" repeats her last action, "a bit more"
  repeats an adjustment (volume, seeking, speed, scrolling), and "undo that" reverses it: the volume level from
  before, previous song, the closed tab back, do-not-disturb off, the timer cancelled… What she can't take
  back (an opened app, a sent message, the trash) she says plainly. Deleting, moving and installing are never
  repeated automatically.
- **Practice.** For each learned request she writes up to 4 other ways you might say it (casual, short, with
  typos). They're kept only if they still contain every name and number, and they help new wordings find the
  right learned request (counted a little lower than your real words). She practises in school and before
  each dream (stops the moment you're back), and the dream trains on them too, without leaking into the exam.
- School round 3: `scripts/school.py tests/curriculum_smarter.json` (your sites, friends, games, the new skills).

## Doing more on the PC (`assistant/actions.py`)

- **Several things at once.** A request with several parts ("open Discord and then turn on do not disturb",
  "put on lofi and make it quieter") is split into steps by a quick planner (her model, no thinking, ~1 s). Each
  step then goes through the fast path → local → Claude like any request, and is learned on its own. If a
  step fails or you decline it, she stops and says what she didn't do.
- **Routines you teach by voice.** "When I say gaming time, open Discord, launch Roblox and turn on do not
  disturb". After that, "gaming time" (or "start gaming time") runs it. "What routines do I have?" lists them,
  and she can delete them. Stored in `~/.local/state/assistant/routines.json`.
- **Keyboard.** `press_keys("ctrl+shift+t", app="kitty")` focuses the app first, then presses the keys.
- **Discord.** Open a DM, server or channel with the quick switcher ("open my DM with Alex"), read which
  chat is open, and send a message. Sending always asks you first and names the chat.
- **Files.** `find_files` ("the PDF I downloaded yesterday": newest first; if nothing turns up for that time
  span, it shows the newest matches anyway). `tidy_downloads` sorts ~/Downloads by kind; it shows the plan
  first and asks before moving. `rm` in her shell goes to the trash, so it can be undone. Package changes are
  already snapshotted by snap-pac.
- **Her own eyes.** `see_screen(question)` asks her local vision model (`vision_model`). qwen3.5:9b sees by
  itself, so looking costs no extra VRAM. A separate one (`qwen3-vl:4b-instruct`, 3.6 GB) only loads while she's
  looking, unloads 30 s later, and makes room by unloading her brain if VRAM is tight. Her eyes are off during
  games (then `look_at_screen` goes to Claude as before).
- **School** (`scripts/school.py`, lessons in `tests/curriculum.json`). Her real brain does ~100 lessons
  written the way you talk, with everything that would change the PC faked. Right answers become wins; for
  wrong ones the correct calls are recorded (src `teacher`). A separate quiz in other wordings is scored
  before and after. Add lessons for anything she gets wrong, and run it again after changing models or tools.
  Round 2, `scripts/school.py tests/curriculum_typos.json`, is the same with typos and shorthand.

## How costs are kept low

- The local model gets the full specs of the tools of the categories a request looks like (≤10, plurals
  and "-ing" forms count), `run_shell` always, short worked examples, and a one-line catalog of every other
  tool. When its thoughts name a catalog tool, that tool is handed over and it tries again.
- `local_thinking = "auto"`: she thinks (the model's thinking mode, no extra VRAM) before choosing a
  tool for everything except small talk. Thinking is streamed and cut off after `think_budget` tokens
  (default 350, ~4 s); she then acts on the thoughts so far. Answering from a tool result skips thinking
  unless the question is hard ("why/explain/compare…").
- Before anything goes to Claude she gets a second, deeper local attempt (thinking on every step, more tools,
  told why the first try failed). Actions already done aren't repeated. If she still has nothing to say she
  asks you to repeat it (likely misheard; Claude can't hear better). Only programming/writing jobs ("write
  me a script/email…") go straight to Claude, because the 8B model writes broken or even destructive code.
- Guards: no "I did it" without a tool, places she wasn't told are dropped ("Paris"), URLs with a path that
  nobody gave her become a web search for the best match, and invalid tool arguments come back as errors she
  can fix. Replies are cleaned of markdown and emoji before speaking.
- `assistant escalations` (or ask "what couldn't you do on your own?") lists what went to Claude in
  the last 7 days — turn frequent ones into fast-path commands or tools.
- "my shortcut / my config / how do I change your voice" is answered from a local search over the
  files in `[knowledge] paths` (lines containing passwords/tokens/keys are skipped).

- Most commands ("open firefox", "volume 40", "next song", "workspace 3") never reach an LLM.
- Ollama handles simple tool calls locally and is unloaded from VRAM after 5 minutes idle.
- Claude only gets: a ~120-word system prompt, the tool list, a summary + the current session's turns (with tool notes), and
  your request. No Claude Code system prompt, no built-in tools, no CLAUDE.md, no session files.
  A typical request is ~1–3k input tokens on Haiku (≈ $0.001–0.003, from the Agent SDK credit
  that comes with Pro since June 2026, not your interactive limits).
- Hard caps: `claude_max_turns = 4` tool round-trips, `claude_daily_limit = 60` requests/day.
  `assistant status` shows today's count; `~/.local/state/assistant/usage.json` has daily $.
- Memory is per session: while she's on screen she remembers the conversation plus short notes of
  what her tools found (including links), so follow-ups like "open their GitHub" work. When she
  hides (or after `session_timeout_s` untouched) it's all forgotten, so context never grows.
  Only facts you ask her to remember are kept (`~/.local/state/assistant/memory.json`).

## Security

- Every action is logged to `~/.local/state/assistant/audit.log`.
- `run_shell`: commands made only of `shell_allow` programs run directly; anything matching
  `shell_confirm_patterns` (rm, sudo, kill, systemctl changes, pacman/paru installs and removals, redirects,
  mv, git push…) or not allow-listed needs a confirmation; `shell_deny_patterns` are refused outright. Pipes
  inside quotes are arguments, `$(…)` and backticks always ask. Read-only uses like `pacman -Q`, `systemctl
  status`, `journalctl`, `ip` run directly. `sudo` commands open in a kitty window for your password.
- Writing, moving and trashing files always needs confirmation and only works inside `file_roots`.
  Deleting uses the trash, never `rm`.
- Confirmation = she asks out loud; say "yes"/"no", or click Allow/Deny on the notification.
  Nothing happens after `confirm_timeout_s` without an answer.
- Claude runs with `--tools ""` (no Bash/Edit of its own), `--strict-mcp-config` and only the
  `desk` tool server, with no settings or CLAUDE.md loaded.
- Sockets are in `$XDG_RUNTIME_DIR` with mode 600. API keys (if any) live in
  `~/.config/assistant/env` (mode 600), never in the config file.
- Nothing listens on the network. Audio never leaves the machine; only the transcribed text of
  requests that escalate to Claude is sent to Anthropic.

## Testing checklist

1. `systemctl --user status assistantd assistant-avatar` — both active; log ends with `ready`.
2. `assistant say "Hello, can you hear me?"` — she appears bottom-right on DP-1, mouth moves, hides after ~6 s.
3. Click the empty area of her window next to her — the click lands on the window below (click-through).
4. Drag her, scroll to resize, switch workspaces — she stays on top.
5. Mod+A → "what time is it" (fast path, instant).
6. Mod+A → "open dolphin" (fast path launches app).
7. Mod+A → "turn on night light" (local model → shell_panel tool). Log shows `local reply`.
8. Mod+A → "how much free space do I have in my downloads folder?" (local or Claude → run_shell `du`, no confirmation).
9. Mod+A → "delete the file test.txt in downloads" → she asks; say "no" → nothing happens; audit log shows `declined`.
10. `assistant status` — `claude_today` increments only for escalated requests.
11. Tray icon appears in the Noctalia bar; left click talks, right click shows the menu.
12. `noctalia msg status`, `niri msg layers` — `assistant-avatar` listed on the Overlay layer.

## Troubleshooting (Wayland / niri)

- **Avatar never appears**: `journalctl --user -u assistant-avatar`. "Failed to initialize
  layer surface" → gtk4-layer-shell must be LD_PRELOADed; use `bin/assistant-avatar`, not
  `python avatar.py`. Run `AVATAR_DEBUG=1 bin/assistant-avatar --show` to see it immediately and
  get the WebKit inspector (right-click is hide, so use the inspector shortcut Ctrl+Shift+I).
- **Black box instead of transparency**: WebKit DMA-BUF renderer on NVIDIA. Add
  `Environment=WEBKIT_DISABLE_DMABUF_RENDERER=1` to the avatar service (`systemctl --user edit assistant-avatar`).
  If WebGL fails completely, try `Environment=WEBKIT_DISABLE_COMPOSITING_MODE=1`.
- **Clicks don't go through**: niri honours input regions; if the whole box is clickable the
  model bounds weren't reported — check the log for "Model failed to load".
- **She shows on the wrong monitor**: set `output` (`niri msg outputs` lists connector names).
- **Keybind does nothing**: `niri validate`; make sure the `include "assistant.kdl"` line is in
  `config.kdl`; `assistant status` from a terminal tells you if the daemon is up.
- **Whisper falls back to CPU / CUDA errors**: `bin/assistantd` puts the pip CUDA libs on
  `LD_LIBRARY_PATH`; check `.venv/lib/python3.12/site-packages/nvidia/*/lib` exists (`uv sync`).
- **No sound / mic**: `wpctl status` — sounddevice uses the PipeWire default source/sink.
  Pick defaults in Noctalia's control center.
- **Claude errors**: run `claude -p hi` in a terminal; if it asks you to log in, do `/login`.
  `claude_backend = "off"` keeps everything local meanwhile.
- **High idle usage**: `systemctl --user status` shows memory; set STT to `small.en` on CPU, or
  `local_enabled = false` so Ollama never loads.

## Revert everything

Disable the services (`systemctl --user disable --now assistantd assistant-avatar`), remove the
`include "assistant.kdl"` line from your niri config and delete this folder, `~/.config/assistant`
and `~/.local/state/assistant`. System-level changes can be undone with snapper if you took the
snapshot from the install step: `snapper -c root list` → `sudo snapper -c root undochange <pre>..0`.

## Wake word, morning briefing, games, emotions

- **Wake word** — say "Hey Mio" (also "Hi/Okay Mio"). Silero VAD picks out short utterances and Whisper
  transcribes only those (~0.1 s GPU each, nothing while it's quiet); she wakes when a greeting + her name
  (Whisper often writes it "Miu") starts the sentence. The check runs *without* Whisper's name hint, which made
  it hear "Mio" in YouTube audio; one- or two-word results get a second look with the hint. Tested on the
  owner's voice and mic: 19/20 caught, 0 false wakes in 60 s of YouTube and in 40 tricky sentences. Near misses
  are logged ("not her name: …"). The old sherpa-onnx keyword model is `detector = "kws"` (cheaper, ~40%
  recall). Audio never leaves the PC and is ignored while she's busy or a game runs. Super+A always works.
- **Morning briefing** — once a day, ~25 s after the first login between 05:00 and 12:00, or any time with
  "good morning" / "catch me up": time, weather, waiting updates, and only *new* Arch/CachyOS news. No LLM.
- **Games** — "launch Sober via Froststrap" / "play Roblox" (`froststrap -player`), "open my Steam library",
  "start <Steam game>" (found from appmanifests). Add your own in `[games] launchers`.
- **Emotions** — her face follows what she says (happy, sad, surprised, shy, curious) and what she's doing
  (attentive while listening, thinking pose while working). Presets live in `avatar/web/app.js` (EMOTIONS) and
  use standard Cubism parameter ids, so other models react too.

- **Game mode** — while a game window is open (Steam `steam_app_*`, Sober, gamescope, `.exe`…, see
  `[gamemode]`), the Ollama model is unloaded (it reloads on demand and is freed again after `unload_after_s`).
  "Hey Mio" keeps working and she pops up on top of the game (layer-shell overlay, click-through); that keeps
  Whisper loaded (~1.3 GB VRAM). `wake_word = false` frees it too (0 MB, Super+A only), `show_avatar = false`
  makes her voice-only.
- **Phone (KDE Connect)** — "find my phone", "phone battery", "send this link to my phone". iPhone: install
  *KDE Connect* from the App Store, allow ports 1714–1764 in ufw, pair. iOS suspends the app in the
  background, so the phone only reacts while the app has been opened recently.

## Clips, timers, patch notes and sales

- **Clip that** — while a game runs, gpu-screen-recorder (`pacman -S gpu-screen-recorder`) keeps the last
  3 minutes of the game's monitor in RAM (NVENC, 1080p60 H.264 at 50 Mbps + desktop audio as AAC, ~1.2 GB
  RAM). "Clip that" saves the last 30 s, "clip the last 2 minutes" more; **Alt+F10** does it silently (chime +
  notification). The seconds it took you to say it are added, so the moment isn't cut off. Clips go to
  `~/Videos/Clips/<Game> <date time>.mp4`. Settings in `[clips]` (`always = true` records outside games too).
- **Timers and reminders** — "timer 10 minutes", "pizza timer for 12 minutes", "remind me in 20 minutes to …",
  "remind me to call mom at 6 pm", "tomorrow at 9", "set an alarm for 7 am", "how long is left", "cancel the
  pasta timer" / "cancel all reminders". Zero tokens; kept in `~/.local/state/assistant/timers.json`, so they
  survive restarts — anything that came due while she was off is announced late (up to 12 h).
- **Patch notes** — "did Roblox update?" (Roblox's deploy history + whether Sober has a flatpak update; Roblox
  publishes no client patch notes), "has Counter-Strike 2 gotten an update?" → latest official Steam
  announcement, "yes" → the local model summarizes the notes.
- **Steam prices** — "is Cyberpunk on sale?", "how much is Baldur's Gate 3 on Steam?", "any deals on Steam?",
  "is anything on my wishlist on sale?" (the wishlist must be public: Steam → Privacy → Game details). Euro
  prices from Steam's public APIs, no key.

## Browser extension (Brave Origin)

`browser/extension` is a small MV3 extension (her chibi icon, popup in her pink/mauve) that lets Mio read and
drive the browser. It talks only to `bin/mio-browser-host`, a native-messaging bridge Brave starts itself,
which exposes `$XDG_RUNTIME_DIR/mio-browser.sock` (0600) to the daemon. Nothing leaves the PC.

Install: `scripts/install_browser.sh` (registers the host for Brave Origin/Brave/Chromium/Chrome), then
brave://extensions → Developer mode → Load unpacked → `browser/extension`, and pin it. The extension id is
fixed (`hmjgoilonpblpoamhakkijgoedphnaim`) by the `key` in its manifest, so the host only accepts it.

- **Video**: "skip the ad", "2x speed" / "normal speed", "skip ahead 2 minutes", "go back 30 seconds",
  "next video", "what's this video about?" (transcript via `yt-dlp`, summarized by the local model).
- **Pages**: "summarize this page", "what does this article say about …" (real page text, not OCR).
- **Tabs**: "switch to my Discord tab", "close all YouTube tabs" (more than 3 asks first), "close this tab",
  "reopen the last tab".
- **Clicking/typing**: "click the first result", "click Sign up", "scroll down"; buttons like buy/pay/
  delete/send and sending non-search forms ask first; it never types into password fields.
- **Play**: "play lofi music on YouTube", "open Asian maps from Rohaze on YouTube", "search X on YouTube and
  open the first video" — finds the top result with `yt-dlp` and opens it directly (forgiving with misheard
  names). "Open the GitHub page of niri" opens the best web match instead of a guessed URL.
- **Searches**: "search lofi music on YouTube", "Amazon search for …" (also Google, Reddit, GitHub, Twitch,
  Steam, the AUR, Wikipedia, DuckDuckGo, Brave).
- **History**: "find that recipe I looked at yesterday".
- **Popup**: connection status, "Summarize this page", "What's this video about?", an ask box (she answers
  out loud), switches for reading pages and for clicking/typing, the off-limits site list (banking, PayPal,
  logins, password managers by default; "+ this site" adds the current one) and recent actions.

## Riff (music player)

`riff/mio.js` is a Riff plugin that lets Mio drive the player. Riff plugins can't listen for connections, so
the plugin long-polls `assistant/riff.py` on `127.0.0.1:38491` (GET `/mio/poll` waits for a command, POST
`/mio/result` answers). Both need the token in `~/.local/state/assistant/riff-token`, which the install script
writes into the plugin, and only a `127.0.0.1`/`localhost` Host header is accepted. The plugin works through
Riff's own search box, track rows and buttons, like a click, so it also works with Riff on another workspace.

Install: `scripts/install_riff.sh` (copies the plugin into Riff's data folder, `~/.cache/devsize/plugins`,
and turns it on; backs up `plugins.json`), then restart Riff. Run it again after editing `riff/mio.js`.

- **Songs**: "play blinding lights by the weeknd", "put on some jazz", "listen to tame impala" — searches Riff
  and plays the best match. Music goes to Riff; YouTube only when you say YouTube or video. Riff is started
  if it isn't open.
- **Playlists**: "play my Alex g playlist", "shuffle my lucio playlist", "open my French playlist" (shows it
  without playing), "what playlists do I have?" — names are matched loosely, so misheard names still work.
- **More**: "play my liked songs (on shuffle)", "play my wave" / "play some music", "what song is this?",
  "like this song" / "unlike this song", "turn shuffle on/off".
- "Pause", "next song", "previous song" control Riff while it plays, otherwise whatever `playerctl` picks.

## Stretch goals (not enabled)
  Push-to-talk is the default because it costs nothing while idle.
- Screen awareness: a `look_at_screen` tool (grim → Claude vision) only on explicit request.
- Proactive speaking: a systemd timer calling `assistant say` (e.g. calendar reminders) — zero tokens.
