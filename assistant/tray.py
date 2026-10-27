"""StatusNotifierItem tray icon (shown by Noctalia's tray widget) with a small dbusmenu.

Left click: talk · Middle click: show/hide avatar · Right click: menu
"""
import asyncio
import logging
import os
import subprocess

from dbus_fast import PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from .config import CONFIG_FILE

log = logging.getLogger("tray")
ICON = "audio-input-microphone-symbolic"


class Item(ServiceInterface):
    def __init__(self, app):
        super().__init__("org.kde.StatusNotifierItem")
        self.app = app

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "assistant"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return self.app.cfg.persona.get("name", "Assistant")

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ICON

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return [ICON, [], self.app.cfg.persona.get("name", "Assistant"), "Click to talk · middle-click to show/hide"]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return "/MenuBar"

    @method()
    def Activate(self, x: "i", y: "i"):
        asyncio.create_task(self.app.ptt())

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        asyncio.create_task(self.app.toggle_pin())

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @signal()
    def NewIcon(self):
        pass


MENU = [(1, "Talk"), (2, "Show / hide character"), (3, "Stop speaking"), (4, "Forget conversation"),
        (5, "Edit settings"), (6, "Quit assistant")]


class Menu(ServiceInterface):
    def __init__(self, app):
        super().__init__("com.canonical.dbusmenu")
        self.app = app

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        children = [Variant("(ia{sv}av)", [i, {"label": Variant("s", label), "enabled": Variant("b", True),
                                               "visible": Variant("b", True)}, []]) for i, label in MENU]
        return [1, [0, {"children-display": Variant("s", "submenu")}, children]]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        return [[i, {"label": Variant("s", label)}] for i, label in MENU if not ids or i in ids]

    @method()
    def GetProperty(self, id: "i", name: "s") -> "v":
        return Variant("s", dict(MENU).get(id, ""))

    @method()
    def Event(self, id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self._clicked(id)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for id_, ev, _, _ in events:
            if ev == "clicked":
                self._clicked(id_)
        return []

    @method()
    def AboutToShow(self, id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @signal()
    def LayoutUpdated(self) -> "ui":
        return [1, 0]

    def _clicked(self, id_: int):
        app = self.app
        actions = {
            1: lambda: asyncio.create_task(app.ptt()),
            2: lambda: asyncio.create_task(app.toggle_pin()),
            3: app.player.stop,
            4: app.memory.clear,
            5: lambda: subprocess.Popen(["xdg-open", str(CONFIG_FILE)]),
            6: lambda: os.kill(os.getpid(), 15),
        }
        if id_ in actions:
            actions[id_]()


class Tray:
    def __init__(self, app):
        self.app = app

    async def run(self):
        bus = await MessageBus().connect()
        bus.export("/StatusNotifierItem", Item(self.app))
        bus.export("/MenuBar", Menu(self.app))
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        await bus.request_name(name)
        await self._register(bus, name)

        # Re-register whenever the shell (tray watcher) restarts, e.g. after `killall noctalia`.
        intro = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        dbus = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", intro) \
            .get_interface("org.freedesktop.DBus")

        def owner_changed(svc, _old, new):
            if svc == "org.kde.StatusNotifierWatcher" and new:
                asyncio.create_task(self._register(bus, name))
        dbus.on_name_owner_changed(owner_changed)
        await bus.wait_for_disconnect()

    async def _register(self, bus, name):
        for attempt in range(60):  # the shell's tray watcher may start after us at login
            try:
                intro = await bus.introspect("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
                watcher = bus.get_proxy_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", intro)
                await watcher.get_interface("org.kde.StatusNotifierWatcher").call_register_status_notifier_item(name)
                log.info("tray icon registered")
                return
            except Exception as e:
                if attempt == 59:
                    log.warning("no tray watcher (%s)", e)
                await asyncio.sleep(2)
