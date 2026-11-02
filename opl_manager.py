#!/usr/bin/env python3
"""OPL Manager para Linux — gestor de la biblioteca de Open PS2 Loader (GTK 4 + libadwaita)."""
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oplcore as core  # noqa: E402
import tray  # noqa: E402
import updater  # noqa: E402

APP_ID = "io.github.camiloferreira.OplManager"
COVER_W, COVER_H = 140, 200

CSS = b"""
.game-card { padding: 6px; border-radius: 12px; }
.game-card:hover { background: alpha(currentColor, 0.07); }
.cover { border-radius: 6px; background: alpha(currentColor, 0.08); }
.cover-missing { border-radius: 6px; background: alpha(currentColor, 0.08); }
.game-title { font-weight: 600; }
.art-thumb { border-radius: 4px; background: alpha(currentColor, 0.06); }
"""


def tilde(path):
    """Ruta para mostrar: la carpeta personal se abrevia como ~."""
    home = os.path.expanduser("~").rstrip("/")
    if path and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path or ""


def run_bg(fn, on_done=None, on_error=None):
    """Ejecuta fn en un hilo; on_done(resultado) / on_error(excepción) en el hilo de la UI."""
    def worker():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if on_error:
                GLib.idle_add(on_error, e)
            return
        if on_done:
            GLib.idle_add(on_done, res)
    threading.Thread(target=worker, daemon=True).start()


def cover_widget(path, w=COVER_W, h=COVER_H, label=""):
    if path and os.path.exists(path):
        try:
            # se carga ya reducida: tamaño fijo en la grilla y menos memoria
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, False)
            pic = Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(pb))
        except GLib.Error:
            pic = None
        if pic:
            pic.set_content_fit(Gtk.ContentFit.FILL)
            pic.set_can_shrink(True)
            pic.set_size_request(w, h)
            pic.set_halign(Gtk.Align.CENTER)
            pic.set_valign(Gtk.Align.START)
            pic.add_css_class("cover")
            frame = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, overflow=Gtk.Overflow.HIDDEN,
                            width_request=w, height_request=h)
            frame.append(pic)
            return frame
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
    box.set_size_request(w, h)
    box.add_css_class("cover-missing")
    img = Gtk.Image.new_from_icon_name("media-optical-symbolic")
    img.set_pixel_size(min(w, h) // 3)
    img.set_vexpand(True)
    img.add_css_class("dim-label")
    box.append(img)
    if label:
        lb = Gtk.Label(label=label, margin_bottom=8)
        lb.add_css_class("caption")
        lb.add_css_class("dim-label")
        box.append(lb)
    return box


# =============================================================== Utilidades comunes
def add_context_menu(widget, build_menu):
    """Menú con clic derecho (o manteniendo pulsado en pantallas táctiles)."""
    def show(x, y):
        pop = Gtk.PopoverMenu.new_from_model(build_menu())
        pop.set_parent(widget)
        pop.set_has_arrow(False)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pop.set_pointing_to(r)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def pressed(gesture, _n, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        show(x, y)
    click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
    click.connect("pressed", pressed)
    widget.add_controller(click)
    hold = Gtk.GestureLongPress(touch_only=True)
    hold.connect("pressed", lambda _g, x, y: show(x, y))
    widget.add_controller(hold)


def action_group(actions):
    """{nombre: (función, habilitada)} -> Gio.SimpleActionGroup"""
    group = Gio.SimpleActionGroup()
    for name, (cb, enabled) in actions.items():
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_a, cb=cb: cb())
        a.set_enabled(enabled)
        group.add_action(a)
    return group


def open_folder(path):
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(folder), None)


def confirm_trash(win, heading, paths, on_yes, extra_body="", extra_child=None):
    """Pide confirmación para mover rutas a la papelera (muestra cuántas y cuánto pesan)."""
    names = [os.path.basename(p.rstrip("/")) + ("/" if os.path.isdir(p) else "") for p in paths]
    total = sum(core.path_size(p) for p in paths)
    body = "\n".join(f"• {n}" for n in names[:8]) + (f"\n… y {len(names) - 8} más" if len(names) > 8 else "")
    body += f"\n\nTotal: {core.human_size(total)}. Van a la papelera: puedes recuperarlos desde Archivos."
    if extra_body:
        body += "\n\n" + extra_body
    ask = Adw.AlertDialog(heading=heading, body=body)
    if extra_child:
        ask.set_extra_child(extra_child)
    ask.add_response("cancel", "Cancelar")
    ask.add_response("trash", "Mover a la papelera")
    ask.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
    ask.set_default_response("cancel")
    ask.set_close_response("cancel")
    ask.connect("response", lambda _d, r: r == "trash" and on_yes())
    ask.present(win)


def trash_paths(win, paths, on_done=None):
    """Mueve a la papelera en segundo plano. Si la unidad no tiene papelera, pregunta antes de borrar."""
    def work():
        moved, unsupported, errors = 0, [], []
        for p in paths:
            try:
                Gio.File.new_for_path(p).trash(None)
                moved += 1
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                    unsupported.append(p)
                elif e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                    moved += 1
                else:
                    errors.append(f"{os.path.basename(p)}: {e.message}")
        return moved, unsupported, errors

    def delete_forever(ps):
        def work2():
            n = 0
            for p in ps:
                try:
                    shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
                    n += 1
                except OSError:
                    pass
            return n
        run_bg(work2, lambda n: (win.toast(f"{n} eliminado(s) definitivamente"), on_done and on_done()))

    def done(res):
        moved, unsupported, errors = res
        if moved:
            win.toast(f"{moved} elemento(s) movidos a la papelera")
        if errors:
            al = Adw.AlertDialog(heading="Algunos no se pudieron mover", body="\n".join(errors[:10]))
            al.add_response("ok", "Aceptar")
            al.present(win)
        if unsupported:
            al = Adw.AlertDialog(
                heading="Esta unidad no tiene papelera",
                body="No se pueden mover a la papelera:\n" + "\n".join(f"• {os.path.basename(p)}" for p in unsupported[:8])
                     + "\n\n¿Eliminarlos definitivamente? No se podrán recuperar.")
            al.add_response("cancel", "Cancelar")
            al.add_response("delete", "Eliminar definitivamente")
            al.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            al.set_default_response("cancel")
            al.set_close_response("cancel")
            al.connect("response", lambda _d, r: delete_forever(unsupported) if r == "delete" else (on_done and on_done()))
            al.present(win)
        elif on_done:
            on_done()
    run_bg(work, done)


def rename_dialog(win, game, on_done=None):
    ml = int(win.cfg.get("max_name", 32) or 0)
    dlg = Adw.AlertDialog(heading="Cambiar nombre")
    entry = Gtk.Entry(text=game.title, activates_default=True)
    hint = Gtk.Label(xalign=0, wrap=True)
    hint.add_css_class("caption")
    hint.add_css_class("dim-label")

    def upd(*_):
        hint.set_text("Quedará como: " + core.opl_filename(game.gid, entry.get_text(), core.ext_of(game.path), ml))
    entry.connect("changed", upd)
    upd()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(entry)
    box.append(hint)
    dlg.set_extra_child(box)
    dlg.add_response("cancel", "Cancelar")
    dlg.add_response("ok", "Cambiar nombre")
    dlg.set_default_response("ok")
    dlg.set_close_response("cancel")
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

    def resp(_d, r):
        if r != "ok" or not entry.get_text().strip():
            return
        try:
            new = core.rename_game(game, entry.get_text().strip(), ml)
        except Exception as e:  # noqa: BLE001
            win.toast(f"No se pudo renombrar: {e}")
            return
        win.toast(f"Renombrado a {os.path.basename(new)}")
        win.library.reload()
        if on_done:
            on_done()
    dlg.connect("response", resp)
    dlg.present(win)
    entry.grab_focus()


def choose_cover(win, game, parent=None, on_done=None):
    d = Gtk.FileDialog(title="Elegir portada")
    f = Gtk.FileFilter(name="Imágenes")
    f.add_mime_type("image/*")
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(f)
    d.set_filters(store)

    def save(path, replace):
        try:
            if replace:
                cur = game.art("COV")
                os.rename(cur, core.free_path(cur + ".bak"))      # nunca se borra: queda de respaldo
            core.save_cover(game.library, game.gid, path)
        except Exception as e:  # noqa: BLE001
            win.toast(f"No se pudo guardar la portada: {e}")
            return
        win.toast("Portada guardada")
        win.library.reload()
        if on_done:
            on_done()

    def chosen(dialog, res):
        try:
            path = dialog.open_finish(res).get_path()
        except GLib.Error:
            return
        if game.art("COV"):
            ask = Adw.AlertDialog(heading="¿Reemplazar la portada?",
                                  body="Este juego ya tiene portada. La actual se guardará como _COV.png.bak.")
            ask.add_response("cancel", "Cancelar")
            ask.add_response("ok", "Reemplazar")
            ask.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            ask.connect("response", lambda _d, r: r == "ok" and save(path, True))
            ask.present(parent or win)
        else:
            save(path, False)
    d.open(win, None, chosen)


def download_game_art(win, game, on_done=None):
    lib = win.cfg["library"]

    def done(got):
        win.toast(f"{len(got)} imágenes nuevas para {game.title}" if got else "No había arte nuevo en la base")
        win.library.reload()
        if on_done:
            on_done()
    run_bg(lambda: core.download_art(lib, game.gid), done, lambda e: win.toast(f"Error: {e}"))


def trash_game_dialog(win, game):
    arts = core.game_art_files(game, win.library.games)
    chk = None
    if arts:
        chk = Gtk.CheckButton(label=f"También mover su arte ({len(arts)} imágenes de ART/)")
    confirm_trash(win, f"¿Mover «{game.title}» a la papelera?", [game.path],
                  lambda: trash_paths(win, [game.path] + (arts if chk and chk.get_active() else []),
                                      win.library.reload),
                  extra_child=chk)


# =============================================================== Biblioteca
class LibraryPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.games = []
        self.filter_text = ""

        # barra: buscador + controles; en ventanas angostas los controles bajan a otra fila
        self.bar = bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=4)
        self.search = Gtk.SearchEntry(placeholder_text="Buscar juego o código…", hexpand=True)
        self.search.connect("search-changed", self._on_search)
        bar.append(self.search)
        controls = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=8,
                               row_spacing=6, min_children_per_line=1, max_children_per_line=6, valign=Gtk.Align.CENTER)
        self.controls = controls
        bar.append(controls)
        bar = controls          # los controles se agregan a la fila que se acomoda sola
        self.kind = Gtk.DropDown.new_from_strings(["Todos", "PS2 (DVD/CD)", "PS1 (POPS)", "Sin portada"])
        self.kind.connect("notify::selected", lambda *_: self.flow.invalidate_filter())
        bar.append(self.kind)
        self.sort = Gtk.DropDown.new_from_strings(core.SORT_NAMES)
        self.sort.set_tooltip_text("Ordenar por")
        self.sort.set_selected(int(win.cfg.get("sort", 0) or 0))
        self.sort.connect("notify::selected", self._on_sort)
        bar.append(self.sort)
        self.dup_btn = Gtk.Button(visible=False, tooltip_text="Juegos repetidos (mismo código)")
        self.dup_btn.add_css_class("destructive-action")
        self.dup_btn.connect("clicked", lambda *_: DuplicatesDialog(self.win, self.dups).present(self.win))
        bar.append(self.dup_btn)
        self.dups = []
        self.art_btn = Gtk.Button(label="Descargar arte faltante", tooltip_text="Baja portadas, lomos, fondos y capturas que falten. No sobrescribe nada.")
        self.art_btn.connect("clicked", self._download_all_art)
        bar.append(self.art_btn)
        self.append(self.bar)

        self.status = Gtk.Label(xalign=0, margin_start=14, margin_bottom=4)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        self.append(self.status)

        self.progress = Gtk.ProgressBar(margin_start=12, margin_end=12, visible=False, show_text=True)
        self.append(self.progress)

        self.flow = Gtk.FlowBox(valign=Gtk.Align.START, selection_mode=Gtk.SelectionMode.NONE,
                                homogeneous=True, min_children_per_line=2, max_children_per_line=20,
                                column_spacing=6, row_spacing=6, margin_start=12, margin_end=12,
                                margin_top=6, margin_bottom=12)
        self.flow.set_filter_func(self._filter)
        self.flow.set_sort_func(lambda a, b: (lambda x, y: (x > y) - (x < y))(
            core.sort_key(a.game, self.sort.get_selected()), core.sort_key(b.game, self.sort.get_selected())))
        self.stack = Gtk.Stack()
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.flow)
        self.stack.add_named(sc, "grid")
        self.empty = Adw.StatusPage(icon_name="media-optical-symbolic", title="Sin juegos",
                                    description="Elige la carpeta de tu biblioteca OPL en Ajustes.")
        find = Gtk.Button(label="Buscar pendrive o disco OPL", halign=Gtk.Align.CENTER)
        find.add_css_class("pill")
        find.add_css_class("suggested-action")
        find.connect("clicked", lambda *_: DriveDialog(self.win).present(self.win))
        self.empty.set_child(find)
        self.stack.add_named(self.empty, "empty")
        self.loading = Adw.StatusPage(title="Cargando biblioteca…")
        sp = Gtk.Spinner(spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER)
        self.loading.set_child(sp)
        self.stack.add_named(self.loading, "loading")
        self.append(self.stack)

    # ---- carga
    def reload(self):
        lib = self.win.cfg["library"]
        self.stack.set_visible_child_name("loading")
        if not os.path.isdir(lib):
            self.empty.set_title("Biblioteca no conectada")
            self.empty.set_description(f"No se encuentra:\n{lib}\n\nConecta tu pendrive o disco de OPL, o búscalo.")
            self.stack.set_visible_child_name("empty")
            self.status.set_text("")
            return
        run_bg(lambda: core.scan_library(lib), self._loaded,
               lambda e: self.win.toast(f"Error al leer la biblioteca: {e}"))

    def _loaded(self, games):
        self.games = games
        while (c := self.flow.get_first_child()):
            self.flow.remove(c)
        for g in games:
            self.flow.append(self._card(g))
        lib = self.win.cfg["library"]
        n_ps2 = sum(g.kind != "POPS" for g in games)
        n_ps1 = sum(g.kind == "POPS" for g in games)
        no_cov = sum(1 for g in games if g.kind != "POPS" and not g.art("COV"))
        self.status.set_text(f"{tilde(lib)}  ·  {n_ps2} PS2  ·  {n_ps1} PS1  ·  {no_cov} sin portada  ·  "
                             f"{core.human_size(core.disk_free(lib))} libres")
        self.dups = core.find_duplicates(games)
        n_extra = sum(len(gs) - 1 for gs in self.dups)
        self.dup_btn.set_label(f"Duplicados ({n_extra})")
        self.dup_btn.set_visible(bool(self.dups))
        self.empty.set_title("Sin juegos")
        self.empty.set_description("No hay juegos en DVD/, CD/ ni POPS/. Cópialos desde la Bandeja de entrada.")
        self.stack.set_visible_child_name("grid" if games else "empty")
        self.flow.invalidate_filter()

    def _card(self, g):
        btn = Gtk.Button(halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        btn.add_css_class("flat")
        btn.add_css_class("game-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(cover_widget(g.art("COV"), label=g.kind))
        t = Gtk.Label(label=g.title, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=16, lines=2,
                      ellipsize=Pango.EllipsizeMode.END, justify=Gtk.Justification.CENTER)
        t.add_css_class("game-title")
        t.set_size_request(COVER_W, -1)
        box.append(t)
        sub = Gtk.Label(label=f"{g.gid or g.kind}")
        sub.add_css_class("caption")
        sub.add_css_class("dim-label")
        box.append(sub)
        btn.set_child(box)
        btn.set_tooltip_text(f"{g.filename}\n{core.human_size(g.size)}")
        btn.connect("clicked", lambda *_: GameDialog(self.win, g).present(self.win))
        win = self.win
        btn.insert_action_group("card", action_group({
            "open": (lambda: GameDialog(win, g).present(win), True),
            "rename": (lambda: rename_dialog(win, g), True),
            "art": (lambda: download_game_art(win, g), bool(g.gid) and g.kind != "POPS"),
            "cover": (lambda: choose_cover(win, g), bool(g.gid)),
            "folder": (lambda: open_folder(g.path), True),
            "copy-name": (lambda: (win.get_clipboard().set(g.filename), win.toast("Nombre copiado")), True),
            "trash": (lambda: trash_game_dialog(win, g), True),
        }))

        def menu():
            m, a, b, c = Gio.Menu(), Gio.Menu(), Gio.Menu(), Gio.Menu()
            a.append("Ver detalles", "card.open")
            a.append("Cambiar nombre…", "card.rename")
            b.append("Descargar arte", "card.art")
            b.append("Cambiar portada…", "card.cover")
            b.append("Abrir carpeta", "card.folder")
            b.append("Copiar nombre del archivo", "card.copy-name")
            c.append("Mover a la papelera…", "card.trash")
            for sec in (a, b, c):
                m.append_section(None, sec)
            return m
        add_context_menu(btn, menu)
        child = Gtk.FlowBoxChild(child=btn)
        child.game = g
        return child

    def _on_sort(self, *_):
        self.win.cfg["sort"] = int(self.sort.get_selected())
        core.save_config(self.win.cfg)
        self.flow.invalidate_sort()

    def _on_search(self, entry):
        self.filter_text = entry.get_text().lower().strip()
        self.flow.invalidate_filter()

    def _filter(self, child):
        g = child.game
        k = self.kind.get_selected()
        if k == 1 and g.kind == "POPS":
            return False
        if k == 2 and g.kind != "POPS":
            return False
        if k == 3 and (g.kind == "POPS" or g.art("COV")):
            return False
        if self.filter_text:
            return self.filter_text in f"{g.title} {g.gid} {g.filename}".lower()
        return True

    # ---- arte de toda la biblioteca
    def _download_all_art(self, *_):
        todo = [g for g in self.games if g.gid and g.kind != "POPS" and g.missing_art()]
        if not todo:
            self.win.toast("Todos los juegos ya tienen su arte")
            return
        lib = self.win.cfg["library"]
        self.art_btn.set_sensitive(False)
        self.progress.set_visible(True)

        def work():
            total = 0
            seen = set()
            for i, g in enumerate(todo):
                GLib.idle_add(self._art_progress, i, len(todo), g.title)
                if g.gid in seen:
                    continue
                seen.add(g.gid)
                total += len(core.download_art(lib, g.gid, g.missing_art()))
            return total

        def done(total):
            self.art_btn.set_sensitive(True)
            self.progress.set_visible(False)
            self.win.toast(f"Se descargaron {total} imágenes")
            self.reload()

        def err(e):
            self.art_btn.set_sensitive(True)
            self.progress.set_visible(False)
            self.win.toast(f"Error al descargar: {e}")
        run_bg(work, done, err)

    def _art_progress(self, i, n, title):
        self.progress.set_fraction(i / max(n, 1))
        self.progress.set_text(f"Arte {i + 1}/{n}: {title}")


# =============================================================== Duplicados
class DuplicatesDialog(Adw.Dialog):
    """Elige qué copia conservar de cada juego repetido; las demás van a la papelera."""

    def __init__(self, win, groups):
        super().__init__(title="Juegos duplicados", content_width=760, content_height=640)
        self.win = win
        self.choices = []      # [(grupo, [CheckButton...])]

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage(description=(
            "Estos juegos tienen el mismo código. Elige cuál conservar de cada uno; "
            "las otras copias se mueven a la papelera (puedes recuperarlas desde Archivos). "
            "Ojo: un parche o mod en español suele tener el mismo código que el original."))
        for gs in groups:
            grp = Adw.PreferencesGroup(title=GLib.markup_escape_text(f"{gs[0].title}"),
                                       description=gs[0].gid)
            leader = None
            checks = []
            for i, g in enumerate(gs):
                when = GLib.DateTime.new_from_unix_local(int(g.mtime)).format("%d/%m/%Y") if g.mtime else "—"
                row = Adw.ActionRow(title=GLib.markup_escape_text(g.filename),
                                    subtitle=f"{g.kind}/  ·  {core.human_size(g.size)}  ·  {when}"
                                             + ("  ·  sugerido" if i == 0 else ""))
                chk = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=(i == 0))
                if leader:
                    chk.set_group(leader)
                else:
                    leader = chk
                chk.connect("toggled", lambda *_: self._update())
                row.add_prefix(chk)
                row.set_activatable_widget(chk)
                checks.append(chk)
                grp.add(row)
            self.choices.append((gs, checks))
            page.add(grp)
        tv.set_content(page)

        bottom = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8,
                         halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", lambda *_: self.close())
        self.go = Gtk.Button()
        self.go.add_css_class("destructive-action")
        self.go.connect("clicked", self._confirm)
        bottom.append(cancel)
        bottom.append(self.go)
        tv.add_bottom_bar(bottom)
        self.set_child(tv)
        self._update()

    def _to_remove(self):
        out = []
        for gs, checks in self.choices:
            out += [g for g, c in zip(gs, checks) if not c.get_active()]
        return out

    def _update(self):
        rm = self._to_remove()
        self.go.set_label(f"Mover {len(rm)} a la papelera ({core.human_size(sum(g.size for g in rm))})")
        self.go.set_sensitive(bool(rm))

    def _confirm(self, *_):
        rm = self._to_remove()
        body = "\n".join(f"• {g.filename}" for g in rm[:15]) + (f"\n… y {len(rm) - 15} más" if len(rm) > 15 else "")
        ask = Adw.AlertDialog(heading=f"¿Mover {len(rm)} archivo(s) a la papelera?", body=body)
        ask.add_response("cancel", "Cancelar")
        ask.add_response("ok", "Mover a la papelera")
        ask.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        ask.set_default_response("cancel")
        ask.connect("response", lambda _d, r: r == "ok" and self._trash(rm))
        ask.present(self)

    def _trash(self, rm):
        self.go.set_sensitive(False)
        self.go.set_label("Moviendo…")

        def work():
            done, errors = 0, []
            for g in rm:
                try:
                    Gio.File.new_for_path(g.path).trash(None)
                    done += 1
                except GLib.Error as e:
                    errors.append(f"{g.filename}: {e.message}")
            return done, errors

        def finished(res):
            done, errors = res
            if errors:
                al = Adw.AlertDialog(heading="Algunos archivos no se movieron",
                                     body="No se borró nada de forma permanente.\n\n" + "\n".join(errors))
                al.add_response("ok", "Aceptar")
                al.present(self.win)
            self.win.toast(f"{done} duplicado(s) movidos a la papelera")
            self.close()
            self.win.reload_all()
        run_bg(work, finished)


# =============================================================== Detalle de un juego
class GameDialog(Adw.Dialog):
    def __init__(self, win, game):
        super().__init__(title=game.title, content_width=640, content_height=620)
        self.win = win
        self.g = game

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=18, margin_end=18,
                       margin_top=6, margin_bottom=18)
        sc.set_child(page)
        tv.set_content(sc)
        self.set_child(tv)

        top = Gtk.Box(spacing=18)
        top.append(cover_widget(game.art("COV"), 168, 240, game.kind))
        info = Adw.PreferencesGroup(hexpand=True)
        for title, val in (("Código", game.gid or "—"), ("Carpeta", game.kind),
                           ("Tamaño", core.human_size(game.size)), ("Archivo", game.filename)):
            row = Adw.ActionRow(title=title, subtitle=val, subtitle_selectable=True)
            row.add_css_class("property")
            info.add(row)
        top.append(info)
        page.append(top)

        grp = Adw.PreferencesGroup(title="Nombre en OPL",
                                   description=self._name_hint())
        self.name_row = Adw.EntryRow(title="Nombre del juego", text=game.title, show_apply_button=True)
        self.name_row.connect("apply", self._rename)
        self.name_row.connect("changed", lambda r: grp.set_description(self._name_hint(r.get_text())))
        grp.add(self.name_row)
        self.name_grp = grp
        page.append(grp)

        acts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=8,
                           row_spacing=8, min_children_per_line=1, max_children_per_line=4)
        b1 = Gtk.Button(label="Descargar arte", sensitive=bool(game.gid))
        b1.connect("clicked", self._download_art)
        b2 = Gtk.Button(label="Cambiar portada…", sensitive=bool(game.gid))
        b2.connect("clicked", lambda *_: choose_cover(self.win, game, self, self.close))
        b3 = Gtk.Button(label="Abrir carpeta")
        b3.connect("clicked", lambda *_: open_folder(game.path))
        b4 = Gtk.Button(label="Mover a la papelera…")
        b4.add_css_class("destructive-action")
        b4.connect("clicked", lambda *_: (self.close(), trash_game_dialog(self.win, game)))
        for b in (b1, b2, b3, b4):
            acts.append(b)
        page.append(acts)

        if game.gid:
            ag = Adw.PreferencesGroup(title="Arte en ART/")
            fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                             min_children_per_line=2, column_spacing=8, row_spacing=8)
            for t, label in core.ART_LABELS.items():
                p = game.art(t)
                b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                pic = None
                if p:
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, 130, 100, True)
                        pic = Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(pb))
                    except GLib.Error:
                        pic = None
                if pic:
                    pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                    pic.set_size_request(130, 100)
                    pic.add_css_class("art-thumb")
                    b.append(pic)
                else:
                    ph = Gtk.Label(label="falta", width_request=130, height_request=100)
                    ph.add_css_class("art-thumb")
                    ph.add_css_class("dim-label")
                    b.append(ph)
                lb = Gtk.Label(label=label)
                lb.add_css_class("caption")
                b.append(lb)
                fb.append(b)
            ag.add(fb)
            page.append(ag)

    def _name_hint(self, text=None):
        text = self.g.title if text is None else text
        ml = int(self.win.cfg.get("max_name", 32) or 0)
        final = core.opl_filename(self.g.gid, text, core.ext_of(self.g.path), ml)
        return f"Quedará como: {final}"

    def _rename(self, row):
        try:
            new = core.rename_game(self.g, row.get_text(), int(self.win.cfg.get("max_name", 32) or 0))
        except Exception as e:  # noqa: BLE001
            self.win.toast(f"No se pudo renombrar: {e}")
            return
        self.win.toast(f"Renombrado a {os.path.basename(new)}")
        self.close()
        self.win.library.reload()

    def _download_art(self, btn):
        btn.set_sensitive(False)
        btn.set_label("Descargando…")
        lib = self.win.cfg["library"]

        def done(got):
            self.win.toast(f"{len(got)} imágenes nuevas" if got else "No había arte nuevo en la base")
            self.close()
            self.win.library.reload()
        run_bg(lambda: core.download_art(lib, self.g.gid), done,
               lambda e: (btn.set_sensitive(True), btn.set_label("Descargar arte"), self.win.toast(f"Error: {e}")))

    def _change_cover(self, *_):
        d = Gtk.FileDialog(title="Elegir portada")
        f = Gtk.FileFilter(name="Imágenes")
        f.add_mime_type("image/*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        d.set_filters(store)

        def chosen(dialog, res):
            try:
                file = dialog.open_finish(res)
            except GLib.Error:
                return
            path = file.get_path()
            if self.g.art("COV"):
                ask = Adw.AlertDialog(heading="¿Reemplazar la portada?",
                                      body="Este juego ya tiene portada. La actual se guardará como _COV.png.bak.")
                ask.add_response("cancel", "Cancelar")
                ask.add_response("ok", "Reemplazar")
                ask.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
                ask.connect("response", lambda _d, r: r == "ok" and self._save_cover(path, True))
                ask.present(self)
            else:
                self._save_cover(path, False)
        d.open(self.win, None, chosen)

    def _save_cover(self, path, replace):
        try:
            if replace:
                cur = self.g.art("COV")
                bak = core.free_path(cur + ".bak")
                os.rename(cur, bak)            # nunca se borra: queda como respaldo
            core.save_cover(self.g.library, self.g.gid, path)
        except Exception as e:  # noqa: BLE001
            self.win.toast(f"No se pudo guardar la portada: {e}")
            return
        self.win.toast("Portada guardada")
        self.close()
        self.win.library.reload()


# =============================================================== Bandeja de entrada
class InboxPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.items = []
        self.checks = []
        self.importing = False

        # acciones de la bandeja
        self.trash_after = Gio.SimpleAction.new_stateful(
            "trash-after", None, GLib.Variant.new_boolean(bool(win.cfg.get("trash_after_copy", False))))
        self.trash_after.connect("change-state", self._on_trash_after)
        group = action_group({
            "select-new": (lambda: self._select(lambda it: not it.in_library), True),
            "select-all": (lambda: self._select(lambda it: True), True),
            "select-none": (lambda: self._select(lambda it: False), True),
            "select-copied": (lambda: self._select(lambda it: bool(it.in_library)), True),
            "clean-copied": (self._clean_copied, True),
            "clean-selected": (self._clean_selected, True),
            "open-folder": (lambda: [open_folder(r) for r in (self.win.cfg.get("inboxes") or [])[:3] if os.path.isdir(r)], True),
        })
        group.add_action(self.trash_after)
        self.insert_action_group("inbox", group)

        self.bar = bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=4)
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        bar.append(self.status)
        buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=8,
                              row_spacing=6, min_children_per_line=1, max_children_per_line=6,
                              valign=Gtk.Align.CENTER)
        self.compact = False
        add = Gtk.Button(icon_name="folder-new-symbolic", tooltip_text="Agregar carpeta de ROMs del equipo")
        add.connect("clicked", lambda *_: self.win.add_inbox_folder())
        buttons.append(add)
        selm = Gio.Menu()
        selm.append("Nuevos", "inbox.select-new")
        selm.append("Todos", "inbox.select-all")
        selm.append("Ya copiados", "inbox.select-copied")
        selm.append("Ninguno", "inbox.select-none")
        buttons.append(Gtk.MenuButton(label="Seleccionar", menu_model=selm, tooltip_text="Elegir qué juegos marcar"))
        cleanm, c1, c2 = Gio.Menu(), Gio.Menu(), Gio.Menu()
        c1.append("Mover a la papelera los ya copiados", "inbox.clean-copied")
        c1.append("Mover a la papelera los seleccionados", "inbox.clean-selected")
        c2.append("Mover a la papelera después de copiar", "inbox.trash-after")
        c2.append("Abrir carpeta de ROMs", "inbox.open-folder")
        cleanm.append_section(None, c1)
        cleanm.append_section(None, c2)
        self.clean_btn = Gtk.MenuButton(icon_name="user-trash-symbolic", menu_model=cleanm,
                                        tooltip_text="Limpiar la bandeja (las ISOs ya copiadas y más)")
        buttons.append(self.clean_btn)
        self.import_btn = Gtk.Button(label="Copiar a la biblioteca")
        self.import_btn.add_css_class("suggested-action")
        self.import_btn.connect("clicked", lambda *_: self._import())
        buttons.append(self.import_btn)
        bar.append(buttons)
        self.append(bar)

        self.progress = Gtk.ProgressBar(margin_start=12, margin_end=12, visible=False, show_text=True)
        self.append(self.progress)

        self.stack = Gtk.Stack(vexpand=True)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=6, margin_bottom=12)
        self.list.add_css_class("boxed-list")
        clamp = Adw.Clamp(maximum_size=900, child=self.list, margin_start=12, margin_end=12)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(clamp)
        self.stack.add_named(sc, "list")
        self.empty = Adw.StatusPage(icon_name="folder-download-symbolic", title="Bandeja vacía",
                                    description="No hay juegos nuevos en la carpeta de descargas.")
        addb = Gtk.Button(label="Agregar carpeta de ROMs…", halign=Gtk.Align.CENTER)
        addb.add_css_class("pill")
        addb.connect("clicked", lambda *_: self.win.add_inbox_folder())
        self.empty.set_child(addb)
        self.stack.add_named(self.empty, "empty")
        self.loading = Adw.StatusPage(title="Revisando descargas…")
        self.loading.set_child(Gtk.Spinner(spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER))
        self.stack.add_named(self.loading, "loading")
        self.append(self.stack)

        note = Gtk.Label(wrap=True, xalign=0, margin_start=14, margin_end=14, margin_bottom=8,
                         label="Se copian, no se mueven, y en la biblioteca nunca se sobrescribe nada. Los PS2 van a DVD/ "
                               "con nombre OPL; los .bin/.cue a la carpeta de PS1 de Ajustes. Con 🗑 puedes llevar a la "
                               "papelera lo que ya copiaste. Clic derecho en un juego para más opciones.")
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        self.append(note)

    def reload(self):
        if self.importing:
            return
        roots, lib = list(self.win.cfg.get("inboxes") or []), self.win.cfg["library"]
        self.stack.set_visible_child_name("loading")
        self.status.set_text("  ·  ".join(map(tilde, roots)))
        self.status.set_tooltip_text("\n".join(map(tilde, roots)))
        if not roots:
            self.empty.set_title("Sin carpetas de ROMs")
            self.empty.set_description("Agrega la carpeta de descargas o donde guardas tus ROMs.")
            self.stack.set_visible_child_name("empty")
            return
        run_bg(lambda: core.scan_inboxes(roots, lib, self.win.cfg.get("ps1_dir") or "CD"), self._loaded,
               lambda e: self.win.toast(f"Error al leer la bandeja: {e}"))

    def _loaded(self, res):
        items, missing = res
        self.missing = missing
        self.items = items
        self.checks = []
        while (r := self.list.get_first_child()):
            self.list.remove(r)
        for it in items:
            self.list.append(self._row(it))
        new = sum(1 for i in items if not i.in_library and not i.busy)
        n = len(self.win.cfg.get("inboxes") or [])
        txt = f"{n} carpeta(s) de ROMs  ·  {len(items)} juegos  ·  {new} nuevos"
        if missing:
            txt += f"  ·  ⚠ {len(missing)} carpeta(s) no encontrada(s)"
        self.status.set_text(txt)
        self.empty.set_title("Bandeja vacía")
        self.empty.set_description("No hay juegos en tus carpetas de ROMs."
                                   + (f"\n\nNo se encuentran:\n" + "\n".join(missing) if missing else ""))
        self.stack.set_visible_child_name("list" if items else "empty")
        self._update_btn()
        self.win.update_badge(new)

    def _final_name(self, it):
        if it.multi:
            return ", ".join(os.path.basename(f) for f in it.files[:2]) + (" …" if len(it.files) > 2 else "")
        return core.opl_filename(it.gid, it.title, core.ext_of(it.main), int(self.win.cfg.get("max_name", 32) or 0))

    def _row(self, it):
        row = Adw.ActionRow(title=GLib.markup_escape_text(self._final_name(it)), title_lines=2)
        dest = "DVD" if (it.console == "PS2" and not it.multi) else (self.win.cfg.get("ps1_dir") or "CD")
        bits = [it.console + (" (adivinado)" if it.guessed else ""), it.gid or "sin código",
                core.human_size(it.size), f"→ {dest}/", f"desde {it.origin}"]
        if it.busy:
            bits.insert(0, "⏳ extrayéndose")
        if it.in_library:
            bits.insert(0, "✔ ya en la biblioteca")
        row.set_subtitle(GLib.markup_escape_text("  ·  ".join(bits)))
        row.set_subtitle_lines(2)

        chk = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=not it.in_library and not it.busy,
                              sensitive=not it.busy)
        chk.connect("toggled", lambda *_: self._update_btn())
        chk.item = it
        self.checks.append(chk)
        row.add_prefix(chk)
        row.set_activatable_widget(chk)

        thumb = it.cover or it.extra.get("inbox_art", {}).get("COV", "")
        row.add_prefix(cover_widget(thumb, 42, 60))

        if not it.multi:
            ed = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Cambiar nombre")
            ed.add_css_class("flat")
            ed.connect("clicked", lambda *_: self._edit_title(it, row))
            row.add_suffix(ed)

        row.insert_action_group("row", action_group({
            "copy": (lambda: self._import([it]), not it.busy and not it.in_library and not self.importing),
            "rename": (lambda: self._edit_title(it, row), not it.multi),
            "folder": (lambda: open_folder(it.main), True),
            "trash": (lambda: self._confirm_clean([it], f"¿Mover «{it.title}» a la papelera?"), not it.busy),
        }))

        def menu():
            m, a, b = Gio.Menu(), Gio.Menu(), Gio.Menu()
            a.append("Copiar solo este a la biblioteca", "row.copy")
            a.append("Cambiar nombre…", "row.rename")
            a.append("Abrir carpeta", "row.folder")
            b.append("Mover a la papelera…", "row.trash")
            m.append_section(None, a)
            m.append_section(None, b)
            return m
        add_context_menu(row, menu)
        return row

    def _edit_title(self, it, row):
        dlg = Adw.AlertDialog(heading="Nombre del juego")
        entry = Gtk.Entry(text=it.title, activates_default=True)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        ml = int(self.win.cfg.get("max_name", 32) or 0)

        def upd(*_):
            hint.set_text("Quedará como: " + core.opl_filename(it.gid, entry.get_text(), core.ext_of(it.main), ml))
        entry.connect("changed", upd)
        upd()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(entry)
        box.append(hint)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", "Guardar")
        dlg.set_default_response("ok")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r == "ok" and entry.get_text().strip():
                it.title = entry.get_text().strip()
                row.set_title(GLib.markup_escape_text(self._final_name(it)))
        dlg.connect("response", resp)
        dlg.present(self.win)

    def _selected(self):
        return [c.item for c in self.checks if c.get_active() and c.get_sensitive()]

    def _to_copy(self):
        return [i for i in self._selected() if not i.in_library]

    def _select(self, pred):
        for c in self.checks:
            if c.get_sensitive():
                c.set_active(pred(c.item))

    def _on_trash_after(self, action, value):
        action.set_state(value)
        self.win.cfg["trash_after_copy"] = value.get_boolean()
        core.save_config(self.win.cfg)
        self.win.toast("Lo copiado se moverá a la papelera" if value.get_boolean()
                       else "Lo copiado se queda en la bandeja")

    def _confirm_clean(self, items, heading, extra=""):
        items = [i for i in items if not i.busy]
        if not items:
            self.win.toast("No hay nada para limpiar")
            return
        targets = core.cleanup_targets(items)
        confirm_trash(self.win, heading, targets, lambda: trash_paths(self.win, targets, self.reload), extra)

    def _clean_copied(self):
        copied = [i for i in self.items if i.in_library and not i.busy]
        self._confirm_clean(copied, f"¿Mover a la papelera {len(copied)} juego(s) ya copiados?",
                            "Ya están en tu biblioteca OPL; solo se limpian de la bandeja.")

    def _clean_selected(self):
        sel = self._selected()
        pending = [i for i in sel if not i.in_library]
        self._confirm_clean(sel, f"¿Mover a la papelera {len(sel)} juego(s) seleccionados?",
                            f"⚠ {len(pending)} de ellos todavía NO están en la biblioteca." if pending else "")

    def _update_btn(self):
        sel = self._to_copy()
        size = sum(i.size for i in sel)
        if self.compact:
            self.import_btn.set_label(f"Copiar ({len(sel)})" if sel else "Copiar")
        else:
            self.import_btn.set_label(f"Copiar a la biblioteca ({len(sel)} · {core.human_size(size)})" if sel
                                      else "Copiar a la biblioteca")
        self.import_btn.set_sensitive(bool(sel) and not self.importing)

    def _import(self, sel=None):
        sel = [i for i in (sel if sel is not None else self._to_copy()) if not i.in_library and not i.busy]
        lib = self.win.cfg["library"]
        if not sel:
            return
        if not os.path.isdir(lib):
            self.win.toast("La biblioteca no está disponible (¿disco conectado?)")
            return
        need = sum(i.size for i in sel)
        if core.disk_free(lib) < need:
            self.win.toast(f"No hay espacio: hacen falta {core.human_size(need)}")
            return
        self.importing = True
        self._update_btn()
        self.progress.set_visible(True)
        cfg = dict(self.win.cfg)

        def work():
            ok, errors, art, copied_items = 0, [], 0, []
            for n, it in enumerate(sel):
                name = it.title

                def prog(done, total, n=n, name=name):
                    GLib.idle_add(self._prog, n, len(sel), name, done, total)
                try:
                    _copied, got = core.import_item(it, cfg, prog)
                    ok += 1
                    art += len(got)
                    copied_items.append(it)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{name}: {e}")
            return ok, errors, art, copied_items

        def done(res):
            ok, errors, art, copied_items = res
            self.importing = False
            self.progress.set_visible(False)
            msg = f"{ok} juego(s) copiados, {art} imágenes de arte"
            self.win.job_finished("Copia terminada", msg + (f" · {len(errors)} con error" if errors else ""))
            if copied_items and self.win.cfg.get("trash_after_copy"):
                trash_paths(self.win, core.cleanup_targets(copied_items), self.reload)
            if errors:
                msg += f" · {len(errors)} con error"
                al = Adw.AlertDialog(heading="Algunos juegos no se copiaron", body="\n".join(errors))
                al.add_response("ok", "Aceptar")
                al.present(self.win)
            self.win.toast(msg)
            self.reload()
            self.win.library.reload()
        run_bg(work, done)

    def _prog(self, n, total_items, name, done, total):
        frac = done / total if total else 1
        self.win.tray_status(f"Copiando {n + 1}/{total_items}: {name} ({int(frac * 100)} %)")
        self.progress.set_fraction((n + frac) / total_items)
        self.progress.set_text(f"{n + 1}/{total_items}  {name}  —  {core.human_size(done)} de {core.human_size(total)}")


# =============================================================== Buscar pendrive / disco OPL
class DriveDialog(Adw.Dialog):
    def __init__(self, win, on_pick=None):
        super().__init__(title="Pendrive o disco de OPL", content_width=620, content_height=520)
        self.win = win
        self.on_pick = on_pick
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        again = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Volver a buscar")
        again.connect("clicked", lambda *_: self.scan())
        hb.pack_start(again)
        tv.add_top_bar(hb)
        self.stack = Gtk.Stack()
        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "list")
        self.none = Adw.StatusPage(icon_name="drive-removable-media-symbolic", title="No hay unidades conectadas",
                                   description="Conecta el pendrive o disco de tu PS2 y pulsa ↻.")
        self.stack.add_named(self.none, "none")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.groups = []
        self.scan()

    def scan(self):
        for g in self.groups:
            self.page.remove(g)
        self.groups = []
        run_bg(core.find_opl_drives, self._show)

    def _show(self, drives):
        if not drives:
            self.stack.set_visible_child_name("none")
            return
        self.stack.set_visible_child_name("list")
        cur = os.path.realpath(self.win.cfg.get("library") or "")
        opl = [d for d in drives if d["is_opl"]]
        other = [d for d in drives if not d["is_opl"]]
        for title, desc, lst in (
                ("Con OPL", "Unidades que ya tienen carpetas de Open PS2 Loader", opl),
                ("Otras unidades", "Al copiar juegos se crearán DVD/ y ART/ en ellas", other)):
            if not lst:
                continue
            g = Adw.PreferencesGroup(title=title, description=desc)
            for d in lst:
                bits = []
                if d["is_opl"]:
                    bits.append(f"{d['games']} juegos")
                bits.append(f"{core.human_size(d['free'])} libres de {core.human_size(d['total'])}")
                bits.append(d["fs"])
                if d["fs"] in ("vfat", "msdos"):
                    bits.append("FAT32: máx. 4 GB por archivo")
                row = Adw.ActionRow(title=GLib.markup_escape_text(d["label"]),
                                    subtitle=GLib.markup_escape_text(f"{d['path']}\n" + "  ·  ".join(bits)),
                                    subtitle_lines=3)
                icon = Gtk.Image.new_from_icon_name("media-optical-symbolic" if d["is_opl"] else "drive-harddisk-symbolic")
                row.add_prefix(icon)
                if os.path.realpath(d["path"]) == cur:
                    lb = Gtk.Label(label="En uso", valign=Gtk.Align.CENTER)
                    lb.add_css_class("dim-label")
                    row.add_suffix(lb)
                else:
                    b = Gtk.Button(label="Usar", valign=Gtk.Align.CENTER)
                    if d["is_opl"]:
                        b.add_css_class("suggested-action")
                    b.connect("clicked", lambda *_a, p=d["path"]: self._pick(p))
                    row.add_suffix(b)
                    row.set_activatable_widget(b)
                g.add(row)
            self.page.add(g)
            self.groups.append(g)

    def _pick(self, path):
        self.win.set_library(path)
        if self.on_pick:
            self.on_pick(path)
        self.close()


# =============================================================== Ajustes
class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Ajustes")
        self.win = win
        cfg = win.cfg
        page = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")
        self.add(page)

        g1 = Adw.PreferencesGroup(title="Biblioteca OPL",
                                  description="Pendrive o disco de la PS2 (donde están DVD/, CD/, POPS/, ART/)")
        self.lib_row = self._folder_row("Carpeta de la biblioteca", "library",
                                        "Raíz del disco de OPL (donde están DVD/, CD/, POPS/, ART/)")
        srch = Gtk.Button(icon_name="system-search-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Buscar pendrive o disco OPL conectado")
        srch.add_css_class("flat")
        srch.connect("clicked", lambda *_: DriveDialog(self.win, on_pick=lambda p: self.lib_row.set_subtitle(tilde(p))).present(self))
        self.lib_row.add_suffix(srch)
        g1.add(self.lib_row)
        ps1 = Adw.EntryRow(title="Carpeta de la biblioteca para .bin/.cue de PS1", text=cfg.get("ps1_dir") or "CD",
                           show_apply_button=True)
        ps1.connect("apply", lambda r: self._set("ps1_dir", r.get_text().strip().strip("/") or "CD"))
        g1.add(ps1)
        page.add(g1)

        self.g_in = Adw.PreferencesGroup(title="Carpetas de ROMs (bandeja de entrada)",
                                         description="Se revisan todas buscando juegos para copiar a la biblioteca. "
                                                     "Quitar una de la lista no borra ningún archivo.")
        addf = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Agregar carpeta")
        addf.add_css_class("flat")
        addf.connect("clicked", lambda *_: self.win.add_inbox_folder(self._fill_inboxes))
        self.g_in.set_header_suffix(addf)
        self.in_rows = []
        self._fill_inboxes()
        page.add(self.g_in)

        g2 = Adw.PreferencesGroup(title="Nombres y arte")
        short = Adw.SwitchRow(title="Nombres cortos para OPL viejo",
                              subtitle="Máx. 32 caracteres, sin acentos. Desactívalo si usas OPL 1.1 o posterior.",
                              active=int(cfg.get("max_name", 32) or 0) > 0)
        short.connect("notify::active", lambda r, _p: self._set("max_name", 32 if r.get_active() else 0))
        g2.add(short)
        art = Adw.SwitchRow(title="Descargar arte al copiar",
                            subtitle="Portada, contraportada, lomo, logo, fondo y capturas desde la base pública de OPL.",
                            active=bool(cfg.get("download_art", True)))
        art.connect("notify::active", lambda r, _p: self._set("download_art", r.get_active()))
        g2.add(art)
        page.add(g2)

        g_bg = Adw.PreferencesGroup(title="Ventana")
        has_tray = tray.tray_available()
        tr = Adw.SwitchRow(title="Al cerrar, quedarse en la bandeja del sistema",
                           subtitle=("Queda un ícono en la barra superior; ábrelo con un clic o usa Salir en su menú."
                                     if has_tray else "Tu escritorio no tiene bandeja del sistema "
                                     "(en GNOME: extensión AppIndicator)."),
                           active=bool(cfg.get("close_to_tray", True)) and has_tray, sensitive=has_tray)
        tr.connect("notify::active", lambda r, _p: (self._set("close_to_tray", r.get_active(), reload=False),
                                                     self.win.setup_tray()))
        g_bg.add(tr)
        bg = Adw.SwitchRow(title="Seguir copiando en segundo plano al cerrar",
                           subtitle="Si cierras la ventana durante una copia, sigue y te avisa al terminar.",
                           active=bool(cfg.get("background_on_close", True)))
        bg.connect("notify::active", lambda r, _p: self._set("background_on_close", r.get_active(), reload=False))
        g_bg.add(bg)
        ta = Adw.SwitchRow(title="Mover a la papelera lo que copies desde la bandeja",
                           subtitle="Solo después de comprobar que la copia quedó completa.",
                           active=bool(cfg.get("trash_after_copy", False)))
        ta.connect("notify::active", lambda r, _p: (self._set("trash_after_copy", r.get_active(), reload=False),
                                                     self.win.inbox.trash_after.set_state(GLib.Variant.new_boolean(r.get_active()))))
        g_bg.add(ta)
        page.add(g_bg)

        mode = updater.install_mode()
        how = {"self": "se instalan solas, sin contraseña",
               "deb": "paquete .deb: se instalan con tu contraseña",
               "source": "copia del código: actualiza con git pull"}[mode]
        g3 = Adw.PreferencesGroup(title="Actualizaciones",
                                  description=f"Versión {core.__version__} · {how}")
        au = Adw.SwitchRow(title="Buscar actualizaciones automáticamente",
                           subtitle="Al abrir la app, como máximo una vez al día, en los releases de GitHub.",
                           active=bool(cfg.get("auto_update", True)))
        au.connect("notify::active", lambda r, _p: self._set("auto_update", r.get_active(), reload=False))
        g3.add(au)
        if mode == "self":
            ai = Adw.SwitchRow(title="Instalarlas solas",
                               subtitle="Se descargan, se verifican (SHA256) y solo tienes que reiniciar.",
                               active=bool(cfg.get("auto_install", True)))
            ai.connect("notify::active", lambda r, _p: self._set("auto_install", r.get_active(), reload=False))
            au.bind_property("active", ai, "sensitive", GObject.BindingFlags.SYNC_CREATE)
            g3.add(ai)
        now = Adw.ButtonRow(title="Buscar ahora") if hasattr(Adw, "ButtonRow") else None
        if now:
            now.connect("activated", lambda *_: (self.win.check_updates(manual=True), self.close()))
            g3.add(now)
        page.add(g3)

    def _fill_inboxes(self):
        for r in self.in_rows:
            self.g_in.remove(r)
        self.in_rows = []
        roots = self.win.cfg.get("inboxes") or []
        if not roots:
            r = Adw.ActionRow(title="Ninguna carpeta", subtitle="Agrega una con el botón +")
            self.g_in.add(r)
            self.in_rows.append(r)
        for path in roots:
            ok = os.path.isdir(path)
            r = Adw.ActionRow(title=GLib.markup_escape_text(os.path.basename(path.rstrip("/")) or path),
                              subtitle=GLib.markup_escape_text(tilde(path) + ("" if ok else "  (no encontrada)")))
            rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Quitar de la lista (no borra archivos)")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda *_a, p=path: self._remove_inbox(p))
            r.add_suffix(rm)
            self.g_in.add(r)
            self.in_rows.append(r)

    def _remove_inbox(self, path):
        self.win.cfg["inboxes"] = [p for p in self.win.cfg.get("inboxes") or [] if p != path]
        core.save_config(self.win.cfg)
        self._fill_inboxes()
        self.win.inbox.reload()

    def _folder_row(self, title, key, desc):
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(tilde(self.win.cfg[key])))
        row.set_tooltip_text(desc)
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        row.add_suffix(btn)
        row.set_activatable_widget(btn)

        def pick(*_):
            d = Gtk.FileDialog(title=title)
            if os.path.isdir(self.win.cfg[key]):
                d.set_initial_folder(Gio.File.new_for_path(self.win.cfg[key]))

            def chosen(dialog, res):
                try:
                    f = dialog.select_folder_finish(res)
                except GLib.Error:
                    return
                self._set(key, f.get_path())
                row.set_subtitle(GLib.markup_escape_text(tilde(f.get_path())))
            d.select_folder(self.win, None, chosen)
        btn.connect("clicked", pick)
        return row

    def _set(self, key, value, reload=True):
        self.win.cfg[key] = value
        core.save_config(self.win.cfg)
        if reload:
            self.win.reload_all()


# =============================================================== Ventana
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="OPL Manager", default_width=1180, default_height=780)
        self.cfg = core.load_config()

        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.vstack = Adw.ViewStack()
        self.switcher = switcher = Adw.ViewSwitcher(stack=self.vstack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Volver a leer (F5)")
        refresh.connect("clicked", lambda *_: self.reload_all())
        header.pack_start(refresh)
        menu = Gio.Menu()
        menu.append("Buscar pendrive o disco OPL", "win.find-drive")
        menu.append("Agregar carpeta de ROMs…", "win.add-folder")
        menu.append("Buscar actualizaciones", "win.check-updates")
        menu.append("Ajustes", "win.settings")
        menu.append("Abrir biblioteca en Archivos", "win.open-lib")
        menu.append("Acerca de", "win.about")
        menu.append("Salir", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        tv.add_top_bar(header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner)
        tv.add_top_bar(self.banner)
        self.pending_release = None
        self.update_state = ""          # "available" | "installed"

        self.library = LibraryPage(self)
        self.inbox = InboxPage(self)
        self.lib_page = self.vstack.add_titled_with_icon(self.library, "library", "Biblioteca", "view-grid-symbolic")
        self.inbox_page = self.vstack.add_titled_with_icon(self.inbox, "inbox", "Bandeja de entrada",
                                                           "folder-download-symbolic")
        tv.set_content(self.vstack)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.vstack)
        tv.add_bottom_bar(self.switcher_bar)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        # ---- diseño adaptable: la ventana se puede achicar hasta tamaño de teléfono
        self.set_size_request(360, 480)
        mid = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 980sp"))
        mid.add_setter(self.library.bar, "orientation", Gtk.Orientation.VERTICAL)
        mid.add_setter(self.inbox.bar, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(mid)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
        narrow.add_setter(self.library.bar, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.inbox.bar, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.switcher, "visible", False)
        narrow.add_setter(self.switcher_bar, "reveal", True)
        narrow.add_setter(self.library.art_btn, "label", "Arte faltante")
        narrow.connect("apply", lambda *_: self._set_compact(True))
        narrow.connect("unapply", lambda *_: self._set_compact(False))
        self.add_breakpoint(narrow)

        # ---- cerrar: a la bandeja del sistema (o, sin bandeja, seguir en segundo plano si está copiando)
        self.in_background = False
        self.quit_when_done = False
        self.tray_hint_shown = False
        self.tray = None
        self.connect("close-request", self._on_close_request)
        self.setup_tray()

        for name, cb in (("settings", lambda *_: SettingsDialog(self).present(self)),
                         ("open-lib", lambda *_: os.path.isdir(self.cfg["library"]) and Gio.AppInfo.launch_default_for_uri(
                             GLib.filename_to_uri(self.cfg["library"]), None)),
                         ("about", self._about),
                         ("find-drive", lambda *_: DriveDialog(self).present(self)),
                         ("add-folder", lambda *_: self.add_inbox_folder()),
                         ("check-updates", lambda *_: self.check_updates(manual=True)),
                         ("reload", lambda *_: self.reload_all())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        app.set_accels_for_action("win.reload", ["F5", "<Control>r"])
        app.set_accels_for_action("win.settings", ["<Control>comma"])
        app.set_accels_for_action("window.close", ["<Control>w"])
        self.vstack.set_visible_child_name("library")
        self.reload_all()

        # Avisar / usar automáticamente el pendrive OPL cuando se conecta
        # unidades ya conectadas al abrir: no se anuncian como "nuevas"
        self.known_mounts = {d["path"] for d in core.find_opl_drives()}
        self.vmon = Gio.VolumeMonitor.get()
        self.vmon.connect("mount-added", lambda *_: GLib.timeout_add(1500, self._on_mount_added))
        self.vmon.connect("mount-removed", lambda *_: GLib.timeout_add(800, self._on_mount_removed))
        if not os.path.isdir(self.cfg["library"]):
            run_bg(core.find_opl_drives, self._auto_pick)
        if updater.should_check(self.cfg):
            GLib.timeout_add_seconds(3, lambda: (self.check_updates(), False)[1])

    # ---- actualizaciones
    def check_updates(self, manual=False):
        def done(rel):
            self.cfg["last_update_check"] = time.time()
            core.save_config(self.cfg)
            if not updater.is_newer(rel["version"], core.__version__):
                if manual:
                    self.toast(f"Tienes la última versión ({core.__version__})")
                return
            self.pending_release = rel
            mode = updater.install_mode()
            if mode == "self" and self.cfg.get("auto_install", True):
                self._install_update()
            else:
                self.update_state = "available"
                self.banner.set_title(f"Hay una nueva versión: {rel['version']} (tienes {core.__version__})")
                self.banner.set_button_label("Actualizar" if mode != "source" else "Ver novedades")
                self.banner.set_revealed(True)

        def fail(e):
            if manual:
                self.toast(f"No se pudo buscar actualizaciones: {e}")
        run_bg(updater.latest_release, done, fail)

    def _install_update(self):
        rel = self.pending_release
        self.banner.set_title(f"Actualizando a la versión {rel['version']}…")
        self.banner.set_button_label(None)
        self.banner.set_revealed(True)

        def done(_):
            self.update_state = "installed"
            self.banner.set_title(f"OPL Manager se actualizó a la versión {rel['version']}")
            self.banner.set_button_label("Reiniciar")

        def fail(e):
            self.update_state = "available"
            self.banner.set_title(f"No se pudo actualizar a {rel['version']}: {e}")
            self.banner.set_button_label("Reintentar")
        run_bg(lambda: updater.apply_update(rel), done, fail)

    def _on_banner(self, *_):
        if self.update_state == "installed":
            if self.inbox.importing:
                self.toast("Espera a que termine la copia antes de reiniciar")
                return
            updater.restart()
        elif self.pending_release:
            if updater.install_mode() == "source":
                Gio.AppInfo.launch_default_for_uri(self.pending_release["url"], None)
            else:
                self._install_update()

    # ---- unidades
    def set_library(self, path):
        self.cfg["library"] = path
        core.save_config(self.cfg)
        self.toast(f"Biblioteca: {tilde(path)}")
        self.reload_all()

    def _auto_pick(self, drives):
        opl = [d for d in drives if d["is_opl"]]
        if len(opl) == 1:
            self.set_library(opl[0]["path"])
        elif len(opl) > 1:
            DriveDialog(self).present(self)

    def _on_mount_added(self):
        if os.path.isdir(self.cfg["library"]):
            def maybe_offer(drives):
                cur = os.path.realpath(self.cfg["library"])
                new = [d for d in drives if d["path"] not in self.known_mounts]
                self.known_mounts |= {d["path"] for d in drives}
                for d in new:
                    if d["is_opl"] and not cur.startswith(os.path.realpath(d["path"])):
                        t = Adw.Toast(title=GLib.markup_escape_text(f"Se conectó «{d['label']}» con OPL"),
                                      button_label="Usar", timeout=8)
                        t.connect("button-clicked", lambda *_a, p=d["path"]: self.set_library(p))
                        self.toasts.add_toast(t)
            run_bg(core.find_opl_drives, maybe_offer)
        else:
            run_bg(core.find_opl_drives, self._auto_pick)
        return False

    def _on_mount_removed(self):
        self.known_mounts = {d["path"] for d in core.find_opl_drives()}
        if not os.path.isdir(self.cfg["library"]):
            self.toast("Se desconectó la biblioteca")
            self.library.reload()
        return False

    def add_inbox_folder(self, after=None):
        d = Gtk.FileDialog(title="Agregar carpeta de ROMs")

        def chosen(dialog, res):
            try:
                f = dialog.select_folder_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            roots = self.cfg.get("inboxes") or []
            if path in roots:
                self.toast("Esa carpeta ya está en la lista")
                return
            if os.path.realpath(path).startswith(os.path.realpath(self.cfg["library"]).rstrip("/") + "/") or \
                    os.path.realpath(path) == os.path.realpath(self.cfg["library"]):
                self.toast("Esa carpeta es la biblioteca OPL; elige otra")
                return
            self.cfg["inboxes"] = roots + [path]
            core.save_config(self.cfg)
            self.toast(f"Agregada: {tilde(path)}")
            self.inbox.reload()
            self.vstack.set_visible_child_name("inbox")
            if after:
                after()
        d.select_folder(self, None, chosen)

    def reload_all(self):
        self.library.reload()
        self.inbox.reload()

    def _set_compact(self, on):
        self.inbox.compact = on
        self.inbox._update_btn()

    def busy(self):
        return self.inbox.importing or not self.library.art_btn.get_sensitive()

    # ---- bandeja del sistema
    def setup_tray(self):
        want = bool(self.cfg.get("close_to_tray", True)) and tray.tray_available()
        if want and not self.tray:
            try:
                self.tray = tray.TrayIcon(APP_ID, "OPL Manager", "media-optical", self.come_back, self._tray_items())
            except GLib.Error as e:
                print("bandeja no disponible:", e.message)
                self.tray = None
        elif not want and self.tray:
            self.tray.remove()
            self.tray = None

    def _tray_items(self, new=0):
        return [
            (1, "Mostrar OPL Manager", self.come_back),
            (2, f"Bandeja de entrada ({new} nuevos)" if new else "Bandeja de entrada",
             lambda: (self.come_back(), self.vstack.set_visible_child_name("inbox"))),
            (3, None, None),
            (4, "Buscar actualizaciones", lambda: self.check_updates(manual=True)),
            (5, None, None),
            (6, "Salir", self.quit_app),
        ]

    def tray_status(self, text=None):
        if self.tray:
            self.tray.set_tooltip(text or "Abrir OPL Manager")

    def _hide(self):
        self.set_visible(False)
        if not self.in_background:
            self.in_background = True
            self.get_application().hold()

    def _on_close_request(self, *_):
        if self.tray:
            self._hide()
            if self.busy():
                self.notify("background", "OPL Manager sigue copiando",
                            "Está en la bandeja del sistema. Te aviso cuando termine.")
            elif not self.tray_hint_shown:
                self.notify("background", "OPL Manager sigue en la bandeja",
                            "Haz clic en su ícono de la barra superior para abrirlo, o elige Salir en su menú.")
            self.tray_hint_shown = True
            return True
        if self.busy() and self.cfg.get("background_on_close", True):
            self._hide()
            self.notify("background", "OPL Manager sigue trabajando",
                        "La copia continúa en segundo plano. Te aviso cuando termine.")
            return True              # no cerrar todavía
        return False

    def notify(self, nid, title, body):
        n = Gio.Notification.new(title)
        n.set_body(body)
        self.get_application().send_notification(nid, n)

    def quit_app(self):
        if self.busy():
            self.come_back()
            ask = Adw.AlertDialog(heading="Hay una copia en curso",
                                  body="Si sales ahora, el juego que se está copiando quedaría a medias.")
            ask.add_response("cancel", "Cancelar")
            ask.add_response("later", "Salir al terminar")
            ask.set_response_appearance("later", Adw.ResponseAppearance.SUGGESTED)
            ask.set_default_response("later")
            ask.set_close_response("cancel")

            def resp(_d, r):
                if r == "later":
                    self.quit_when_done = True
                    self.toast("OPL Manager se cerrará al terminar la copia")
            ask.connect("response", resp)
            ask.present(self)
            return
        if self.tray:
            self.tray.remove()
            self.tray = None
        if self.in_background:
            self.in_background = False
            self.get_application().release()
        self.get_application().quit()

    def come_back(self):
        """Volver a mostrar la ventana (p. ej. al abrir la app otra vez mientras trabaja)."""
        if self.in_background:
            self.in_background = False
            self.get_application().release()
            self.get_application().withdraw_notification("background")
        self.set_visible(True)
        self.present()

    def job_finished(self, title, body):
        self.tray_status()
        app = self.get_application()
        if self.quit_when_done and not self.busy():
            self.notify("done", title, body)
            self.quit_when_done = False
            GLib.timeout_add(300, lambda: (self.quit_app(), False)[1])
            return
        if self.in_background:
            self.notify("done", title, body)
            app.withdraw_notification("background")
            if not self.tray and not self.busy():   # sin bandeja: ahora sí se cierra
                self.in_background = False
                app.release()
                self.destroy()
        else:
            self.toast(body)

    def update_badge(self, n):
        self.inbox_page.set_needs_attention(n > 0)
        self.inbox_page.set_badge_number(n)
        if self.tray:
            self.tray.set_items(self._tray_items(n))

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=4))

    def _about(self, *_):
        Adw.AboutDialog(application_name="OPL Manager", application_icon="media-optical",
                        developer_name="CamiloFerreira", version=core.__version__,
                        website="https://github.com/CamiloFerreira/opl-manager-linux",
                        issue_url="https://github.com/CamiloFerreira/opl-manager-linux/issues",
                        license_type=Gtk.License.MIT_X11,
                        comments="Gestor de bibliotecas de Open PS2 Loader para Linux.\n"
                                 "Arte: github.com/Luden02/psx-ps2-opl-art-database").present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: self.get_windows()[0].quit_app() if self.get_windows() else self.quit())
        self.add_action(q)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        from gi.repository import Gdk
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        wins = self.get_windows()
        if wins:
            wins[0].come_back()
        else:
            MainWindow(self).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
