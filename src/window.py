import os
import re
import tempfile
import threading
import shutil
import urllib.parse
import gettext

import cairo
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import (
    Adw,
    Gdk,
    GdkPixbuf,
    Gio,
    GLib,
    GObject,
    Gst,
    GstPbutils,
    Gtk,
)

from .dvd_engine import DVDEngine, ProcessCancelledError

_ = gettext.gettext
Gst.init(None)


@Gtk.Template(resource_path="/io/github/marcopcabrera/hifidvdmaker/window.ui")
class HiFiDvdMakerWindow(Adw.ApplicationWindow):
    __gtype_name__ = "HiFiDvdMakerWindow"

    btn_add_audio = Gtk.Template.Child()
    list_audio_files = Gtk.Template.Child()
    btn_select_cover = Gtk.Template.Child()
    row_portada = Gtk.Template.Child()
    preview_stack = Gtk.Template.Child()
    img_preview = Gtk.Template.Child()
    combo_sistema_tv = Gtk.Template.Child()
    combo_formato_audio = Gtk.Template.Child()
    combo_tipo_dvd = Gtk.Template.Child()
    switch_normalizar = Gtk.Template.Child()
    combo_tipo_salida = Gtk.Template.Child()
    btn_select_output = Gtk.Template.Child()
    row_destino = Gtk.Template.Child()
    btn_generar = Gtk.Template.Child()
    progress_espacio = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Establece el icono de la ventana para el dock y la barra de tareas
        self.set_icon_name("io.github.marcopcabrera.HiFiDvdMaker")

        # Instancia de GSettings
        self.settings = Gio.Settings.new("io.github.marcopcabrera.HiFiDvdMaker")

        self.audio_files = []
        self.cover_path = None
        self.output_path = None
        self.temp_preview_path = None
        self.progreso_win = None
        self.current_engine = None

        css_provider = Gtk.CssProvider()
        css_provider.load_from_string("""
            .preview-card {
                border-radius: 8px;
            }
            .preview-card picture,
            .preview-card image,
            .preview-card stack {
                border-radius: 8px;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Recorte nativo GTK4 para la vista previa
        if hasattr(self, "preview_stack") and self.preview_stack:
            self.preview_stack.set_overflow(Gtk.Overflow.HIDDEN)

        # Registrar las acciones de la ventana
        self.setup_window_actions()

        # Conectar clics de botones a sus acciones correspondientes
        self.btn_add_audio.connect(
            "clicked", lambda b: self.activate_action("win.add-audio", None)
        )
        self.btn_select_cover.connect(
            "clicked", lambda b: self.activate_action("win.select-cover", None)
        )
        self.btn_select_output.connect(
            "clicked", lambda b: self.activate_action("win.select-output", None)
        )
        self.btn_generar.connect(
            "clicked", lambda b: self.activate_action("win.generate-dvd", None)
        )

        self.combo_sistema_tv.connect(
            "notify::selected", self.on_sistema_tv_changed
        )
        self.combo_formato_audio.connect(
            "notify::selected", self.on_formato_audio_changed
        )
        self.combo_tipo_dvd.connect(
            "notify::selected", self.on_tipo_dvd_changed
        )
        self.combo_tipo_salida.connect(
            "notify::selected", self.on_tipo_salida_changed
        )

        self.switch_normalizar.set_active(False)

        # Cargar preferencia guardada del formato de audio
        formato_guardado = self.settings.get_string("audio-format")
        idx_inicial = 1 if formato_guardado == "ac3" else 0
        self.combo_formato_audio.set_selected(idx_inicial)

        self.actualizar_subtitulo_tv()
        self.on_formato_audio_changed(None, None)
        self.on_tipo_dvd_changed(None, None)
        self.on_tipo_salida_changed(None, None)
        self.actualizar_lista_audio()
        self.validar_requisitos_dvd()
        self.on_tipo_dvd_changed(self.combo_tipo_dvd, None)

    def set_audio_format_by_index(self, index):
        """Permite a main.py cambiar dinámicamente la opción si cambia en Preferencias."""
        if self.combo_formato_audio.get_selected() != index:
            self.combo_formato_audio.set_selected(index)

    def setup_window_actions(self):
        """Registra las acciones del ámbito de la ventana (win.*) para atajos."""
        action_add_audio = Gio.SimpleAction.new("add-audio", None)
        action_add_audio.connect(
            "activate", lambda a, p: self.on_add_audio_clicked(None)
        )
        self.add_action(action_add_audio)

        action_cover = Gio.SimpleAction.new("select-cover", None)
        action_cover.connect(
            "activate", lambda a, p: self.on_select_cover_clicked(None)
        )
        self.add_action(action_cover)

        action_output = Gio.SimpleAction.new("select-output", None)
        action_output.connect(
            "activate", lambda a, p: self.on_select_output_clicked(None)
        )
        self.add_action(action_output)

        action_generate = Gio.SimpleAction.new("generate-dvd", None)
        action_generate.connect(
            "activate", lambda a, p: self.on_generar_clicked(None)
        )
        self.add_action(action_generate)

        action_help_overlay = Gio.SimpleAction.new("show-help-overlay", None)
        action_help_overlay.connect("activate", self.on_show_help_overlay)
        self.add_action(action_help_overlay)

    def on_show_help_overlay(self, action, param):
        """Carga la interfaz desplegable desde shortcuts-dialog.ui."""
        try:
            builder = Gtk.Builder.new_from_resource(
                "/io/github/marcopcabrera/hifidvdmaker/shortcuts-dialog.ui"
            )
            shortcuts_win = builder.get_object("help_overlay")
            if shortcuts_win:
                shortcuts_win.set_title(_("Atajos de teclado"))
                shortcuts_win.set_transient_for(self)
                shortcuts_win.present()
        except Exception as e:
            print(f"Error al cargar shortcuts-dialog.ui: {e}")

    def on_tipo_salida_changed(self, combo, pspec):
        self.output_path = None
        self.row_destino.set_subtitle(_("No seleccionada"))
        self.validar_requisitos_dvd()

    def calcular_tamano_estimado_mb(self, duracion_total_segundos):
        if duracion_total_segundos <= 0:
            return 0.0

        es_ac3 = self.combo_formato_audio.get_selected() == 1

        # Bitrate total en kbps ajustado al motor DVDEngine:
        # Audio: LPCM (1536 kbps) o AC3 (448 kbps)
        # Video: Portada fija MPEG-2 (3000 kbps reales en lugar de 1200)
        bitrate_audio_kbps = 448.0 if es_ac3 else 1536.0
        bitrate_video_kbps = 2000.0 # cambiando este valor puedo agregar mas pistas

        bitrate_total_kbps = bitrate_audio_kbps + bitrate_video_kbps

        # MB por segundo = (kbps / 8) / 1024 = kbps / 8192
        mb_por_segundo = bitrate_total_kbps / 8192.0

        # Se añade un 2% de overhead por multiplexación MPEG-PS y UDF/IFO
        estimado = (duracion_total_segundos * mb_por_segundo) * 1.02

        return round(estimado, 1)

    def obtener_estado_disco(self):
        duracion_total = sum(
            item.get("duration", 0) for item in self.audio_files
        )
        es_dvd9 = self.combo_tipo_dvd.get_selected() == 1

        limite_mb = 8000.0 if es_dvd9 else 4390.0
        estimado_mb = (
            self.calcular_tamano_estimado_mb(duracion_total)
            if len(self.audio_files) > 0
            else 0.0
        )
        porcentaje = (estimado_mb / limite_mb) * 100 if limite_mb > 0 else 0
        excedido = estimado_mb > limite_mb

        return (
            estimado_mb,
            limite_mb,
            porcentaje,
            excedido,
            duracion_total,
            es_dvd9,
        )

    def validar_requisitos_dvd(self):
        estimado_mb, limite_mb, porcentaje, excedido, _, _ = (
            self.obtener_estado_disco()
        )

        tiene_audio = len(self.audio_files) > 0
        tiene_portada = self.cover_path is not None and os.path.exists(
            self.cover_path
        )
        destino_valido = self.output_path is not None

        se_puede_generar = (
            tiene_audio and tiene_portada and destino_valido and not excedido
        )
        self.btn_generar.set_sensitive(se_puede_generar)

    def actualizar_lista_audio(self):
        while child := self.list_audio_files.get_first_child():
            self.list_audio_files.remove(child)

        estimado_mb, limite_mb, porcentaje, excedido, duracion_total, es_dvd9 = (
            self.obtener_estado_disco()
        )

        fraccion = (
            min(estimado_mb / limite_mb, 1.0) if limite_mb > 0 else 0.0
        )
        self.progress_espacio.set_fraction(fraccion)

        peso_str = (
            f"{estimado_mb / 1024:.2f} GB"
            if estimado_mb >= 1000
            else f"{estimado_mb:.0f} MB"
        )
        limite_str = (
            f"{limite_mb / 1024:.1f} GB"
            if limite_mb >= 1000
            else f"{limite_mb:.0f} MB"
        )

        horas = duracion_total // 3600
        minutos = (duracion_total % 3600) // 60
        segundos = duracion_total % 60
        tiempo_str = (
            f"{horas:02d}:{minutos:02d}:{segundos:02d}"
            if horas > 0
            else f"{minutos:02d}:{segundos:02d}"
        )

        parent = self.progress_espacio.get_parent()
        while parent and not isinstance(parent, Adw.ActionRow):
            parent = parent.get_parent()

        if parent:
            porcentaje_str = f"{porcentaje:.1f}"

            if excedido:
                sub_texto = _("{peso} / {limite} ({porcentaje}%) — ⚠️ Excede la capacidad!").format(
                    peso=peso_str, limite=limite_str, porcentaje=porcentaje_str
                )
                parent.set_subtitle(sub_texto)
                parent.add_css_class("error")
            else:
                parent.remove_css_class("error")
                sub_texto = _("{peso} / {limite} ({porcentaje}%) — Duración: {tiempo}").format(
                    peso=peso_str, limite=limite_str, porcentaje=porcentaje_str, tiempo=tiempo_str
                )
                parent.set_subtitle(sub_texto)

        if not self.audio_files:
            empty_row = Adw.ActionRow()
            empty_row.set_activatable(False)

            box_empty = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=12
            )
            box_empty.set_halign(Gtk.Align.CENTER)
            box_empty.set_valign(Gtk.Align.CENTER)
            box_empty.set_margin_top(60)
            box_empty.set_margin_bottom(60)

            icon_empty = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
            icon_empty.set_pixel_size(96)
            icon_empty.add_css_class("dim-label")
            box_empty.append(icon_empty)

            texto_titulo = _("No hay pistas de audio")
            texto_subtitulo = _("Usa el botón '+' superior para agregar archivos")

            lbl_title = Gtk.Label()
            # Escapamos el texto para que caracteres como '&' no rompan el formato XML/Pango
            titulo_seguro = GLib.markup_escape_text(texto_titulo)
            lbl_title.set_markup(f"<b><span size='x-large'>{titulo_seguro}</span></b>")
            box_empty.append(lbl_title)

            lbl_subtitle = Gtk.Label(label=texto_subtitulo)
            lbl_subtitle.add_css_class("dim-label")
            box_empty.append(lbl_subtitle)

            empty_row.set_child(box_empty)
            self.list_audio_files.append(empty_row)
            return

        for index, item in enumerate(self.audio_files, start=1):
            row = Adw.ActionRow()
            nombre_archivo = os.path.basename(item["path"])

            # Escapamos el título y subtítulo para soportar '&', '<', '>' etc.
            titulo_bruto = f"{index}. {nombre_archivo}"
            row.set_title(GLib.markup_escape_text(titulo_bruto))
            row.set_title_lines(1)

            row.set_subtitle(GLib.markup_escape_text(item["subtitle"]))
            row.set_subtitle_lines(1)

            icon_drag = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            icon_drag.set_pixel_size(16)
            icon_drag.add_css_class("dim-label")
            row.add_prefix(icon_drag)

            btn_remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            btn_remove.set_valign(Gtk.Align.CENTER)
            btn_remove.add_css_class("flat")
            btn_remove.add_css_class("circular")
            btn_remove.add_css_class("destructive-action")
            btn_remove.connect(
                "clicked", self.on_remove_audio_clicked, item["path"]
            )
            row.add_suffix(btn_remove)

            row.set_cursor(Gdk.Cursor.new_from_name("grab", None))

            drag_source = Gtk.DragSource.new()
            drag_source.set_actions(Gdk.DragAction.MOVE)
            drag_source.connect("prepare", self._on_drag_prepare, index - 1)

            def _on_drag_begin(ds, drag, r=row):
                r.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))

            def _on_drag_end(ds, drag, delete, r=row):
                r.set_cursor(Gdk.Cursor.new_from_name("grab", None))

            drag_source.connect("drag-begin", _on_drag_begin)
            drag_source.connect("drag-end", _on_drag_end)
            row.add_controller(drag_source)

            drop_target = Gtk.DropTarget.new(int, Gdk.DragAction.MOVE)
            drop_target.connect("drop", self._on_drop, index - 1)
            row.add_controller(drop_target)

            self.list_audio_files.append(row)

    def _on_drag_prepare(self, drag_source, x, y, src_index):
        value = GObject.Value(int, src_index)
        return Gdk.ContentProvider.new_for_value(value)

    def _on_drop(self, drop_target, value, x, y, dest_index):
        src_index = value
        if src_index == dest_index or src_index is None:
            return False

        item = self.audio_files.pop(src_index)
        self.audio_files.insert(dest_index, item)

        self.actualizar_lista_audio()
        return True

    def generar_preview_tv(self, input_path):
        """Genera la vista previa en formato 16:9 con bordes redondeados recortados mediante Cairo."""
        try:
            pixbuf_orig = GdkPixbuf.Pixbuf.new_from_file(input_path)
            ancho = pixbuf_orig.get_width()
            alto = pixbuf_orig.get_height()
            ratio = ancho / alto

            target_w, target_h = 1024, 576

            target_pixbuf = GdkPixbuf.Pixbuf.new(
                GdkPixbuf.Colorspace.RGB, True, 8, target_w, target_h
            )
            target_pixbuf.fill(0x1A1A1AFF)

            if 1.70 <= ratio <= 1.85:
                scaled = pixbuf_orig.scale_simple(
                    target_w, target_h, GdkPixbuf.InterpType.BILINEAR
                )
                scaled.copy_area(
                    0, 0, target_w, target_h, target_pixbuf, 0, 0
                )
                estado_str = _("16:9 Ajuste perfecto")
            elif ratio > 1.7778:
                new_w = target_w
                new_h = int(target_w / ratio)
                scaled = pixbuf_orig.scale_simple(
                    new_w, new_h, GdkPixbuf.InterpType.BILINEAR
                )
                offset_y = (target_h - new_h) // 2
                scaled.copy_area(
                    0, 0, new_w, new_h, target_pixbuf, 0, offset_y
                )
                estado_str = _("Ajustado a la pantalla")
            else:
                new_h = target_h
                new_w = int(target_h * ratio)
                scaled = pixbuf_orig.scale_simple(
                    new_w, new_h, GdkPixbuf.InterpType.BILINEAR
                )
                offset_x = (target_w - new_w) // 2
                scaled.copy_area(
                    0, 0, new_w, new_h, target_pixbuf, offset_x, 0
                )
                estado_str = _("Ajustado a la pantalla")

            radio_esquina = 24
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, target_w, target_h
            )
            cr = cairo.Context(surface)

            degrees = 3.141592653589793 / 180.0
            cr.new_sub_path()
            cr.arc(
                target_w - radio_esquina,
                radio_esquina,
                radio_esquina,
                -90 * degrees,
                0 * degrees,
            )
            cr.arc(
                target_w - radio_esquina,
                target_h - radio_esquina,
                radio_esquina,
                0 * degrees,
                90 * degrees,
            )
            cr.arc(
                radio_esquina,
                target_h - radio_esquina,
                radio_esquina,
                90 * degrees,
                180 * degrees,
            )
            cr.arc(
                radio_esquina,
                radio_esquina,
                radio_esquina,
                180 * degrees,
                270 * degrees,
            )
            cr.close_path()
            cr.clip()

            Gdk.cairo_set_source_pixbuf(cr, target_pixbuf, 0, 0)
            cr.paint()

            rounded_pixbuf = Gdk.pixbuf_get_from_surface(
                surface, 0, 0, target_w, target_h
            )

            temp_dir = tempfile.gettempdir()
            self.temp_preview_path = os.path.join(
                temp_dir, "hifi_dvd_preview.png"
            )
            rounded_pixbuf.savev(self.temp_preview_path, "png", [], [])

            texture = Gdk.Texture.new_for_pixbuf(rounded_pixbuf)

            self.img_preview.set_size_request(240, 135)
            self.img_preview.set_visible(True)

            if isinstance(self.img_preview, Gtk.Picture):
                self.img_preview.set_can_shrink(True)
                if hasattr(Gtk, "ContentFit"):
                    self.img_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
                self.img_preview.set_paintable(texture)
            elif isinstance(self.img_preview, Gtk.Image):
                self.img_preview.set_from_paintable(texture)
            else:
                if hasattr(self.img_preview, "set_paintable"):
                    self.img_preview.set_paintable(texture)
                elif hasattr(self.img_preview, "set_from_paintable"):
                    self.img_preview.set_from_paintable(texture)

            if hasattr(self, "preview_stack") and self.preview_stack:
                self.preview_stack.set_visible_child_name("picture")

            self.row_portada.set_subtitle(f"{ancho}×{alto} px • {estado_str}")

        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"Error generando preview con Cairo: {e}")
            self.row_portada.set_subtitle(os.path.basename(input_path))

    def _aplicar_carpeta_inicial_dialogo(self, dialog):
        if self.settings.get_boolean("remember-location"):
            last_folder = self.settings.get_string("last-folder")
            if last_folder and os.path.exists(last_folder):
                folder_file = Gio.File.new_for_path(last_folder)
                dialog.set_initial_folder(folder_file)

    def _guardar_carpeta_seleccionada(self, file_or_folder):
        if self.settings.get_boolean("remember-location") and file_or_folder:
            path = file_or_folder.get_path()
            if path:
                folder_path = (
                    path if os.path.isdir(path) else os.path.dirname(path)
                )
                self.settings.set_string("last-folder", folder_path)

    def on_select_cover_clicked(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title(
            _("Seleccionar Portada para el DVD (Recomendado 1024×576)")
        )
        dialog.set_accept_label(_("Seleccionar"))  # <-- Línea añadida
        self._aplicar_carpeta_inicial_dialogo(dialog)

        filter_img = Gtk.FileFilter()
        filter_img.set_name(_("Imágenes (*.png, *.jpg, *.jpeg, *.webp)"))
        for mime in ["image/jpeg", "image/png", "image/webp"]:
            filter_img.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_img)
        dialog.set_filters(filters)
        dialog.open(self, None, self.on_cover_file_selected)

    def on_cover_file_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self._guardar_carpeta_seleccionada(file)
                self.cover_path = file.get_path()
                self.generar_preview_tv(self.cover_path)
                self.validar_requisitos_dvd()
        except GLib.Error:
            pass

    def on_add_audio_clicked(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Seleccionar archivos de audio"))
        self._aplicar_carpeta_inicial_dialogo(dialog)

        dialog.set_accept_label(_("Añadir"))

        filter_audio = Gtk.FileFilter()
        filter_audio.set_name(
            _("Archivos de audio (*.wav, *.flac, *.mp3, *.ogg, *.m4a)")
        )
        for mime in [
            "audio/x-wav",
            "audio/flac",
            "audio/mpeg",
            "audio/ogg",
            "audio/mp4",
        ]:
            filter_audio.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_audio)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self.on_audio_files_selected)

    def on_audio_files_selected(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
            if files and files.get_n_items() > 0:
                self._guardar_carpeta_seleccionada(files.get_item(0))
                rutas_existentes = [item["path"] for item in self.audio_files]
                for i in range(files.get_n_items()):
                    file_path = files.get_item(i).get_path()
                    if file_path:
                         subtitulo_info, duracion_seg = self.obtener_info_audio(
                             file_path
                         )
                         self.audio_files.append(
                             {
                                 "path": file_path,
                                 "subtitle": subtitulo_info,
                                 "duration": duracion_seg,
                             }
                         )

                self.actualizar_lista_audio()
                self.validar_requisitos_dvd()
        except GLib.Error:
            pass

    def on_remove_audio_clicked(self, button, path):
        self.audio_files = [
            item for item in self.audio_files if item["path"] != path
        ]
        self.actualizar_lista_audio()
        self.validar_requisitos_dvd()

    def obtener_info_audio(self, path):
        ext = os.path.splitext(path)[1].upper().replace(".", "") or "AUDIO"
        uri = Gst.filename_to_uri(path)
        discoverer = GstPbutils.Discoverer.new(Gst.SECOND * 2)
        total_segundos = 0
        artista = None
        album = None

        try:
            info = discoverer.discover_uri(uri)
            duration_ns = info.get_duration()
            if duration_ns > 0:
                total_segundos = duration_ns // Gst.SECOND

            tags = info.get_tags()
            if tags:
                success, val = tags.get_string(Gst.TAG_ARTIST)
                if success and val:
                    artista = val

                success, val = tags.get_string(Gst.TAG_ALBUM)
                if success and val:
                    album = val
        except Exception:
            pass

        minutos = (total_segundos % 3600) // 60
        segundos = total_segundos % 60
        horas = total_segundos // 3600
        tiempo_fmt = (
            f"{horas:02d}:{minutos:02d}:{segundos:02d}"
            if horas > 0
            else f"{minutos:02d}:{segundos:02d}"
        )

        partes = []
        if artista:
            partes.append(artista)
        if album:
            partes.append(album)

        texto_metadatos = " - ".join(partes)

        if texto_metadatos:
            subtitulo = f"{texto_metadatos} • {tiempo_fmt} • {ext}"
        else:
            subtitulo = f"{tiempo_fmt} • {ext}"

        return subtitulo, total_segundos

    def on_sistema_tv_changed(self, combo, pspec):
        self.actualizar_subtitulo_tv()

    def on_formato_audio_changed(self, combo, pspec):
        selected = self.combo_formato_audio.get_selected()
        if selected == 0:
            self.combo_formato_audio.set_subtitle(_("LPCM • 48 kHz / 16-bit"))
        else:
            self.combo_formato_audio.set_subtitle(
                _("AC3 • 448 kbps / Alta compatibilidad")
            )

        val_str = "ac3" if selected == 1 else "pcm"
        self.settings.set_string("audio-format", val_str)

        self.actualizar_lista_audio()
        self.validar_requisitos_dvd()

    def on_tipo_dvd_changed(self, combo, pspec):
        if self.combo_tipo_dvd.get_selected() == 0:
            self.combo_tipo_dvd.set_subtitle(_("4.7 GB / Capa simple"))
        else:
            self.combo_tipo_dvd.set_subtitle(_("8.5 GB / Doble capa"))

        self.actualizar_lista_audio()
        self.validar_requisitos_dvd()

    def actualizar_subtitulo_tv(self):
        if self.combo_sistema_tv.get_selected() == 0:
            self.combo_sistema_tv.set_subtitle(_("PAL (25 fps / 720x576)"))
        else:
            self.combo_sistema_tv.set_subtitle(_("NTSC (29.97 fps / 720x480)"))

    def on_select_output_clicked(self, button):
        dialog = Gtk.FileDialog()
        self._aplicar_carpeta_inicial_dialogo(dialog)

        if self.combo_tipo_salida.get_selected() == 0:
            dialog.set_title(_("Guardar Imagen ISO"))
            dialog.set_accept_label(_("Guardar"))
            dialog.set_initial_name("dvd_audio.iso")
            # ... resto del filtro iso ...
            dialog.save(self, None, self.on_output_file_selected)
        else:
            dialog.set_title(_("Seleccionar Carpeta de Salida"))
            dialog.set_accept_label(_("Seleccionar"))
            dialog.select_folder(self, None, self.on_output_file_selected)

    def on_output_file_selected(self, dialog, result):
        try:
            file = (
                dialog.save_finish(result)
                if self.combo_tipo_salida.get_selected() == 0
                else dialog.select_folder_finish(result)
            )
            if file:
                self._guardar_carpeta_seleccionada(file)
                self.output_path = file.get_path()

                nombre_archivo = file.get_basename()
                parent = file.get_parent()
                carpeta_padre = parent.get_basename() if parent else ""

                es_sandbox = (
                    carpeta_padre in ["doc", "by-app", "run", "user"]
                    or carpeta_padre.isdigit()
                    or (len(carpeta_padre) > 20 and " " not in carpeta_padre)
                )

                if carpeta_padre and not es_sandbox:
                    display_path = f"~/{carpeta_padre}/{nombre_archivo}"
                else:
                    display_path = f"~/{nombre_archivo}"

                self.row_destino.set_subtitle(display_path)
                self.validar_requisitos_dvd()
        except GLib.Error:
            pass

    def _crear_ventana_progreso(self):
        self._cancelando = False
        self.progreso_win = Gtk.Window(transient_for=self)
        self.progreso_win.set_title(_("Generando DVD..."))
        self.progreso_win.set_default_size(420, 210)
        self.progreso_win.set_modal(True)
        self.progreso_win.set_resizable(False)
        self.progreso_win.set_deletable(False)

        self.progreso_win.connect(
            "close-request", self._on_progreso_close_request
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        self.progreso_win.set_child(box)

        self.lbl_progreso_estado = Gtk.Label(label=_("Iniciando proceso..."))
        self.lbl_progreso_estado.set_xalign(0.0)
        box.append(self.lbl_progreso_estado)

        self.barra_progreso_modal = Gtk.ProgressBar()
        self.barra_progreso_modal.set_show_text(True)
        self.barra_progreso_modal.set_text("0%")
        box.append(self.barra_progreso_modal)

        self.btn_cancelar_proceso = Gtk.Button(label=_("Cancelar"))
        self.btn_cancelar_proceso.add_css_class("destructive-action")
        self.btn_cancelar_proceso.set_halign(Gtk.Align.CENTER)
        self.btn_cancelar_proceso.connect(
            "clicked", self.on_cancelar_proceso_clicked
        )
        box.append(self.btn_cancelar_proceso)

        self._progreso_actual_visual = 0.0
        self._progreso_objetivo = 0.0
        self._mensaje_actual = _("Iniciando proceso...")

        self._timeout_animacion = GLib.timeout_add(30, self._animar_barra_paso)
        self.progreso_win.present()

    def _on_progreso_close_request(self, window):
        if not getattr(self, "_cancelando", False):
            self.on_cancelar_proceso_clicked(None)
        return True

    def on_cancelar_proceso_clicked(self, button):
        if getattr(self, "_cancelando", False):
            return

        # Desactivar botón en el acto
        if hasattr(self, "btn_cancelar_proceso") and self.btn_cancelar_proceso:
            self.btn_cancelar_proceso.set_sensitive(False)

        dialog = Adw.MessageDialog(
            transient_for=self.progreso_win,
            heading=_("¿Cancelar proceso?"),
            body=_("Se detendrá la creación del DVD y se eliminarán los archivos temporales generados."),
        )
        dialog.add_response("no", _("Continuar"))
        dialog.add_response("yes", _("Sí, cancelar"))
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self.on_confirmacion_cancelar_response)
        dialog.present()

    def on_confirmacion_cancelar_response(self, dialog, response):
        dialog.destroy()
        if response == "yes":
            self._cancelando = True
            # Notificar al usuario visualmente mientras se limpian subprocesos
            if hasattr(self, "lbl_progreso_estado") and self.lbl_progreso_estado:
                self.lbl_progreso_estado.set_label(_("Cancelando y limpiando..."))

            # Ejecutar la detención en segundo plano para no congelar la UI
            threading.Thread(target=self.detener_proceso_y_limpiar, daemon=True).start()
        else:
            if hasattr(self, "btn_cancelar_proceso") and self.btn_cancelar_proceso:
                self.btn_cancelar_proceso.set_sensitive(True)

    def detener_proceso_y_limpiar(self):
        # 1. Cancelar en el motor
        if hasattr(self, "current_engine") and self.current_engine:
            if hasattr(self.current_engine, "cancel"):
                try:
                    self.current_engine.cancel()
                except Exception as e:
                    print(f"Error al cancelar motor: {e}")

        # 2. Eliminar ISO inconclusa
        if self.output_path:
            output_real = os.path.expanduser(self.output_path)
            try:
                if os.path.isfile(output_real) and output_real.lower().endswith(".iso"):
                    os.remove(output_real)
            except Exception:
                pass

        # 3. Eliminar carpetas temporales
        posibles_rutas = [os.path.join(os.getcwd(), "DVD_FINAL")]
        if self.output_path:
            output_real = os.path.expanduser(self.output_path)
            base = output_real if os.path.isdir(output_real) else os.path.dirname(output_real)
            posibles_rutas.append(os.path.join(base, "DVD_FINAL"))

        for carpeta in posibles_rutas:
            try:
                if os.path.exists(carpeta) and os.path.isdir(carpeta):
                    shutil.rmtree(carpeta, ignore_errors=True)
            except Exception:
                pass

        # 4. Restablecer UI en el hilo principal
        def _restaurar_ui():
            self._cerrar_ventana_progreso()
            self.btn_generar.set_sensitive(True)
            self.btn_generar.set_label(_("Crear DVD"))
            return False

        GLib.idle_add(_restaurar_ui)

    def _animar_barra_paso(self):
        if not hasattr(self, "barra_progreso_modal") or not self.progreso_win:
            return False

        diff = self._progreso_objetivo - self._progreso_actual_visual
        if abs(diff) > 0.001:
            self._progreso_actual_visual += diff * 0.15
        else:
            self._progreso_actual_visual = self._progreso_objetivo

        fraccion = min(max(self._progreso_actual_visual, 0.0), 1.0)
        self.barra_progreso_modal.set_fraction(fraccion)
        self.barra_progreso_modal.set_text(f"{int(fraccion * 100)}%")
        self.lbl_progreso_estado.set_label(self._mensaje_actual)

        return True

    def _actualizar_progreso_modal(self, mensaje, porcentaje):
        # Ignorar actualizaciones si el proceso fue cancelado
        if getattr(self, "_cancelando", False):
            return False

        if hasattr(self, "barra_progreso_modal") and self.progreso_win:
            self._progreso_objetivo = float(porcentaje)
            self._mensaje_actual = mensaje
        return False

    def _cerrar_ventana_progreso(self):
        if hasattr(self, "_timeout_animacion"):
            GLib.source_remove(self._timeout_animacion)
            del self._timeout_animacion

        if hasattr(self, "progreso_win") and self.progreso_win:
            self.progreso_win.destroy()
            self.progreso_win = None
        return False

    def on_generar_clicked(self, button):
        if not self.btn_generar.get_sensitive():
            return

        self.btn_generar.set_sensitive(False)
        self.btn_generar.set_label(_("Procesando..."))

        GLib.idle_add(self._crear_ventana_progreso)

        tv_system = (
            "PAL" if self.combo_sistema_tv.get_selected() == 0 else "NTSC"
        )
        audio_format = (
            "ac3" if self.combo_formato_audio.get_selected() == 1 else "pcm"
        )
        normalize = self.switch_normalizar.get_active()
        is_iso = self.combo_tipo_salida.get_selected() == 0

        use_multithreading = self.settings.get_boolean("multithreading")

        output_path_real = (
            os.path.expanduser(self.output_path) if self.output_path else None
        )
        archivos_audio = [item["path"] for item in self.audio_files]

        def proceso_hilo():
            try:
                self.current_engine = DVDEngine(
                    tv_system=tv_system,
                    normalize_audio=normalize,
                    audio_format=audio_format,
                    multithreading=use_multithreading,
                )
                portada = self.cover_path

                def callback_motor(mensaje, porcentaje):
                    GLib.idle_add(
                        self._actualizar_progreso_modal, mensaje, porcentaje
                    )

                self.current_engine.crear_dvd(
                    audio_files=archivos_audio,
                    cover_image_path=portada,
                    output_path=output_path_real,
                    is_iso=is_iso,
                    progress_callback=callback_motor,
                    app=self.get_application(),
                    window=self,
                )

                if not getattr(self, "_cancelando", False):
                    GLib.idle_add(self._on_proceso_finalizado, True, None)
            except ProcessCancelledError:
                print("Proceso cancelado correctamente.")
            except Exception as e:
                if not getattr(self, "_cancelando", False):
                    import traceback
                    traceback.print_exc()
                    GLib.idle_add(self._on_proceso_finalizado, False, str(e))

        threading.Thread(target=proceso_hilo, daemon=True).start()

    def _on_proceso_finalizado(self, exito, error_msg):
        # Si fue cancelado previamente, omitir cualquier alerta de fin/error
        if getattr(self, "_cancelando", False):
            return False

        GLib.idle_add(self._cerrar_ventana_progreso)

        self.btn_generar.set_sensitive(True)
        self.btn_generar.set_label(_("Crear DVD"))

        if self.settings.get_boolean("notify-on-complete") and not self.is_active():
            try:
                notif = Gio.Notification.new("HiFiDvdMaker")
                msg = _("El DVD fue generado correctamente.") if exito else f"{_('Error:')} {error_msg}"
                notif.set_body(msg)
                self.get_application().send_notification("dvd-created", notif)
            except Exception as e:
                print(f"Error al enviar notificación: {e}")

        mensaje_principal = _("Proceso Completado") if exito else _("Error en la Generación")
        mensaje_secundario = (
            _("El DVD fue generado correctamente.")
            if exito
            else f"{_('Ocurrió un error al procesar el DVD:')}\n{error_msg}"
        )

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO if exito else Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=mensaje_principal,
            secondary_text=mensaje_secundario,
        )
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.present()
        return False
