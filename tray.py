"""
Ícono en la bandeja del sistema (StatusNotifierItem + dbusmenu) hecho directo sobre D-Bus.

Funciona con la extensión AppIndicator de GNOME/Ubuntu, KDE, XFCE, Cinnamon, etc. No usa
libappindicator (que es GTK 3 y no se puede mezclar con GTK 4).
"""
import os

from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})"/><arg type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  </interface>
</node>"""


def tray_available():
    """¿Hay una bandeja del sistema (StatusNotifierWatcher) en la sesión?"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                          GLib.Variant("(s)", (WATCHER,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        return bool(r.unpack()[0])
    except GLib.Error:
        return False


class TrayIcon:
    """Ícono de bandeja. items: lista de (id, etiqueta, función) o (id, None, None) para un separador."""

    def __init__(self, app_id, title, icon_name, on_activate, items):
        self.title = title
        self.icon_name = icon_name
        self.on_activate = on_activate
        self.items = list(items)
        self.tooltip = title
        self.revision = 1
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._ids = []
        self._ids.append(self.bus.register_object(
            ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0],
            self._item_call, self._item_prop, None))
        self._ids.append(self.bus.register_object(
            MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
            self._menu_call, self._menu_prop, None))
        self._own = Gio.bus_own_name_on_connection(self.bus, self.service, Gio.BusNameOwnerFlags.NONE, None, None)
        # registrarse ahora y cada vez que la bandeja (re)aparezca, p. ej. si se reinicia GNOME Shell
        self._watch = Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                      lambda *_: self._register(), None)

    # ---------------------------------------------------------------- registro
    def _register(self):
        self.bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, -1, None, None)

    def remove(self):
        for i in self._ids:
            self.bus.unregister_object(i)
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._own)
        self._ids = []

    # ---------------------------------------------------------------- StatusNotifierItem
    def _item_prop(self, _conn, _sender, _path, _iface, name):
        tooltip = GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], self.title, self.tooltip))
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "opl-manager"),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", ""),
            "ToolTip": tooltip,
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }.get(name)

    def _item_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        invocation.return_value(None)

    def set_tooltip(self, text):
        self.tooltip = text
        self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)

    # ---------------------------------------------------------------- menú (dbusmenu)
    def set_items(self, items):
        self.items = list(items)
        self.revision += 1
        self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                             GLib.Variant("(ui)", (self.revision, 0)))

    def _props(self, item):
        iid, label, _cb = item
        if label is None:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True)}

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (it[0], self._props(it), [])) for it in self.items]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _menu_prop(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            out = [(it[0], self._props(it)) for it in self.items if not ids or it[0] in ids]
            if not ids or 0 in ids:
                out.insert(0, (0, {"children-display": GLib.Variant("s", "submenu")}))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            iid, name = params.unpack()
            props = next((self._props(it) for it in self.items if it[0] == iid), {})
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            iid, event = params.unpack()[:2]
            if event == "clicked":
                self._clicked(iid)
            invocation.return_value(None)
        elif method == "EventGroup":
            for iid, event, *_ in params.unpack()[0]:
                if event == "clicked":
                    self._clicked(iid)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _clicked(self, iid):
        for it in self.items:
            if it[0] == iid and it[2]:
                GLib.idle_add(lambda cb=it[2]: (cb(), False)[1])
