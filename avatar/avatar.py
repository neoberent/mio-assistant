#!/usr/bin/env python3
"""Avatar overlay: a transparent layer-shell surface (OVERLAY layer) hosting a WebKit view
that renders the Live2D model. Only the model's bounding box accepts input; everything
else is click-through. Hidden = unmapped, so it costs nothing on the GPU.

Two layouts, chosen each time she appears:
  stage   — the surface covers her whole monitor; she's drawn in a box at a fresh spot, with the screen glow,
            working capsule and sparkles around her (normal desktop use).
  compact — the old small corner window (while a game runs: nothing full-screen over the game).

Listens on $XDG_RUNTIME_DIR/assistant-avatar.sock for JSON lines from assistantd:
  {"cmd": "show"|"hide"|"state"|"speak"|"speak_end"|"error", ...}
"""
import json
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("WebKit", "6.0")
import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "avatar" / "web"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCK = RUNTIME / "assistant-avatar.sock"
DAEMON_SOCK = RUNTIME / "assistant.sock"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "assistant" / "avatar.json"


def load_cfg() -> dict:
    cfg = tomllib.loads((ROOT / "config" / "config.toml").read_text()).get("avatar", {})
    user = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "assistant" / "config.toml"
    if user.exists():
        cfg.update(tomllib.loads(user.read_text()).get("avatar", {}))
    try:
        cfg.update(json.loads(STATE.read_text()))  # remembered position / size
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def daemon(cmd: str):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(DAEMON_SOCK))
            s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
            s.recv(1024)
    except OSError:
        pass


class Avatar(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.mio.AssistantAvatar", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.cfg = load_cfg()
        self.win = None
        self.view = None
        self.bounds = None
        self.dragging = False
        self.stage = False
        self.start_hidden = "--show" not in sys.argv

    # ── window ──────────────────────────────────────────────────────────────
    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_string("window, window.background { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = Gtk.ApplicationWindow(application=self, title="assistant-avatar", decorated=False)
        self.win = win
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "assistant-avatar")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(win, 0)
        LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        mon = self._monitor(self.cfg.get("output"))
        if mon:
            LayerShell.set_monitor(win, mon)
        self._apply_geometry()

        view = WebKit.WebView()
        self.view = view
        view.set_background_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=0))
        s = view.get_settings()
        s.set_allow_file_access_from_file_urls(True)
        s.set_allow_universal_access_from_file_urls(True)
        s.set_enable_webgl(True)
        s.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)
        s.set_enable_developer_extras(os.environ.get("AVATAR_DEBUG") == "1")
        ucm = view.get_user_content_manager()
        ucm.register_script_message_handler("avatar", None)
        ucm.connect("script-message-received::avatar", self._on_js)
        win.set_child(view)

        model = self.cfg.get("model", "Hiyori/Hiyori.model3.json")
        if "--model" in sys.argv:  # preview another model without editing the config
            model = sys.argv[sys.argv.index("--model") + 1]
        uri = (WEB / "index.html").as_uri() + "?model=" + GLib.uri_escape_string(f"models/{model}", "/", False)
        zoom = self.cfg.get("zoom", 1.0)  # >1 crops in on models with wide props (umbrellas, wings)
        if "--zoom" in sys.argv:
            zoom = float(sys.argv[sys.argv.index("--zoom") + 1])
        focus = self.cfg.get("focus", 1.0)  # which height of the model stays put when zoomed: 0 top, 1 feet
        if "--focus" in sys.argv:
            focus = float(sys.argv[sys.argv.index("--focus") + 1])
        if zoom != 1.0:
            uri += f"&zoom={zoom}&focus={focus}"
        if self.cfg.get("emotions"):  # per-model face presets, replace the defaults in app.js
            uri += "&emotions=" + GLib.uri_escape_string(json.dumps(self.cfg["emotions"]), None, False)
        recolor = self.cfg.get("recolor")
        if "--recolor" in sys.argv:
            recolor = json.loads(sys.argv[sys.argv.index("--recolor") + 1])
        if "--dump" in sys.argv:
            uri += "&dump=" + GLib.uri_escape_string(sys.argv[sys.argv.index("--dump") + 1], None, False)
        if recolor and not ("--model" in sys.argv and "--recolor" not in sys.argv):
            uri += "&recolor=" + GLib.uri_escape_string(json.dumps(recolor), None, False)
        view.load_uri(uri)
        win.connect("map", lambda *_: self._apply_input_region())

        self._serve()
        if not self.start_hidden:
            win.present()
        else:
            win.realize()

    def _monitor(self, connector):
        monitors = Gdk.Display.get_default().get_monitors()
        names = [connector]
        try:  # configured output missing (renamed/unplugged): use the one you're working on
            out = subprocess.run(["niri", "msg", "-j", "focused-output"], capture_output=True, text=True, timeout=1)
            names.append(json.loads(out.stdout).get("name"))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        for name in names:
            for i in range(monitors.get_n_items()):
                m = monitors.get_item(i)
                if name and m.get_connector() == name:
                    return m
        return None

    def _apply_geometry(self):
        scale = float(self.cfg.get("scale", 1.0))
        w, h = int(self.cfg.get("width", 360) * scale), int(self.cfg.get("height", 560) * scale)
        mon = self._monitor(self.cfg.get("output"))
        if mon:  # keep her on the monitor whatever the saved state says
            g = mon.get_geometry()
            w, h = min(w, g.width), min(h, g.height)
            self.cfg["margin_right"] = min(int(self.cfg.get("margin_right", 40)), g.width - w)
            self.cfg["margin_bottom"] = min(int(self.cfg.get("margin_bottom", 0)), g.height - h)
        if self.stage:  # covers the monitor; the page places her inside
            for e in (LayerShell.Edge.LEFT, LayerShell.Edge.TOP, LayerShell.Edge.RIGHT, LayerShell.Edge.BOTTOM):
                LayerShell.set_anchor(self.win, e, True)
                LayerShell.set_margin(self.win, e, 0)
            if mon:
                g = mon.get_geometry()
                self.win.set_size_request(g.width, g.height)
                self.win.set_default_size(g.width, g.height)
            return
        LayerShell.set_anchor(self.win, LayerShell.Edge.LEFT, False)
        LayerShell.set_anchor(self.win, LayerShell.Edge.TOP, False)
        LayerShell.set_margin(self.win, LayerShell.Edge.LEFT, 0)
        LayerShell.set_margin(self.win, LayerShell.Edge.TOP, 0)
        self.win.set_default_size(w, h)
        self.win.set_size_request(w, h)
        LayerShell.set_margin(self.win, LayerShell.Edge.RIGHT, int(self.cfg.get("margin_right", 40)))
        LayerShell.set_margin(self.win, LayerShell.Edge.BOTTOM, int(self.cfg.get("margin_bottom", 0)))

    def _apply_input_region(self):
        surface = self.win.get_surface() if self.win else None
        if surface is None:
            return
        if self.bounds and self.bounds[2] >= 1 and self.bounds[3] >= 1:
            x, y, w, h = (int(v) for v in self.bounds)
            surface.set_input_region(cairo.Region(cairo.RectangleInt(x, y, max(1, w), max(1, h))))
        else:
            surface.set_input_region(cairo.Region())  # fully click-through until the model has loaded

    def _drag_start(self):
        """Cover the whole monitor while dragging (see the drag notes in app.js)."""
        mon = self._monitor(self.cfg.get("output"))
        if not mon:
            return
        g = mon.get_geometry()
        w, h = self.win.get_width(), self.win.get_height()
        x = g.width - int(self.cfg.get("margin_right", 40)) - w
        y = g.height - int(self.cfg.get("margin_bottom", 0)) - h
        self.dragging = True
        surface = self.win.get_surface()
        if surface:
            surface.set_input_region(None)  # whole window while dragging
        self.win.set_size_request(g.width, g.height)
        self.win.set_default_size(g.width, g.height)
        LayerShell.set_margin(self.win, LayerShell.Edge.RIGHT, 0)
        LayerShell.set_margin(self.win, LayerShell.Edge.BOTTOM, 0)
        self.js(f"avatar.dragFrame({x}, {y}, {w}, {h}, {g.width})")

    def _save_state(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: self.cfg[k] for k in ("margin_right", "margin_bottom", "scale", "home") if k in self.cfg}
        STATE.write_text(json.dumps(keep))

    def js(self, code: str):
        self.view.evaluate_javascript(code, -1, None, None, None, None, None)

    def show(self, game: bool = False):
        arriving = not self.win.get_visible()
        if arriving:  # pick the layout only on a fresh appearance; never jump while she's on screen
            self.stage = bool(self.cfg.get("stage", True)) and not game
            mon = self._monitor(self.cfg.get("output"))
            if mon:
                LayerShell.set_monitor(self.win, mon)
            self._apply_geometry()
            scale = float(self.cfg.get("scale", 1.0))
            g = mon.get_geometry() if mon else None
            self.js("avatar.layout(%s)" % json.dumps({
                "mw": g.width if g else 0, "mh": g.height if g else 0,
                "stage": self.stage, "w": int(self.cfg.get("width", 360) * scale),
                "h": int(self.cfg.get("height", 560) * scale), "home": self.cfg.get("home"),
                "fixed": not self.cfg.get("move_around", False),  # her corner, like the compact window
                "margins": [int(self.cfg.get("margin_right", 40)), int(self.cfg.get("margin_bottom", 0))],
                "glow": bool(self.cfg.get("glow", True))}))
            self.win.present()
        self.js("avatar.setActive(true)")
        if arriving:
            self.js("avatar.arrive()")

    def hide(self):
        self.js("avatar.setActive(false)")
        self.win.set_visible(False)

    # ── messages from the page ──────────────────────────────────────────────
    def _on_js(self, _ucm, value):
        msg = json.loads(value.to_json(0))
        t = msg.get("t")
        if t == "bounds":
            self.bounds = (msg["x"], msg["y"], msg["w"], msg["h"])
            if not self.dragging:
                self._apply_input_region()
        elif t == "drag_start":
            self._drag_start()
        elif t == "drag_end":
            self.dragging = False
            self.cfg["margin_right"] = max(0, int(self.cfg.get("margin_right", 40) - msg["dx"]))
            self.cfg["margin_bottom"] = max(0, int(self.cfg.get("margin_bottom", 0) - msg["dy"]))
            self._apply_geometry()
            self._save_state()
        elif t == "stage_drag":  # stage: she moves inside the full-screen surface, so just open up input
            self.dragging = True
            surface = self.win.get_surface()
            if surface:
                surface.set_input_region(None)
        elif t == "stage_drop":  # remember where she was dropped
            self.dragging = False
            if self.cfg.get("move_around", False):  # her favourite spot, as a fraction of the monitor
                self.cfg["home"] = [round(msg["fx"], 4), round(msg["fy"], 4)]
            else:  # her fixed corner: same margins the compact (game) window uses
                self.cfg["margin_right"] = max(0, int(msg["mr"]))
                self.cfg["margin_bottom"] = max(0, int(msg["mb"]))
            self._save_state()
            self._apply_input_region()
        elif t == "scale":
            self.cfg["scale"] = min(3.0, max(0.4, float(self.cfg.get("scale", 1.0)) * msg["f"]))
            if self.stage:
                s = float(self.cfg["scale"])
                self.js("avatar.resize(%d, %d)" % (self.cfg.get("width", 360) * s, self.cfg.get("height", 560) * s))
            else:
                self._apply_geometry()
            self._save_state()
        elif t == "log":
            print("page:", msg.get("text"), flush=True)
        elif t == "daemon":
            GLib.idle_add(lambda: daemon(msg["cmd"]) and False)

    # ── socket from assistantd ──────────────────────────────────────────────
    def _serve(self):
        if SOCK.exists():
            SOCK.unlink()
        self.service = Gio.SocketService()
        self.service.add_address(Gio.UnixSocketAddress.new(str(SOCK)), Gio.SocketType.STREAM,
                                 Gio.SocketProtocol.DEFAULT, None)
        os.chmod(SOCK, 0o600)
        self.service.connect("incoming", self._incoming)
        self.service.start()

    def _incoming(self, _service, conn, _source):
        stream = Gio.DataInputStream.new(conn.get_input_stream())
        self._read(stream, conn)
        return True

    def _read(self, stream, conn):
        def done(src, res):
            try:
                line, _ = src.read_line_finish_utf8(res)
            except GLib.Error:
                line = None
            if line is None:
                conn.close(None)
                return
            if line.strip():
                self._command(json.loads(line))
            self._read(stream, conn)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, done)

    def _command(self, msg: dict):
        cmd = msg.get("cmd")
        if cmd == "show":
            self.show(bool(msg.get("game")))
        elif cmd == "hide":
            self.hide()
        else:
            self.js(f"avatar.command({json.dumps(msg)})")


if __name__ == "__main__":
    sys.exit(Avatar().run([sys.argv[0]]))
