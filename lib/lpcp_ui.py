# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""The control panel window (GTK 3): one row per registered project with a status dot (click to start or
stop it), its name, and its CPU and memory use. `./dev ui` opens it; `./dev ui --install` adds a launcher to
the desktop's application menu. Closing the window leaves every project as it is.

GTK is only needed here: the command line stays standard-library only.
"""
from __future__ import annotations

import math
import os
import sys
import threading
from pathlib import Path

import lpcp
import lpcp_monitor as mon

REPO = Path(__file__).resolve().parent.parent
ICON = REPO / "share" / "lpcp.svg"
APP_ID = "com.antzcode.lpcp"
WM_CLASS = "lpcp"
WIDTH = 450
REFRESH = 2.0           # seconds between refreshes; reading docker stats adds about 2 more
COLORS = {"green": (0.18, 0.74, 0.31, 1.0), "red": (0.89, 0.22, 0.21, 1.0), "off": (0.0, 0.0, 0.0, 0.25)}
CSS = b"""
.lpcp-dot, .lpcp-dot:hover, .lpcp-dot:active, .lpcp-dot:focus {
  padding: 4px; min-width: 0; min-height: 0;
  border-color: transparent; background: none; box-shadow: none;
}
.lpcp-name { font-weight: bold; }
.lpcp-metric { font-feature-settings: "tnum"; }
.lpcp-dim { opacity: 0.45; }
.lpcp-status { opacity: 0.75; }
"""


def desktop_entry() -> str:
    command = f'"{REPO / "dev"}" ui'
    if os.environ.get("LPCP_HOME"):
        command = f'env "LPCP_HOME={lpcp.home()}" {command}'
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=LPCP",
        "GenericName=Docker project control panel",
        "Comment=Start and stop your local docker projects", f"Exec={command}", f"Icon={ICON}",
        "Terminal=false", "Categories=Development;", f"StartupWMClass={WM_CLASS}", "StartupNotify=true", ""])


def install_launcher() -> Path:
    apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    path = apps / "lpcp.desktop"
    path.write_text(desktop_entry(), encoding="utf-8")
    return path


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


def main(args) -> int:
    if getattr(args, "install", False):
        path = install_launcher()
        print(f"Launcher written to {path}\n"
              "To put it on the MATE panel: open the Applications menu > Programming, right-click LPCP, and choose "
              "'Add this launcher to panel'.")
        return 0
    Gdk, Gio, GLib, Gtk = load_gtk()
    GLib.set_prgname(WM_CLASS)
    Gdk.set_program_class(WM_CLASS)
    build_classes(Gdk, GLib, Gtk)
    flags = Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("LPCP_HOME") else Gio.ApplicationFlags.FLAGS_NONE
    app = Gtk.Application(application_id=APP_ID, flags=flags)

    def activate(application):
        window = application.get_active_window()
        if window is None:
            window = Panel(application)
            window.show_all()
        window.present()

    app.connect("activate", activate)
    return app.run([sys.argv[0]])


# The widget classes subclass GTK types, so they are defined once GTK has loaded.
Panel = None


def build_classes(Gdk, GLib, Gtk) -> None:
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
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_start(4)
            box.set_margin_end(8)
            box.pack_start(self.dot, False, False, 0)
            box.pack_start(self.label, True, True, 0)
            box.pack_start(self.cpu, False, False, 0)
            box.pack_start(self.mem, False, False, 0)
            self.add(box)

        def update(self, row: mon.Row, busy: str | None, stats: bool) -> None:
            self.dot.state, self.dot.busy = row.state, busy
            self.dot.queue_draw()
            if busy:
                self.dot.set_tooltip_text("Starting..." if busy == "up" else "Stopping...")
            else:
                self.dot.set_tooltip_text("Click to start" if row.action == "up" else "Click to stop")
            self.label.set_tooltip_text("\n".join(x for x in (row.why, row.url, row.address and
                                                              f"address {row.address}") if x))
            if stats or row.cpu is None:
                for label, text in ((self.cpu, None if row.cpu is None else f"{row.cpu:.1f}%"),
                                    (self.mem, None if row.mem_gb is None else f"{row.mem_gb:.1f} GB")):
                    label.set_text(text or "-")
                    context = label.get_style_context()
                    (context.remove_class if text else context.add_class)("lpcp-dim")

    class PanelWindow(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="LPCP")
            self.set_default_size(WIDTH, -1)
            self.set_border_width(6)
            try:
                self.set_icon_from_file(str(ICON))
            except GLib.Error:
                self.set_icon_name("applications-system")
            self.phase, self.pulsing, self.closed = 0.0, False, False
            self.current: dict[str, mon.Row] = {}
            self.rows: dict[str, ProjectRow] = {}
            self.error: str | None = None
            self.message: str | None = None
            self.actions = mon.Actions(on_change=lambda: GLib.idle_add(self.action_done))
            self.monitor = mon.Monitor(self.actions.failures)

            self.listbox = Gtk.ListBox()
            self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
            self.status.set_line_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR (long log paths)
            # Wrap at the window's width instead of widening it. The width request also makes the window's height
            # fit the wrapped text (otherwise GTK sizes it for the label at its narrowest, leaving a gap).
            self.status.set_max_width_chars(1)
            self.status.set_size_request(WIDTH - 12, -1)
            self.status.get_style_context().add_class("lpcp-status")
            self.status.set_no_show_all(True)
            self.show_status("Reading docker...")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.pack_start(self.listbox, False, False, 0)
            box.pack_start(self.status, False, False, 0)
            self.add(box)
            self.connect("destroy", self.on_destroy)

            self.wake = threading.Event()
            threading.Thread(target=self.poll, daemon=True).start()

        # ---- background refresh

        def poll(self) -> None:
            fast = True
            while not self.closed:
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
            self.error = snapshot.error
            for r in snapshot.rows:
                self.rows[r.name].update(r, self.actions.busy.get(r.name), snapshot.stats)
            self.refresh_status()
            return False

        # ---- start/stop

        def toggle(self, name: str) -> None:
            row = self.current.get(name)
            if row is None or name in self.actions.busy:
                return
            if self.actions.request(name, row.action):
                self.message = f"{name}: {'starting' if row.action == 'up' else 'stopping'}..."
                self.rows[name].update(row, row.action, False)
                self.refresh_status()
                self.start_pulse()

        def action_done(self) -> bool:
            if self.closed:
                return False
            if self.actions.messages:
                self.message = self.actions.messages[-1]
            self.refresh_status()
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

        # ---- status line

        def show_status(self, text: str | None) -> None:
            if (text or "") == self.status.get_text() and self.status.get_visible() == bool(text):
                return
            self.status.set_text(text or "")
            self.status.set_visible(bool(text))
            self.resize(self.get_size()[0], 1)      # fit the new text: shrink after a long message

        def refresh_status(self) -> None:
            self.show_status(self.error or self.message)

        def on_destroy(self, *_) -> None:
            self.closed = True
            self.wake.set()

    Panel = PanelWindow
