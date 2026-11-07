#!/usr/bin/env python3
"""The small window "upload the clip" opens: watch it, name it, choose whether it keeps its sound, upload.

A layer-shell surface, so it also shows up over a game and needs no compositor rules. Nothing leaves the PC until
Upload is pressed. Enter uploads, Esc cancels; while it uploads the keyboard goes back to the game.
"""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant import clipshare, config  # noqa: E402

AUTO_CLOSE_MS = 12000
CSS = """
window { background: transparent; }
.card { background: #1e1e2e; border: 2px solid #cba6f7; border-radius: 16px; padding: 16px; color: #cdd6f4; }
.heading { color: #f5c2e7; font-size: 18px; font-weight: 700; }
.note { color: #a6adc8; font-size: 13px; }
.error { color: #f38ba8; }
.preview { background: #11111b; border-radius: 10px; }
entry { background: #313244; color: #cdd6f4; border: 1px solid #45475a; border-radius: 8px; min-height: 36px; caret-color: #f5c2e7; }
entry:focus-within { border-color: #f5c2e7; outline: none; }
button { background: #313244; color: #cdd6f4; border: none; border-radius: 8px; padding: 8px 16px; box-shadow: none; }
button:hover { background: #45475a; }
button.primary { background: #f5c2e7; color: #1e1e2e; font-weight: 700; }
button.primary:hover { background: #cba6f7; }
switch { background: #45475a; border: none; } switch:checked { background: #f5c2e7; } switch slider { background: #cdd6f4; }
progressbar trough { background: #313244; border-radius: 6px; min-height: 8px; }
progressbar progress { background: #f5c2e7; border-radius: 6px; min-height: 8px; }
"""


class ClipWindow:
    def __init__(self, app: Gtk.Application, path: Path):
        self.path = path
        self.cancel = threading.Event()
        self.running = False
        self.url = ""
        self.auto_close = 0
        clips = config.load().clips
        self.cap = int(clips.get("share_cap_mb", 120)) * 1_000_000
        self.max_kbps = int(clips.get("share_max_kbps", 32000))
        self.keep = int(clips.get("share_keep_gb", 9)) * 1_000_000_000
        self.info = clipshare.probe(path)

        win = self.win = Gtk.ApplicationWindow(application=app, title="Upload clip", resizable=False)
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "assistant-clipshare")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card"])
        card.set_size_request(560, -1)
        win.set_child(card)
        self.heading = Gtk.Label(label="Upload this clip?", xalign=0, css_classes=["heading"])
        card.append(self.heading)

        self.video = Gtk.Video.new_for_filename(str(path))
        self.video.set_autoplay(False)
        self.video.set_size_request(528, 297)
        self.video.add_css_class("preview")
        self.still = self.make_still()  # shown if this PC can't play it in the window (no gst-plugins-good)
        self.preview = Gtk.Stack()
        self.preview.add_named(self.video, "video")
        self.preview.add_named(self.still, "still")
        # a fixed frame: a video's or picture's natural size would stretch the whole window to 1080p
        frame = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                   propagate_natural_width=False, propagate_natural_height=False,
                                   min_content_width=528, min_content_height=297, max_content_width=528, max_content_height=297)
        frame.set_child(self.preview)
        card.append(frame)
        stream = self.video.get_media_stream()
        if stream:
            stream.connect("notify::error", lambda s, _p: self.preview.set_visible_child_name("still") if s.get_error() else None)
            if stream.get_error():
                self.preview.set_visible_child_name("still")

        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.append(self.form)
        self.entry = Gtk.Entry(text=clipshare.default_title(path), max_length=100, placeholder_text="Name of the clip")
        self.entry.connect("activate", lambda *_: self.start())
        self.form.append(self.entry)
        row = Gtk.Box(spacing=10)
        row.append(Gtk.Label(label="Include sound (it can contain voice chat)", xalign=0, hexpand=True))
        self.sound = Gtk.Switch(active=bool(self.info.audio_codec), sensitive=bool(self.info.audio_codec), valign=Gtk.Align.CENTER)
        self.sound.connect("notify::active", lambda *_: self.update_note())
        row.append(self.sound)
        self.form.append(row)

        self.note = Gtk.Label(xalign=0, wrap=True, css_classes=["note"])
        card.append(self.note)
        self.bar = Gtk.ProgressBar(visible=False)
        card.append(self.bar)
        self.link = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False, wrap_mode=2)
        card.append(self.link)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", lambda *_: self.quit())
        self.go = Gtk.Button(label="Upload", css_classes=["primary"])
        self.go.connect("clicked", lambda *_: self.primary())
        buttons.append(self.cancel_button)
        buttons.append(self.go)
        card.append(buttons)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        win.add_controller(keys)
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: self.keep_open())
        win.add_controller(hover)
        win.connect("close-request", lambda *_: self.quit() or True)
        self.update_note()
        win.present()
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def make_still(self) -> Gtk.Widget:
        """A frame from the middle of the clip with a button that opens the real player."""
        overlay = Gtk.Overlay(css_classes=["preview"])
        overlay.set_size_request(528, 297)
        fd, name = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            clipshare.thumbnail(self.path, Path(name), self.info.seconds)
            picture = Gtk.Picture.new_for_filename(name)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            overlay.set_child(picture)
        except (clipshare.ShareError, subprocess.SubprocessError):
            pass  # still has the button
        finally:
            GLib.timeout_add(3000, lambda: os.path.exists(name) and os.unlink(name))
        watch = Gtk.Button(label="▶  Watch", css_classes=["primary"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        watch.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(self.path.as_uri(), None))
        overlay.add_overlay(watch)
        return overlay

    def update_note(self):
        plan = clipshare.plan_for(self.info, self.cap, self.max_kbps, self.sound.get_active())
        self.note.set_label(clipshare.describe(self.info, plan))
        self.note.remove_css_class("error")

    def on_key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.quit()
            return True
        return False

    def primary(self):
        if self.url:
            self.quit()
        else:
            self.start()

    def start(self):
        if self.running or self.url:
            return
        self.running = True
        self.cancel.clear()
        stream = self.video.get_media_stream()
        if stream:
            stream.pause()
        self.form.set_sensitive(False)
        self.go.set_sensitive(False)
        self.bar.set_visible(True)
        self.bar.set_fraction(0)
        self.heading.set_label("Uploading…")
        self.note.remove_css_class("error")
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)  # type in the game again while it runs
        threading.Thread(target=self.work, args=(self.entry.get_text(), self.sound.get_active()), daemon=True).start()

    def work(self, title: str, audio: bool):
        def progress(stage: str, fraction: float):
            GLib.idle_add(self.show_progress, stage, fraction)
        try:
            shared = clipshare.share(self.path, title, audio, self.cap, self.max_kbps, self.keep, progress, self.cancel)
        except clipshare.Cancelled:
            return
        except clipshare.ShareError as e:
            GLib.idle_add(self.failed, str(e))
        except Exception as e:  # the window must say something, not just hang on "Uploading…"
            GLib.idle_add(self.failed, f"something went wrong ({type(e).__name__})")
        else:
            GLib.idle_add(self.finished, shared)

    def show_progress(self, stage: str, fraction: float):
        self.note.set_label(stage)
        self.bar.set_fraction(fraction)

    def failed(self, message: str):
        self.running = False
        self.heading.set_label("That didn't work")
        self.note.set_label(message[0].upper() + message[1:] + ".")
        self.note.add_css_class("error")
        self.bar.set_visible(False)
        self.form.set_sensitive(True)
        self.go.set_sensitive(True)
        self.go.set_label("Try again")
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.EXCLUSIVE)
        self.entry.grab_focus()

    def finished(self, shared: clipshare.Shared):
        self.running = False
        self.url = shared.url
        subprocess.Popen(["wl-copy", shared.url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["notify-send", "-a", "Assistant", "-t", "6000", "Clip uploaded", "The link is in your clipboard."],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.heading.set_label("Link copied!")
        made_room = f" Removed {shared.removed} old clip{'s' if shared.removed != 1 else ''} to make room." if shared.removed else ""
        self.note.set_label(f"{shared.bytes / 1e6:.0f} MB. Paste it into Discord.{made_room}")
        self.bar.set_visible(False)
        self.form.set_visible(False)
        self.link.set_label(shared.url)
        self.link.set_visible(True)
        self.cancel_button.set_visible(False)
        self.go.set_label("Close")
        self.go.set_sensitive(True)
        self.auto_close = GLib.timeout_add(AUTO_CLOSE_MS, self.quit)

    def keep_open(self):
        """The pointer is on it: don't close under the user's hand."""
        if self.auto_close:
            GLib.source_remove(self.auto_close)
            self.auto_close = 0

    def quit(self, *_):
        self.cancel.set()
        stream = self.video.get_media_stream()
        if stream:
            stream.pause()
        self.win.get_application().quit()
        return False


def main():
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    if not path or not path.is_file():
        sys.exit("usage: clip_window.py CLIP.mp4")
    app = Gtk.Application(application_id="dev.mio.clipshare", flags=Gio.ApplicationFlags.NON_UNIQUE)
    keep = []
    app.connect("activate", lambda a: keep.append(ClipWindow(a, path)))
    sys.exit(app.run([]))


if __name__ == "__main__":
    main()
