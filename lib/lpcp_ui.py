# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""The control panel window (GTK 3): one row per registered project with a status dot (click to start or
stop it), its name, its CPU and memory use, and a launch button per launcher (its `./dev config.launchers`, or
"open" for its URL). `./dev ui` opens it; `./dev ui --tray` puts an icon in the
panel's tray instead, which shows and hides the window; `--install` adds a launcher to the desktop's
application menu (and, with --tray, starts the icon at login). Closing the window leaves every project as it is.

GTK is only needed here: the command line stays standard-library only. The tray icon uses XApp (Linux Mint);
without it, --tray opens the window as usual.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

import lpcp
import lpcp_monitor as mon

REPO = Path(__file__).resolve().parent.parent
ICON = REPO / "share" / "lpcp.svg"
APP_ID = "com.antzcode.lpcp"
WM_CLASS = "lpcp"
WIDTH = 450
REFRESH = 2.0           # seconds between refreshes; reading docker stats adds about 2 more
EDGE = 64               # px: with no work area known, a launch this close to a screen edge counts as a panel click
COLORS = {"green": (0.18, 0.74, 0.31, 1.0), "red": (0.89, 0.22, 0.21, 1.0), "off": (0.0, 0.0, 0.0, 0.25)}
CSS = b"""
.lpcp-dot, .lpcp-dot:hover, .lpcp-dot:active, .lpcp-dot:focus {
  padding: 4px; min-width: 0; min-height: 0;
  border-color: transparent; background: none; box-shadow: none;
}
.lpcp-name { font-weight: bold; }
.lpcp-metric { font-feature-settings: "tnum"; }
.lpcp-dim { opacity: 0.45; }
.lpcp-note { opacity: 0.75; }
.lpcp-launch, .lpcp-launch:hover, .lpcp-launch:active, .lpcp-launch:focus, .lpcp-launch:disabled {
  padding: 2px; min-width: 0; min-height: 0;
  border-color: transparent; background: none; box-shadow: none;
}
"""


# ---- launcher icons: a round, coloured badge with a white glyph per class (lpcp.LAUNCHER_CLASSES)

ICON_SIZE = 20          # px: a launcher icon's diameter
LAUNCHER_COLORS = {     # badge colours, chosen to differ from each other and from the status dot's green and red
    "web": (0.20, 0.52, 0.89), "mail": (0.91, 0.45, 0.18), "db": (0.55, 0.36, 0.78),
    "logs": (0.15, 0.61, 0.58), "config": (0.74, 0.60, 0.10), "tool": (0.42, 0.47, 0.53),
}


def _ellipse(cr, x, y, rx, ry, start=0.0, end=2 * math.pi):
    cr.save()
    cr.translate(x, y)
    cr.scale(rx, ry)
    cr.arc(0, 0, 1, start, end)
    cr.restore()        # the path keeps its shape; the stroke that follows uses the normal line width


def _glyph_web(cr):     # a globe
    _ellipse(cr, 0, 0, 1, 1)
    cr.new_sub_path()
    _ellipse(cr, 0, 0, 0.45, 1)
    cr.move_to(-1, 0)
    cr.line_to(1, 0)


def _glyph_mail(cr):    # an envelope
    cr.rectangle(-1, -0.7, 2, 1.4)
    cr.move_to(-1, -0.7)
    cr.line_to(0, 0.1)
    cr.line_to(1, -0.7)


def _glyph_db(cr):      # a cylinder
    _ellipse(cr, 0, -0.65, 0.95, 0.35)
    cr.new_sub_path()
    _ellipse(cr, 0, 0, 0.95, 0.35, 0, math.pi)
    cr.new_sub_path()
    _ellipse(cr, 0, 0.65, 0.95, 0.35, 0, math.pi)
    cr.move_to(-0.95, -0.65)
    cr.line_to(-0.95, 0.65)
    cr.move_to(0.95, -0.65)
    cr.line_to(0.95, 0.65)


def _glyph_logs(cr):    # lines of text
    for y, x2 in ((-0.7, 1.0), (0.0, 0.5), (0.7, 0.8)):
        cr.move_to(-1, y)
        cr.line_to(x2, y)


def _glyph_config(cr):  # a gear
    cr.arc(0, 0, 0.5, 0, 2 * math.pi)
    for n in range(8):
        angle = n * math.pi / 4
        cr.new_sub_path()
        cr.move_to(0.7 * math.cos(angle), 0.7 * math.sin(angle))
        cr.line_to(1.0 * math.cos(angle), 1.0 * math.sin(angle))


def _glyph_tool(cr):    # a wrench: a ring open at the far end (the jaw), on a handle. A narrow gap reads as a magnifier
    cr.arc(-0.42, -0.42, 0.58, 4.88, 9.28)
    cr.move_to(-0.01, -0.01)
    cr.line_to(0.95, 0.95)


GLYPHS = {"web": _glyph_web, "mail": _glyph_mail, "db": _glyph_db, "logs": _glyph_logs,
          "config": _glyph_config, "tool": _glyph_tool}


def paint_launcher(cr, kind: str, size: float, alpha: float = 1.0, hover: bool = False) -> None:
    """Draw a launcher's icon, `size` px across, on a cairo context at its top left corner. An unknown class
    is drawn as a tool. `hover` lightens the badge."""
    kind = kind if kind in GLYPHS else "tool"
    r, g, b = LAUNCHER_COLORS[kind]
    if hover:
        r, g, b = (c + (1 - c) * 0.25 for c in (r, g, b))
    radius = size / 2
    cr.save()
    cr.translate(radius, radius)
    cr.set_source_rgba(r, g, b, alpha)
    cr.arc(0, 0, radius - 0.5, 0, 2 * math.pi)
    cr.fill()
    cr.scale(radius * 0.52, radius * 0.52)      # glyph coordinates run -1..1
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.set_line_width(0.3)
    cr.set_line_cap(1)      # round
    cr.set_line_join(1)
    GLYPHS[kind](cr)
    cr.stroke()
    cr.restore()


SIDES = {0: "left", 1: "right", 2: "top", 3: "bottom"}   # Gtk.PositionType, as the tray reports the panel's side
TRAY_WAIT = 30.0       # s: how long --tray waits for the panel's tray applet to appear (it starts late at login)
FOCUS_GRACE = 0.5       # s: a tray click this soon after the window lost focus still counts as "it was in front"


def desktop_entry(tray: bool = False) -> str:
    command = f'"{REPO / "dev"}" ui' + (" --tray" if tray else "")
    if os.environ.get("LPCP_HOME"):
        command = f'env "LPCP_HOME={lpcp.home()}" {command}'
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=LPCP",
        "GenericName=Docker project control panel",
        "Comment=Start and stop your local docker projects", f"Exec={command}", f"Icon={ICON}",
        "Terminal=false", "Categories=Development;", f"StartupWMClass={WM_CLASS}",
        f"StartupNotify={'false' if tray else 'true'}", ""])


def install_launcher(tray: bool = False) -> list[str]:
    """Write the menu launcher; with tray, also an autostart entry (removed again by a plain --install)."""
    apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    autostart = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "lpcp.desktop"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "lpcp.desktop").write_text(desktop_entry(tray), encoding="utf-8")
    done = [f"Launcher written to {apps / 'lpcp.desktop'}"]
    if tray:
        autostart.parent.mkdir(parents=True, exist_ok=True)
        autostart.write_text(desktop_entry(tray), encoding="utf-8")
        done.append(f"Autostart entry written to {autostart}: the tray icon starts when you log in")
    elif autostart.exists() and f'"{REPO / "dev"}" ui' in autostart.read_text(encoding="utf-8"):
        autostart.unlink()
        done.append(f"Autostart entry removed: {autostart}")
    return done


def anchor(point: tuple[int, int], side: str, monitor: tuple, size: tuple[int, int]):
    """Where to put the window for a click on its tray icon, like a menu dropping from the icon.
    `point` is what the tray reports: the panel's inner edge at the icon (top panel: the icon's left and bottom;
    bottom: left and top; left: right and top; right: left and top). Kept on the monitor.
    -> (gravity, x, y), (x, y) being the window corner named by the Gdk.Gravity."""
    x, y = point
    w, h = size
    mx, my, mw, mh = monitor

    def clamp(v, lo, hi):
        return max(lo, min(v, hi))

    if side == "top":
        return "NORTH_WEST", clamp(x, mx, mx + mw - w), y
    if side == "bottom":
        return "SOUTH_WEST", clamp(x, mx, mx + mw - w), y
    if side == "left":
        return "NORTH_WEST", x, clamp(y, my, my + mh - h)
    return "NORTH_EAST", x, clamp(y, my, my + mh - h)


def place(pointer: tuple[int, int], geometry: tuple, workarea: tuple, size: tuple[int, int]):
    """Where to put the window when it's launched with the pointer at `pointer` (on a panel launcher's icon):
    just inside the work area, against the panel the pointer is on, centred on the pointer.
    Rectangles are (x, y, width, height); size is the window's outer size.
    -> (gravity, x, y), where (x, y) is the window's corner named by the Gdk.Gravity; or None to leave the window
    where it is (the pointer is inside the work area: launched from a menu or a terminal).
    """
    px, py = pointer
    w, h = size
    ax, ay, aw, ah = workarea
    if tuple(workarea) == tuple(geometry):
        # No work area for this monitor (GTK knows it only for the primary one): if the pointer is near an edge,
        # take it as a panel there, as thick as twice the pointer's distance (launcher icons sit mid-panel).
        # Top and bottom panels (the usual layout) win in a corner: an icon at the end of a top panel is nearer
        # the side edge than the top.
        gx, gy, gw, gh = geometry
        distance = {"top": py - gy, "bottom": gy + gh - 1 - py, "left": px - gx, "right": gx + gw - 1 - px}
        edge = next((e for e in ("top", "bottom", "left", "right") if distance[e] < EDGE), None)
        if edge is None:
            return None
        t = 2 * distance[edge] + 1
        ax, ay, aw, ah = {"top": (gx, gy + t, gw, gh - t), "bottom": (gx, gy, gw, gh - t),
                          "left": (gx + t, gy, gw - t, gh), "right": (gx, gy, gw - t, gh)}[edge]

    def clamp(v, lo, hi):
        return max(lo, min(v, hi))

    if py < ay:
        return "NORTH_WEST", clamp(px - w // 2, ax, ax + aw - w), ay
    if py >= ay + ah:
        return "SOUTH_WEST", clamp(px - w // 2, ax, ax + aw - w), ay + ah
    if px < ax:
        return "NORTH_WEST", ax, clamp(py - h // 2, ay, ay + ah - h)
    if px >= ax + aw:
        return "NORTH_EAST", ax + aw, clamp(py - h // 2, ay, ay + ah - h)
    return None


def load_gtk():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError) as e:
        raise lpcp.LpcpError(f"the panel needs GTK 3 for Python ({e}). On Debian/Ubuntu: "
                             "sudo apt install python3-gi gir1.2-gtk-3.0") from e
    return Gdk, Gio, GLib, Gtk


def load_xapp():
    """XApp's StatusIcon, or None (not Linux Mint, or no tray on the panel) with the reason."""
    try:
        import gi
        gi.require_version("XApp", "1.0")
        from gi.repository import XApp
    except (ImportError, ValueError):
        return None, "the tray icon needs XApp (Linux Mint: gir1.2-xapp-1.0)"
    # At login this can start before the panel's tray applet is up: give it a while instead of giving up.
    deadline = time.monotonic() + TRAY_WAIT
    while not XApp.StatusIcon.any_monitors():
        if time.monotonic() >= deadline:
            return None, "no XApp tray on the panel (Linux Mint: add the 'XApp Status Applet' to a panel)"
        time.sleep(0.5)
    return XApp, None


def main(args) -> int:
    tray = getattr(args, "tray", False)
    if getattr(args, "install", False):
        print("\n".join(install_launcher(tray)))
        if tray:
            print("Start it now with: ./dev ui --tray")
        else:
            print("To put it on the MATE panel: open the Applications menu > Programming, right-click LPCP, and "
                  "choose 'Add this launcher to panel'.")
        return 0
    Gdk, Gio, GLib, Gtk = load_gtk()
    XApp = None
    if tray:
        XApp, why = load_xapp()
        if XApp is None:
            print(f"lpcp: {why}; opening the window instead", file=sys.stderr)
    GLib.set_prgname(WM_CLASS)
    Gdk.set_program_class(WM_CLASS)
    build_classes(Gdk, Gio, GLib, Gtk)
    flags = Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("LPCP_HOME") else Gio.ApplicationFlags.FLAGS_NONE
    app = Gtk.Application(application_id=APP_ID, flags=flags)
    app.panel = None

    def activate(application):
        # Runs on every launch: a second launch (any mode) hands over to this one, so each click on a panel
        # launcher brings the window to the launcher, or hides it when it's in front.
        if application.panel is None:
            application.panel = Panel(application, XApp)
            if XApp is not None:
                application.hold()      # stay alive with the window hidden: the tray icon is the way back
                return
            application.panel.move_to_launcher()
            application.panel.reveal(Gdk.CURRENT_TIME)
            return
        application.panel.launcher_click()

    app.connect("activate", activate)
    return app.run([sys.argv[0]])


# The widget classes subclass GTK types, so they are defined once GTK has loaded.
Panel = None


def build_classes(Gdk, Gio, GLib, Gtk) -> None:
    global Panel

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    class Dot(Gtk.Button):
        """The status dot. Pulses while its project is starting or stopping."""

        def __init__(self, panel):
            super().__init__()
            self.panel, self.state, self.busy = panel, "off", None
            self.set_relief(Gtk.ReliefStyle.NONE)
            self.get_style_context().add_class("lpcp-dot")
            area = Gtk.DrawingArea()
            area.set_size_request(16, 16)
            area.connect("draw", self.draw)
            self.add(area)
            self.connect("enter-notify-event", lambda w, e: self.cursor("pointer"))
            self.connect("leave-notify-event", lambda w, e: self.cursor(None))

        def cursor(self, name):
            window = self.get_window()
            if window:
                window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name) if name else None)

        def draw(self, area, cr):
            w, h = area.get_allocated_width(), area.get_allocated_height()
            r, g, b, a = COLORS["green" if self.busy else self.state]
            if self.busy:
                a = 0.3 + 0.7 * (0.5 + 0.5 * math.cos(self.panel.phase))
            cr.set_source_rgba(r, g, b, a)
            cr.arc(w / 2, h / 2, min(w, h) / 2 - 1, 0, 2 * math.pi)
            cr.fill()

    class LaunchIcon(Gtk.Button):
        """A launcher: a round icon whose colour and glyph say what it opens (its class)."""

        def __init__(self, launcher):
            super().__init__()
            self.kind, self.hover = launcher.kind, False
            self.set_relief(Gtk.ReliefStyle.NONE)
            self.set_focus_on_click(False)
            self.get_style_context().add_class("lpcp-launch")
            area = Gtk.DrawingArea()
            area.set_size_request(ICON_SIZE, ICON_SIZE)
            area.connect("draw", self.draw)
            self.add(area)
            self.connect("enter-notify-event", lambda w, e: self.hovered(True))
            self.connect("leave-notify-event", lambda w, e: self.hovered(False))

        def hovered(self, hover: bool) -> None:
            self.hover = hover and self.get_sensitive()
            window = self.get_window()
            if window:
                window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "pointer") if self.hover else None)
            self.queue_draw()

        def draw(self, area, cr):
            paint_launcher(cr, self.kind, min(area.get_allocated_width(), area.get_allocated_height()),
                           alpha=1.0 if self.get_sensitive() else 0.3, hover=self.hover and self.get_sensitive())

    class ProjectRow(Gtk.ListBoxRow):
        def __init__(self, panel, name):
            super().__init__()
            self.set_activatable(False)
            self.set_selectable(False)
            self.dot = Dot(panel)
            self.dot.connect("clicked", lambda button: panel.toggle(name))
            self.label = Gtk.Label(label=name, xalign=0)
            self.label.set_ellipsize(3)        # Pango.EllipsizeMode.END
            self.label.get_style_context().add_class("lpcp-name")
            self.cpu = Gtk.Label(xalign=1, width_chars=7)
            self.mem = Gtk.Label(xalign=1, width_chars=8)
            for metric in (self.cpu, self.mem):
                metric.get_style_context().add_class("lpcp-metric")
            self.panel, self.name = panel, name
            self.launch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            self.launch_buttons: list = []
            self.shown_launchers: tuple = ()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_start(4)
            box.set_margin_end(8)
            box.pack_start(self.dot, False, False, 0)
            box.pack_start(self.label, True, True, 0)
            box.pack_start(self.launch_box, False, False, 0)     # left of the stats
            box.pack_start(self.cpu, False, False, 0)
            box.pack_start(self.mem, False, False, 0)
            self.add(box)

        def set_launchers(self, launchers: list, running: bool, error: str | None) -> None:
            """One round icon per launcher, the default first; they work while the project runs."""
            if tuple(launchers) != self.shown_launchers:
                for button in self.launch_buttons:
                    self.launch_box.remove(button)
                self.launch_buttons = []
                for launcher in launchers:
                    button = LaunchIcon(launcher)
                    button.connect("clicked", lambda b, lch=launcher: self.panel.launch(self.name, lch))
                    self.launch_box.pack_start(button, False, False, 0)
                    self.launch_buttons.append(button)
                self.launch_box.show_all()
                self.shown_launchers = tuple(launchers)
            for button, launcher in zip(self.launch_buttons, launchers):
                usable = running or launcher.viewer     # logs can be read while the project is stopped
                button.set_sensitive(usable)
                tip = launcher.title if usable else f"{launcher.title}\n(start the project first)"
                if error and launcher.builtin:
                    tip += f"\nIts own launchers can't be read: {error}"
                button.set_tooltip_text(tip)

        def update(self, row: mon.Row, busy: str | None, stats: bool) -> None:
            self.set_launchers(row.launchers, row.state == "green" and not busy, row.launcher_error)
            self.dot.state, self.dot.busy = row.state, busy
            self.dot.queue_draw()
            if busy:
                self.dot.set_tooltip_text("Starting..." if busy == "up" else "Stopping...")
            else:
                self.dot.set_tooltip_text("Click to start" if row.action == "up" else "Click to stop")
            self.label.set_tooltip_text("\n".join(x for x in (row.why, row.url, row.address and
                                                              f"address {row.address}") if x))
            if row.containers:
                across = f"across its {row.containers} running container{'s' if row.containers != 1 else ''}"
                self.cpu.set_tooltip_text(f"CPU use, added up {across}.\n100% = one full CPU core, so a busy "
                                          "project can go above 100%.")
                self.mem.set_tooltip_text(f"Memory (RAM) in use, added up {across}.\n"
                                          "GB = 1,000,000,000 bytes.")
            else:
                self.cpu.set_tooltip_text("CPU use: nothing of this project is running")
                self.mem.set_tooltip_text("Memory (RAM) in use: nothing of this project is running")
            if stats or row.cpu is None:
                for label, text in ((self.cpu, None if row.cpu is None else f"{row.cpu:.1f}%"),
                                    (self.mem, None if row.mem_gb is None else f"{row.mem_gb:.1f} GB")):
                    label.set_text(text or "-")
                    context = label.get_style_context()
                    (context.remove_class if text else context.add_class)("lpcp-dim")

    class Tray:
        """The tray icon (XApp): left click shows or hides the window, right click opens a small menu."""

        def __init__(self, panel, XApp):
            self.panel, self.XApp = panel, XApp
            self.icon = XApp.StatusIcon.new_with_name(WM_CLASS)
            self.icon.set_icon_name(str(ICON))           # XApp takes an icon name or a file path
            self.icon.set_tooltip_text("LPCP: click to show or hide")
            self.icon.connect("button-release-event", self.on_release)
            self.icon.connect("state-changed", self.on_state)
            menu = Gtk.Menu()
            for label, handler in (("Open", lambda item: panel.reveal(Gdk.CURRENT_TIME)),
                                   ("Quit LPCP", lambda item: panel.get_application().quit())):
                item = Gtk.MenuItem(label=label)
                item.connect("activate", handler)
                menu.append(item)
            menu.show_all()
            self.icon.set_secondary_menu(menu)
            self.icon.set_visible(True)

        def on_release(self, icon, x, y, button, timestamp, panel_position):
            if button == 1:
                self.panel.tray_click(x, y, SIDES.get(panel_position, "top"), timestamp)

        def on_state(self, icon, state):
            if state == self.XApp.StatusIconState.NO_SUPPORT:   # the tray went away: don't leave the window lost
                self.panel.tray_lost()

    class PanelWindow(Gtk.ApplicationWindow):
        def __init__(self, app, XApp=None):
            super().__init__(application=app, title="Lan Projects Control Panel")
            self.set_default_size(WIDTH, -1)
            self.set_border_width(6)
            try:
                self.set_icon_from_file(str(ICON))
            except GLib.Error:
                self.set_icon_name("applications-system")
            self.phase, self.pulsing, self.closed = 0.0, False, False
            self.current: dict[str, mon.Row] = {}
            self.rows: dict[str, ProjectRow] = {}
            self.reported: dict[str, str] = {}      # failures already sent as a notification
            self.actions = mon.Actions(on_change=lambda: GLib.idle_add(self.action_done))
            self.monitor = mon.Monitor(self.actions.failures)

            self.listbox = Gtk.ListBox()
            self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            # A note under the list, only while there is something to say that the rows can't: the first read, or
            # an unreadable registry / unreachable docker. It's hidden the rest of the time.
            self.note = Gtk.Label(xalign=0, wrap=True, selectable=True)
            self.note.set_line_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR (long paths)
            # Wrap at the window's width instead of widening it. The width request also makes the window's height
            # fit the wrapped text (otherwise GTK sizes it for the label at its narrowest, leaving a gap).
            self.note.set_max_width_chars(1)
            self.note.set_size_request(WIDTH - 12, -1)
            self.note.get_style_context().add_class("lpcp-note")
            self.note.set_no_show_all(True)
            self.set_note("Reading docker...")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.pack_start(self.listbox, False, False, 0)
            box.pack_start(self.note, False, False, 0)
            self.add(box)
            self.connect("destroy", self.on_destroy)
            self.connect("delete-event", self.on_close)
            self.focus_lost_at = 0.0
            self.panicking = False
            self.connect("focus-out-event", self.on_focus_out)

            self.wake = threading.Event()
            self.shown = threading.Event()      # cleared while hidden in the tray: no docker calls then
            self.tray = Tray(self, XApp) if XApp is not None else None
            if self.tray is None:
                self.shown.set()
            threading.Thread(target=self.poll, daemon=True).start()

        # ---- showing and hiding

        def reveal(self, timestamp) -> None:
            self.show_all()
            self.present_with_time(timestamp)
            if not self.shown.is_set():
                self.shown.set()
                self.wake.set()                 # read docker straight away

        def conceal(self) -> None:
            self.hide()
            self.shown.clear()

        def in_front(self) -> bool:
            return self.get_visible() and (self.is_active() or time.monotonic() - self.focus_lost_at < FOCUS_GRACE)

        def tray_click(self, x: int, y: int, side: str, timestamp: int) -> None:
            # The tray doesn't pass key state, so read it now: Ctrl+Alt is still held as the click arrives.
            state = Gdk.Keymap.get_for_display(self.get_display()).get_modifier_state()
            panic_keys = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
            if state & panic_keys == panic_keys:
                self.panic()
                return
            if self.in_front():
                self.conceal()
                return
            monitor = self.get_display().get_monitor_at_point(x, y)
            g = monitor.get_geometry()
            gdk_window = self.get_window()
            if gdk_window is not None and self.get_mapped():
                frame = gdk_window.get_frame_extents()
                size = (frame.width, frame.height)
            else:
                w, h = self.get_size()
                size = (w + 2, h + 32)          # not shown: allow for the window manager's frame
            gravity, wx, wy = anchor((x, y), side, (g.x, g.y, g.width, g.height), size)
            self.set_gravity(getattr(Gdk.Gravity, gravity))
            self.move(wx, wy)
            self.reveal(timestamp)
            self.move(wx, wy)       # again once shown: a window shown before reappears where it was otherwise

        def panic(self) -> None:
            """Ctrl+Alt+click on the tray icon: kill the busiest project (see lpcp_monitor.panic), off the UI
            thread, without showing the window; the result comes as a desktop notification."""
            if self.panicking:
                return
            self.panicking = True
            self.notify("Panic: finding the busiest project...")

            def work():
                try:
                    message = mon.panic(self.monitor)
                except Exception as e:  # never leave the panic button stuck
                    message = f"Panic failed: {e}"
                GLib.idle_add(self.panic_done, message)
            threading.Thread(target=work, daemon=True).start()

        def panic_done(self, message: str) -> bool:
            self.panicking = False
            self.notify(message)
            self.wake.set()
            return False

        def notify(self, text: str, nid: str = "lpcp-panic") -> None:
            note = Gio.Notification.new("LPCP")
            note.set_body(text)
            note.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(str(ICON))))
            self.get_application().send_notification(nid, note)   # same id: replaces the last one

        def launcher_click(self) -> None:
            """Another launch handed over to this window: hide it if it's in front, else bring it to the launcher."""
            if self.get_visible() and self.is_active():
                self.conceal() if self.tray else self.iconify()
                return
            self.move_to_launcher()
            self.reveal(Gdk.CURRENT_TIME)

        def tray_lost(self) -> None:
            if self.tray is not None:
                self.tray = None
                self.get_application().release()    # closing the window quits again
                self.reveal(Gdk.CURRENT_TIME)

        def on_close(self, *_) -> bool:
            if self.tray is not None:
                self.conceal()
                return True                     # keep running in the tray
            return False

        def on_focus_out(self, *_) -> bool:
            self.focus_lost_at = time.monotonic()
            return False

        def move_to_launcher(self) -> None:
            """Put the window by the pointer's panel launcher (see place()); leave it be otherwise."""
            display = self.get_display()
            seat = display.get_default_seat()
            pointer = seat.get_pointer() if seat else None
            if pointer is None:
                return
            _, px, py = pointer.get_position()
            monitor = display.get_monitor_at_point(px, py)
            if monitor is None:
                return
            g, a = monitor.get_geometry(), monitor.get_workarea()
            gdk_window = self.get_window()
            if gdk_window is not None and self.get_mapped():
                frame = gdk_window.get_frame_extents()
                size = (frame.width, frame.height)
            else:
                w, h = self.get_size()
                size = (w + 2, h + 32)      # not shown yet: allow for the window manager's frame
            spot = place((px, py), (g.x, g.y, g.width, g.height), (a.x, a.y, a.width, a.height), size)
            if spot:
                gravity, x, y = spot
                self.set_gravity(getattr(Gdk.Gravity, gravity))
                self.move(x, y)

        # ---- background refresh

        def poll(self) -> None:
            fast = True
            while not self.closed:
                if not self.shown.is_set():
                    self.shown.wait()
                    fast = True
                    if self.closed:
                        break
                try:
                    snapshot = self.monitor.snapshot(stats=not fast)
                except Exception as e:  # keep refreshing: a launcher has nowhere to show a traceback
                    snapshot = mon.Snapshot(rows=list(self.current.values()), error=f"Can't read the projects: {e}")
                GLib.idle_add(self.apply, snapshot)
                # After a quick read (first run, or right after a start/stop), read cpu/mem straight away.
                fast = self.wake.wait(0 if fast else REFRESH)
                self.wake.clear()

        def apply(self, snapshot: mon.Snapshot) -> bool:
            if self.closed:
                return False
            names = [r.name for r in snapshot.rows]
            if names != list(self.rows):
                for child in self.listbox.get_children():
                    self.listbox.remove(child)
                self.rows = {name: ProjectRow(self, name) for name in names}
                for row in self.rows.values():
                    self.listbox.add(row)
                self.listbox.show_all()
                self.resize(max(WIDTH, self.get_size()[0]), 1)   # shrink to fit when rows go away
            self.current = {r.name: r for r in snapshot.rows}
            for r in snapshot.rows:
                self.rows[r.name].update(r, self.actions.busy.get(r.name), snapshot.stats)
            self.set_note(snapshot.error)
            # A failure the monitor has cleared (the project recovered) may notify again if it recurs.
            self.reported = {n: m for n, m in self.reported.items() if self.actions.failures.get(n) == m}
            return False

        # ---- start/stop

        def toggle(self, name: str) -> None:
            row = self.current.get(name)
            if row is None or name in self.actions.busy:
                return
            if self.actions.request(name, row.action):
                self.rows[name].update(row, row.action, False)
                self.start_pulse()

        def launch(self, name: str, launcher) -> None:
            """A launch icon: run its launcher off the UI thread. Success needs no message (the browser opens); a
            failure is a notification."""
            def work():
                if launcher.viewer:
                    ok, message = mon.open_logs(name)
                else:
                    ok, message = mon.launch(name, None if launcher.builtin else launcher.app)
                if not ok:
                    GLib.idle_add(self.notify, message, "lpcp-launch")
            threading.Thread(target=work, daemon=True).start()

        def action_done(self) -> bool:
            """A start or stop finished. The dot says how it went; a failure is also sent as a notification."""
            if self.closed:
                return False
            failures = dict(self.actions.failures)
            for name, message in failures.items():
                if self.reported.get(name) != message:
                    self.notify(f"{name}: {message}", f"lpcp-{name}")
            self.reported = failures
            self.wake.set()
            return False

        def start_pulse(self) -> None:
            if not self.pulsing:
                self.pulsing = True
                GLib.timeout_add(50, self.pulse)

        def pulse(self) -> bool:
            if self.closed:
                return False
            self.phase += 0.25
            busy = self.actions.busy
            for name, row in self.rows.items():
                if row.dot.busy and name not in busy and name in self.current:
                    row.update(self.current[name], None, False)
                if name in busy:
                    row.dot.queue_draw()
            self.pulsing = bool(busy)
            return self.pulsing

        # ---- the note under the list

        def set_note(self, text: str | None) -> None:
            if (text or "") == self.note.get_text() and self.note.get_visible() == bool(text):
                return
            self.note.set_text(text or "")
            self.note.set_visible(bool(text))
            self.resize(self.get_size()[0], 1)      # fit the new text: shrink after a long message

        def on_destroy(self, *_) -> None:
            self.closed = True
            self.wake.set()
            self.shown.set()

    Panel = PanelWindow
