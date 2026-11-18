# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""The log viewer window (GTK 3): one interface for a project's log sources (`./dev config.logs`): a streaming
command or a followed file, whichever the project provides.

  - Filter: lines must match the include expression and not the exclude one (regular expressions, like
    `grep -E` / `grep -v`); "Match case" applies to both and to find.
  - Find (Ctrl+F): highlights every occurrence in what is shown; Enter / F3 next, Shift+F3 previous.
  - Pause (Ctrl+P): freezes the display while the log keeps collecting; resuming shows what arrived.
  - Follow: keeps the newest line in view; scrolling up stops it, scrolling to the end resumes it.
  - Source selector, error / warning colouring, wrap, clear, copy and save of what is shown.

`./dev logview <site>` opens it (so does the logs icon in the panel). The logic is in lpcp_logs.py; closing the
window ends the source's processes. Each project has its own window; opening it again brings it forward.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import lpcp_logs as logs

FLUSH_MS = 60           # how often arrived lines are put on screen
BATCH = 2000            # at most this many lines per flush, so a flood doesn't freeze the window
FILTER_DELAY_MS = 220   # wait for typing to settle before filtering again
CSS = b"""
textview.lpcp-log { font-family: monospace; font-size: 10pt; }
.lpcp-status { opacity: 0.8; }
"""
COLORS = {"error": "#e5534b", "warn": "#d29922", "match_bg": "#fff3a0", "current_bg": "#ff9632", "dark": "#1b1b1b"}


def application_id(project_name: str) -> str:
    """A D-Bus-safe id per project: a second launch for the same project reaches the window already open."""
    return "com.antzcode.lpcp.logs.p_" + re.sub(r"[^A-Za-z0-9]", "_", project_name)


def main(project, sources: list[logs.LogSource], env: dict[str, str], start: str | None = None) -> int:
    import lpcp_ui      # only for loading GTK the same way as the panel
    Gdk, Gio, GLib, Gtk = lpcp_ui.load_gtk()
    window_class = build_window_class(Gdk, GLib, Gtk)
    GLib.set_prgname("lpcp-logs")
    flags = Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("LPCP_HOME") else Gio.ApplicationFlags.FLAGS_NONE
    app = Gtk.Application(application_id=application_id(project.name), flags=flags)
    app.window = None

    def activate(application):
        if application.window is None:
            application.window = window_class(application, project, sources, env, start)
            application.window.show_all()
            application.window.finish_layout()
        application.window.present()

    app.connect("activate", activate)
    for sig in (2, 15):                 # SIGINT, SIGTERM: close the window, which ends the source's processes
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: (app.quit(), False)[1])
    try:
        return app.run([sys.argv[0]])
    finally:
        if app.window is not None:
            app.window.shutdown()


def build_window_class(Gdk, GLib, Gtk):
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    class LogWindow(Gtk.ApplicationWindow):
        def __init__(self, app, project, sources, env, start=None, flush_ms=FLUSH_MS):
            super().__init__(application=app, title=f"{project.name}: logs")
            self.set_default_size(980, 640)
            self.project, self.sources, self.env = project, sources, env
            self.stream: logs.Stream | None = None
            self.source: logs.LogSource | None = None
            self.buffer = logs.LineBuffer()
            self.flt = logs.LineFilter()
            self.shown: list[str] = []          # the lines on screen, one per text line
            self.snapshot: list[str] | None = None      # while paused: the lines at that moment
            self.matches: list[tuple[int, int, int]] = []
            self.current = -1                   # index into matches
            self.programmatic = False           # a scroll the window itself made
            self.closed = False
            self.filter_timer = 0

            self.text = Gtk.TextBuffer()
            self.tags = {
                "error": self.text.create_tag("error", foreground=COLORS["error"]),
                "warn": self.text.create_tag("warn", foreground=COLORS["warn"]),
                "match": self.text.create_tag("match", background=COLORS["match_bg"], foreground=COLORS["dark"]),
                "current": self.text.create_tag("current", background=COLORS["current_bg"],
                                                foreground=COLORS["dark"]),
            }
            self.tags["current"].set_priority(self.text.get_tag_table().get_size() - 1)
            self.view = Gtk.TextView(buffer=self.text, editable=False, cursor_visible=False, monospace=True)
            self.view.get_style_context().add_class("lpcp-log")
            self.view.set_wrap_mode(Gtk.WrapMode.NONE)
            self.scroller = Gtk.ScrolledWindow()
            self.scroller.add(self.view)
            self.adjustment = self.scroller.get_vadjustment()
            self.adjustment.connect("changed", self.on_content_changed)
            self.adjustment.connect("value-changed", self.on_scrolled)

            # ---- filter row
            self.source_box = Gtk.ComboBoxText()
            for src in sources:
                self.source_box.append(src.id, src.title)
            self.source_box.set_tooltip_text("Which log")
            self.include = Gtk.SearchEntry(placeholder_text="Show only lines matching (regex)", hexpand=True)
            self.exclude = Gtk.SearchEntry(placeholder_text="Hide lines matching (regex)", hexpand=True)
            for entry in (self.include, self.exclude):
                entry.set_width_chars(8)        # let them shrink: the buttons beside them must stay visible
            self.include.set_tooltip_text("Show only lines matching this regular expression (like grep -E)")
            self.exclude.set_tooltip_text("Hide lines matching this regular expression (like grep -v)")
            self.match_case = Gtk.CheckButton(label="Match case")
            self.pause = Gtk.ToggleButton(label="Pause")
            self.pause.set_tooltip_text("Freeze the display; the log keeps collecting (Ctrl+P)")
            self.follow = Gtk.ToggleButton(label="Follow", active=True)
            self.follow.set_tooltip_text("Keep the newest line in view")
            self.wrap = Gtk.ToggleButton(label="Wrap")
            clear = Gtk.Button(label="Clear")
            clear.set_tooltip_text("Empty the window (the log itself is untouched)")
            copy = Gtk.Button(label="Copy")
            copy.set_tooltip_text("Copy the lines on screen")
            save = Gtk.Button(label="Save…")
            save.set_tooltip_text("Save the lines on screen to a file")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if len(sources) > 1:
                row.pack_start(self.source_box, False, False, 0)
            for w in (self.include, self.exclude):
                row.pack_start(w, True, True, 0)
            for w in (self.match_case, self.pause, self.follow, self.wrap, clear, copy, save):
                row.pack_start(w, False, False, 0)
            row.set_border_width(6)

            # ---- find bar (Ctrl+F)
            self.find = Gtk.SearchEntry(placeholder_text="Find in the lines shown", hexpand=True)
            self.find_count = Gtk.Label(label="", xalign=0)
            self.find_count.set_width_chars(16)
            prev, nxt = Gtk.Button(label="Previous"), Gtk.Button(label="Next")
            find_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for w, fill in ((self.find, True), (prev, False), (nxt, False), (self.find_count, False)):
                find_box.pack_start(w, fill, fill, 0)
            self.searchbar = Gtk.SearchBar()
            self.searchbar.add(find_box)
            self.searchbar.connect_entry(self.find)
            self.searchbar.set_show_close_button(True)

            self.status = Gtk.Label(xalign=0, selectable=True)
            self.status.get_style_context().add_class("lpcp-status")
            self.status.set_margin_start(6)
            self.status.set_margin_end(6)
            self.status.set_ellipsize(3)        # Pango.EllipsizeMode.END

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.pack_start(row, False, False, 0)
            box.pack_start(self.searchbar, False, False, 0)
            box.pack_start(self.scroller, True, True, 0)
            box.pack_start(self.status, False, False, 4)
            self.add(box)

            self.source_box.connect("changed", lambda w: self.open_source(w.get_active_id()))
            for entry in (self.include, self.exclude):
                entry.connect("search-changed", lambda e: self.schedule_filter())
            self.match_case.connect("toggled", lambda w: (self.schedule_filter(), self.refind()))
            self.pause.connect("toggled", lambda w: self.set_paused(w.get_active()))
            self.follow.connect("toggled", self.on_follow_toggled)
            self.wrap.connect("toggled", lambda w: self.view.set_wrap_mode(
                Gtk.WrapMode.WORD_CHAR if w.get_active() else Gtk.WrapMode.NONE))
            clear.connect("clicked", lambda w: self.clear())
            copy.connect("clicked", lambda w: self.copy())
            save.connect("clicked", lambda w: self.save_dialog())
            self.find.connect("search-changed", lambda e: self.refind())
            self.find.connect("activate", lambda e: self.step(1))
            nxt.connect("clicked", lambda w: self.step(1))
            prev.connect("clicked", lambda w: self.step(-1))
            self.connect("key-press-event", self.on_key)
            self.connect("destroy", lambda w: self.shutdown())

            self.flush_ms = flush_ms
            self.timer = GLib.timeout_add(flush_ms, self.tick)
            first = start if start in {s.id for s in sources} else sources[0].id
            self.source_box.set_active_id(first)
            self.open_source(first)

        def finish_layout(self):
            self.include.grab_focus()

        # ---- sources

        def open_source(self, source_id) -> None:
            src = next((s for s in self.sources if s.id == source_id), None)
            if src is None or (self.source is src and self.stream is not None):
                return
            self.stop_stream()
            self.source = src
            self.buffer.clear()
            self.snapshot = list(self.buffer.lines) if self.pause.get_active() else None
            self.reset_view()
            self.stream = logs.Stream(src, self.project.dir, self.env)
            if self.stream.error:
                self.status.set_text(f"{src.id}: {self.stream.error}")

        def stop_stream(self) -> None:
            if self.stream is not None:
                self.stream.stop()
                self.stream = None

        def shutdown(self) -> None:
            if not self.closed:
                self.closed = True
                if self.timer:
                    GLib.source_remove(self.timer)
                    self.timer = 0
                self.stop_stream()

        # ---- the lines on screen

        def tick(self) -> bool:
            if self.closed:
                return False
            if self.stream is not None:
                lines = self.stream.drain(BATCH)
                if lines:
                    self.buffer.extend(lines)
                    if self.snapshot is None:
                        self.append(lines)
            self.update_status()
            return True

        def source_lines(self) -> list[str]:
            return self.snapshot if self.snapshot is not None else list(self.buffer.lines)

        def insert(self, lines: list[str]) -> None:
            """Put lines at the end of the text, coloured by level; consecutive lines of one level go in one call."""
            levels = [logs.level(line) for line in lines]
            i = 0
            while i < len(lines):
                j = i + 1
                while j < len(lines) and levels[j] == levels[i]:
                    j += 1
                chunk = "\n".join(lines[i:j]) + "\n"
                if levels[i]:
                    self.text.insert_with_tags(self.text.get_end_iter(), chunk, self.tags[levels[i]])
                else:
                    self.text.insert(self.text.get_end_iter(), chunk)
                i = j

        def append(self, lines: list[str]) -> None:
            visible = [line for line in lines if self.flt(line)]
            if not visible:
                return
            first_new = len(self.shown)
            self.insert(visible)
            self.shown.extend(visible)
            extra = len(self.shown) - logs.MAX_LINES
            if extra > 0:
                self.text.delete(self.text.get_start_iter(), self.text.get_iter_at_line(extra))
                del self.shown[:extra]
                self.refind()
            elif self.find.get_text():
                self.find_in(first_new)

        def reset_view(self) -> None:
            """Show the (filtered) source lines again from scratch."""
            self.programmatic = True        # emptying and refilling moves the scrollbar: not the user scrolling
            try:
                self.text.set_text("")
                self.shown = []
                self.matches, self.current = [], -1
                self.append(self.source_lines())
                self.refind()
            finally:
                self.programmatic = False
            if self.follow.get_active():
                self.scroll_end()

        def schedule_filter(self) -> None:
            if self.filter_timer:
                GLib.source_remove(self.filter_timer)
            self.filter_timer = GLib.timeout_add(FILTER_DELAY_MS, self.apply_filter)

        def apply_filter(self) -> bool:
            self.filter_timer = 0
            ignore = not self.match_case.get_active()
            try:
                flt = logs.LineFilter(self.include.get_text(), self.exclude.get_text(), ignore)
                self.mark_invalid(self.include, None)
                self.mark_invalid(self.exclude, None)
            except logs.LogError as e:
                # keep showing with the last good filter; point at the field that is wrong
                bad = self.include if str(e).startswith("include") else self.exclude
                self.mark_invalid(bad, str(e))
                return False
            self.flt = flt
            self.reset_view()
            return False

        def mark_invalid(self, entry, message) -> None:
            ctx = entry.get_style_context()
            if message:
                ctx.add_class(Gtk.STYLE_CLASS_ERROR)
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "dialog-error-symbolic")
                entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, message)
            else:
                ctx.remove_class(Gtk.STYLE_CLASS_ERROR)
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)

        def clear(self) -> None:
            self.buffer.clear()
            if self.snapshot is not None:
                self.snapshot = []
            self.reset_view()

        # ---- pause and follow

        def set_paused(self, paused: bool) -> None:
            if paused:
                self.snapshot = list(self.buffer.lines)
                self.follow.set_active(False)
            else:
                self.snapshot = None
                self.reset_view()
                self.follow.set_active(True)
            self.update_status()

        def on_follow_toggled(self, button) -> None:
            if button.get_active() and not self.programmatic:
                self.scroll_end()

        def scroll_end(self) -> None:
            self.programmatic = True
            self.adjustment.set_value(self.adjustment.get_upper() - self.adjustment.get_page_size())
            self.programmatic = False

        def on_content_changed(self, adjustment) -> None:
            if self.follow.get_active():
                self.scroll_end()

        def on_scrolled(self, adjustment) -> None:
            if self.programmatic:
                return
            at_end = adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 2
            if at_end != self.follow.get_active() and self.snapshot is None:
                self.follow.set_active(at_end)

        # ---- find

        def find_in(self, first_line: int) -> None:
            """Mark matches in the lines from `first_line` on (new lines arrived)."""
            try:
                found = logs.find_matches(self.shown[first_line:], self.find.get_text(),
                                          not self.match_case.get_active())
            except logs.LogError:
                return
            room = logs.MAX_MATCHES - len(self.matches)
            for line, start, end in found[:max(room, 0)]:
                self.matches.append((line + first_line, start, end))
                self.text.apply_tag(self.tags["match"], *self.iters(line + first_line, start, end))
            self.show_find_count()

        def iters(self, line, start, end):
            return (self.text.get_iter_at_line_offset(line, start), self.text.get_iter_at_line_offset(line, end))

        def refind(self) -> None:
            begin, end = self.text.get_bounds()
            self.text.remove_tag(self.tags["match"], begin, end)
            self.text.remove_tag(self.tags["current"], begin, end)
            self.matches, self.current = [], -1
            pattern = self.find.get_text()
            if not pattern:
                self.show_find_count()
                return
            try:
                self.matches = logs.find_matches(self.shown, pattern, not self.match_case.get_active())
            except logs.LogError as e:
                self.find_count.set_text("bad pattern")
                self.find_count.set_tooltip_text(str(e))
                return
            self.find_count.set_tooltip_text(None)
            for line, start, stop in self.matches:
                self.text.apply_tag(self.tags["match"], *self.iters(line, start, stop))
            if self.matches:
                self.select(0)
            self.show_find_count()

        def select(self, index: int) -> None:
            if not self.matches:
                return
            if 0 <= self.current < len(self.matches):
                self.text.remove_tag(self.tags["current"], *self.iters(*self.matches[self.current]))
            self.current = index % len(self.matches)
            a, b = self.iters(*self.matches[self.current])
            self.text.apply_tag(self.tags["current"], a, b)
            self.follow.set_active(False)           # the user is reading, not tailing
            self.view.scroll_to_iter(a, 0.1, True, 0.0, 0.5)
            self.show_find_count()

        def step(self, direction: int) -> None:
            if self.matches:
                self.select((self.current if self.current >= 0 else -1 if direction > 0 else 0) + direction)

        def show_find_count(self) -> None:
            if not self.find.get_text():
                self.find_count.set_text("")
            elif not self.matches:
                self.find_count.set_text("no matches")
            else:
                capped = "+" if len(self.matches) >= logs.MAX_MATCHES else ""
                self.find_count.set_text(f"{self.current + 1} of {len(self.matches)}{capped}")

        # ---- keys, status, copy and save

        def on_key(self, widget, event) -> bool:
            ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
            key = Gdk.keyval_name(event.keyval) or ""
            if ctrl and key.lower() == "f":
                self.searchbar.set_search_mode(True)
                self.find.grab_focus()
                return True
            if ctrl and key.lower() == "p":
                self.pause.set_active(not self.pause.get_active())
                return True
            if key == "F3" or (ctrl and key.lower() == "g"):
                self.step(-1 if shift else 1)
                return True
            if key == "Escape" and self.searchbar.get_search_mode():
                self.searchbar.set_search_mode(False)
                return True
            return False

        def update_status(self) -> None:
            stream, src = self.stream, self.source
            if src is None or (stream is not None and stream.error):
                return
            held = len(self.buffer) - len(self.snapshot) if self.snapshot is not None else 0
            parts = [f"{len(self.shown)} shown of {len(self.buffer)}"]
            if self.buffer.dropped:
                parts.append(f"{self.buffer.dropped} older dropped")
            if self.snapshot is not None:
                parts.append(f"PAUSED: {max(held, 0)} new")
            if stream is not None and stream.ended.is_set():
                rc = stream.returncode
                parts.append("ended" if not rc else f"ended (exit {rc})")
            else:
                parts.append("live")
            parts.append(f"{src.cmd}" if src.cmd else f"{src.file}")
            self.status.set_text("  ·  ".join(parts))

        def shown_text(self) -> str:
            return "\n".join(self.shown) + ("\n" if self.shown else "")

        def copy(self) -> None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.shown_text(), -1)

        def write_shown(self, path) -> None:
            Path(path).write_text(self.shown_text(), encoding="utf-8")

        def save_dialog(self) -> None:
            dialog = Gtk.FileChooserDialog(title="Save the lines shown", parent=self,
                                           action=Gtk.FileChooserAction.SAVE)
            dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
            dialog.set_do_overwrite_confirmation(True)
            dialog.set_current_name(f"{self.project.name}-{self.source.id}.log")
            if dialog.run() == Gtk.ResponseType.OK:
                try:
                    self.write_shown(dialog.get_filename())
                except OSError as e:
                    self.status.set_text(f"could not save: {e}")
            dialog.destroy()

    return LogWindow
