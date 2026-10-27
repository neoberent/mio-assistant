"""'Do that again', 'more', 'undo that': instant and certain, no model needed.

Her last request that changed something is kept (its tool calls). "Again" runs those calls once more through the
normal permission gate; "more" / "a bit more" only repeats adjustments (volume, seeking, speed, scrolling).
"Undo" runs the opposite of each call where one exists (volume back to the level it had, next ↔ previous song,
a closed tab reopened, dnd on ↔ off, a timer cancelled…) and says honestly what it can't take back.
"""
from __future__ import annotations

import re

AGAIN = re.compile(r"^\W*(?:(?:mio,? )?(?:do|say|try) (?:that|it|this) again|again|one more time|once more|"
                   r"repeat that|same again|do the same(?: thing)?(?: again)?)(?: please| pls)?\W*$", re.I)
MORE = re.compile(r"^\W*(?:(?:a )?(?:bit|little|lil|tad) more|(?:even |some |a lot )?more|keep going|"
                  r"more please|bit more)\W*$", re.I)
UNDO = re.compile(r"^\W*(?:(?:no,? )?undo(?: that| it| this)?|take (?:that|it) back|revert(?: that| it)?|"
                  r"change it back|put it back|turn it back|go back to how it was|make it like before)"
                  r"(?: please| pls)?\W*$", re.I)



def more_of(calls: list[dict]) -> list[dict]:
    out = []
    for c in calls:
        if c["name"] == "volume" and c["args"].get("mode") in ("up", "down"):
            out.append(c)
        elif c["name"] == "browser_video" and c["args"].get("action") in ("seek",):
            out.append(c)
        elif c["name"] == "browser_video" and c["args"].get("action") == "speed" and c["args"].get("value"):
            v = float(c["args"]["value"])
            out.append({"name": c["name"], "args": {"action": "speed", "value": min(v + 0.25, 3) if v >= 1
                                                    else max(v - 0.25, 0.25)}})
        elif c["name"] == "browser_scroll" and c["args"].get("direction") in ("up", "down"):
            out.append(c)
    return out


def _flip_set(cmd: str) -> str | None:
    if m := re.match(r"(.+-set) (on|off)$", cmd):
        return f"{m[1]} {'off' if m[2] == 'on' else 'on'}"
    if cmd.endswith("-toggle"):
        return cmd
    return None


def inverse(c: dict, before: dict) -> dict | str:
    """The call that undoes `c`, or a short sentence why it can't be undone. before: state saved before it ran
    (e.g. {"volume": 0.45})."""
    name, a = c["name"], c["args"]
    if name == "volume":
        if "volume" in before:
            return {"name": "volume", "args": {"mode": "set", "value": round(before["volume"] * 100)}}
        if a.get("mode") == "mute":
            return {"name": "volume", "args": {"mode": "mute"}}  # it toggles
        if a.get("mode") in ("up", "down"):
            return {"name": "volume", "args": {"mode": "down" if a["mode"] == "up" else "up",
                                               **({"value": a["value"]} if a.get("value") else {})}}
    if name == "media":
        flip = {"next": "previous", "previous": "next", "play-pause": "play-pause"}.get(a.get("action"))
        if flip:
            return {"name": "media", "args": {"action": flip}}
    if name == "browser_video":
        act, v = a.get("action"), a.get("value")
        if act == "pause":
            return {"name": "browser_video", "args": {"action": "play"}}
        if act == "play":
            return {"name": "browser_video", "args": {"action": "pause"}}
        if act == "seek" and v:
            return {"name": "browser_video", "args": {"action": "seek", "value": -float(v)}}
        if act == "speed":
            return {"name": "browser_video", "args": {"action": "speed", "value": before.get("speed", 1)}}
    if name == "browser_tabs" and a.get("action") == "close":
        return {"name": "browser_tabs", "args": {"action": "reopen"}}
    if name == "browser_scroll" and a.get("direction") in ("up", "down"):
        return {"name": "browser_scroll", "args": {"direction": "up" if a["direction"] == "down" else "down"}}
    if name == "shell_panel" and (flip := _flip_set(str(a.get("command", "")))):
        return {"name": "shell_panel", "args": {"command": flip}}
    if name == "niri_action" and a.get("action") in ("toggle-window-floating", "fullscreen-window",
                                                      "maximize-column", "toggle-overview"):
        return c  # those toggle
    if name == "niri_action" and a.get("action") in ("move-window-to-monitor-left", "move-window-to-monitor-right"):
        side = "right" if a["action"].endswith("left") else "left"
        return {"name": "niri_action", "args": {"action": f"move-window-to-monitor-{side}"}}
    if name == "timer" and a.get("action") == "set":
        label = a.get("label") or (f"{a['minutes']} minutes" if a.get("minutes") else "")
        return {"name": "timer", "args": {"action": "cancel", **({"label": label} if label else {})}}
    if name == "trash":
        return "it's in the trash, so you can restore it from there"
    if name in ("launch_app", "launch_game", "open", "youtube_play", "open_best_match", "search_site"):
        return "I can't close what I opened by myself, but you can say 'close this window' or 'close this tab'"
    if name == "discord" and a.get("action") == "send":
        return "a sent message can't be taken back by me"
    return f"I don't know how to undo {name.replace('_', ' ')}"
