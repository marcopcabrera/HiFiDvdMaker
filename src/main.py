# main.py
import ctypes
import gettext
import os
import sys

# --- 1. IMPORTACIONES CRÍTICAS DE GI ---
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gio, GLib, Gtk, Adw

# --- 2. IDENTIDAD DEL PROCESO EN GLIB/WAYLAND ---
APP_ID = 'io.github.marcopcabrera.HiFiDvdMaker'
SCHEMA_ID = 'io.github.marcopcabrera.HiFiDvdMaker'
APP_NAME = 'hifidvdmaker'

# Forzar el nombre del programa y de la aplicación antes de cualquier UI
GLib.set_prgname(APP_ID)
GLib.set_application_name('HiFi DVD Maker')


# --- 2. CONFIGURACIÓN Y LECTURA DE IDIOMA EN GSETTINGS ---
LANGUAGES_MAP = [
    ('system', 'Predeterminado del sistema'),
    ('ca', 'Català'),
    ('de', 'Deutsch'),
    ('en', 'English'),
    ('es', 'Español'),
    ('eu', 'Euskara'),
    ('fr', 'Français'),
    ('gl', 'Galego'),
    ('it', 'Italiano'),
    ('ja', '日本語'),
    ('pt_BR', 'Português (Brasil)'),
    ('pt_PT', 'Português (Portugal)'),
    ('ru', 'Русский'),
    ('zh_CN', '中文 (简体)'),
]

# Leer preferencia e inyectar LANGUAGE en el entorno antes de inicializar GTK
try:
    init_settings = Gio.Settings.new(SCHEMA_ID)
    saved_lang = init_settings.get_string('language')
    if saved_lang and saved_lang != 'system':
        os.environ['LANGUAGE'] = saved_lang
except Exception:
    pass


# --- 3. RUTAS Y BINDING DE GETTEXT (C Y PYTHON) ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_BUILD_PO = os.path.join(PROJECT_ROOT, '_build', 'po')
SYSTEM_LOCALE = '/app/share/locale' if os.path.exists('/app/share/locale') else '/usr/share/locale'

LOCALE_DIR = (
    LOCAL_BUILD_PO if os.path.exists(LOCAL_BUILD_PO) else SYSTEM_LOCALE
)

# Binding para C (GTK4 / Libadwaita traducen archivos .ui mediante esto)
try:
    libc = ctypes.CDLL(None)
    for domain in [b'libadwaita', b'gtk40', APP_NAME.encode('utf-8')]:
        libc.bindtextdomain(domain, LOCALE_DIR.encode('utf-8'))
        libc.bind_textdomain_codeset(domain, b'UTF-8')
except Exception:
    pass

# Binding para Python
try:
    gettext.bindtextdomain(APP_NAME, LOCALE_DIR)
    gettext.textdomain(APP_NAME)

    lang = os.environ.get('LANGUAGE')
    languages = [lang] if lang else None

    trans = gettext.translation(APP_NAME, localedir=LOCALE_DIR, languages=languages, fallback=True)
    trans.install()
    _ = trans.gettext
except Exception:
    _ = lambda s: s


# --- 4. INICIALIZACIÓN DE GTK / LIBADWAITA ---
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gdk, Gtk

from .window import HiFiDvdMakerWindow


class HifidvdmakerApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            resource_base_path='/io/github/marcopcabrera/HiFiDvdMaker',
        )

        self.settings = Gio.Settings.new(SCHEMA_ID)

        self.create_action('quit', lambda *unused: self.quit(), ['<control>q'])
        self.create_action('about', self.on_about_action)
        self.create_action(
            'preferences', self.on_preferences_action, ['<control>comma']
        )

    def do_startup(self):
        Adw.Application.do_startup(self)

        self.set_accels_for_action('win.add-audio', ['<Control>o'])
        self.set_accels_for_action('win.select-cover', ['<Control>i'])
        self.set_accels_for_action(
            'win.select-output', ['<Control><Shift>o']
        )
        self.set_accels_for_action('win.generate-dvd', ['<Control>Return'])
        self.set_accels_for_action(
            'win.show-help-overlay', ['F1', '<Control>question']
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = HiFiDvdMakerWindow(application=self)
            self.add_window(win)  # <--- Vincular explícitamente la ventana a la app
        win.present()

    def on_about_action(self, *args):
        about = Adw.AboutDialog(
            application_name='HiFi DVD Maker',
            application_icon=APP_ID,
            developer_name='Marco P. Cabrera',
            version='0.1.0',
            copyright='© 2026 Marco P. Cabrera',
            license_type=Gtk.License.GPL_3_0,
            website='https://github.com/marcopcabrera/HiFiDvdMaker',
            issue_url='https://github.com/marcopcabrera/HiFiDvdMaker/issues',
            developers=[
                'Marco P. Cabrera (Project Direction & Integration)'
            ],
            designers=['Marco P. Cabrera (UX/UI Design)'],
            artists=['Marco P. Cabrera'],
            translator_credits='Susana Leão (Portuguese - Portugal)',
        )

        about.add_acknowledgement_section(
            'Technical Assistance', ['Gemini (AI Code Engine)']
        )

        about.present(self.props.active_window)

    def on_preferences_action(self, widget, _action_param):
        win = self.props.active_window

        # 1. Cargar el diálogo desde el recurso compilado
        builder = Gtk.Builder.new_from_resource(
            '/io/github/marcopcabrera/hifidvdmaker/preferences-dialog.ui'
        )
        prefs = builder.get_object('preferences_dialog')
        prefs.set_transient_for(win)

        # 2. Obtener los widgets definidos en el XML por su ID
        row_format = builder.get_object('pref_combo_audio')
        row_language = builder.get_object('pref_combo_language')
        row_remember = builder.get_object('pref_switch_location')
        row_speed = builder.get_object('pref_switch_multithread')
        row_notify = builder.get_object('pref_switch_notifications')

        # 3. Asignar la lista dinámica de idiomas al ComboRow de idioma
        lang_names = [name for _, name in LANGUAGES_MAP]
        row_language.set_model(Gtk.StringList.new(lang_names))

        # 4. Establecer los valores iniciales leídos de GSettings
        formato_guardado = self.settings.get_string('audio-format')
        row_format.set_selected(1 if formato_guardado == 'ac3' else 0)

        saved_lang_code = self.settings.get_string('language')
        idx_lang = next(
            (i for i, (code, _) in enumerate(LANGUAGES_MAP) if code == saved_lang_code), 0
        )
        row_language.set_selected(idx_lang)

        row_remember.set_active(self.settings.get_boolean('remember-location'))
        row_speed.set_active(self.settings.get_boolean('multithreading'))
        row_notify.set_active(self.settings.get_boolean('notify-on-complete'))

        # 5. Conectar señales para actualizar GSettings y la ventana principal
        def on_format_changed(combo, _pspec):
            nuevo_val = 'ac3' if combo.get_selected() == 1 else 'pcm'
            self.settings.set_string('audio-format', nuevo_val)
            if win and hasattr(win, 'set_audio_format_by_index'):
                win.set_audio_format_by_index(combo.get_selected())

        def on_language_changed(combo, _pspec):
            selected_code = LANGUAGES_MAP[combo.get_selected()][0]
            self.settings.set_string('language', selected_code)

        row_format.connect('notify::selected', on_format_changed)
        row_language.connect('notify::selected', on_language_changed)

        row_remember.connect(
            'notify::active',
            lambda w, _pspec: self.settings.set_boolean('remember-location', w.get_active())
        )
        row_speed.connect(
            'notify::active',
            lambda w, _pspec: self.settings.set_boolean('multithreading', w.get_active())
        )
        row_notify.connect(
            'notify::active',
            lambda w, _pspec: self.settings.set_boolean('notify-on-complete', w.get_active())
        )

        prefs.present()

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f'app.{name}', shortcuts)


def main(version=None):
    # La inicialización del entorno visual ocurre cuando la identidad de proceso ya está registrada
    Adw.init()

    # Iconos locales
    display = Gdk.Display.get_default()
    if display:
        icon_theme = Gtk.IconTheme.get_for_display(display)
        project_icons = os.path.join(PROJECT_ROOT, 'data', 'icons')
        if os.path.exists(project_icons):
            icon_theme.add_search_path(project_icons)

    app = HifidvdmakerApplication()
    return app.run(sys.argv)
