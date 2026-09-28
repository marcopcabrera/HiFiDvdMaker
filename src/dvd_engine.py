#!/usr/bin/env python3
"""
DVD Engine - Motor de autoría de DVD de Audio HiFi.
Crea un VOB independiente por cada pista dentro de un PGC único para navegación
nativa de capítulos y compila la estructura a ISO con genisoimage/mkisofs.
"""

import gettext
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile

# Intentar importar GTK / GIO nativo para manejar inhibición de energía en Flatpak
try:
    from gi.repository import Gio, GLib, Gtk

    HAS_GTK = True
except ImportError:
    HAS_GTK = False

# Configuración de gettext para el motor
_ = gettext.gettext

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


class ProcessCancelledError(Exception):
    """Excepción específica para interrupciones por parte del usuario."""

    pass


class InhibitManager:
    """Gestor híbrido de inhibición de suspensión/idle.

    Soporta Gtk.Application (Portales Flatpak), ScreenSaver D-Bus y
    systemd-inhibit.
    """

    def __init__(self, reason=None, app=None, window=None):
        self.reason = reason if reason else _("Creando disco DVD HiFi...")
        self.app = app
        self.window = window
        self.cookie = None
        self.subproc = None

    def __enter__(self):
        # 1. Utilizar Gtk.Application si está disponible (Integración nativa con Portales Flatpak)
        if HAS_GTK and self.app and isinstance(self.app, Gtk.Application):
            try:
                flags = (
                    Gtk.ApplicationInhibitFlags.SUSPEND
                    | Gtk.ApplicationInhibitFlags.IDLE
                )
                self.cookie = self.app.inhibit(self.window, flags, self.reason)
                logging.info(
                    f"Inhibición activada mediante Gtk.Application (Cookie: {self.cookie})"
                )
                return self
            except Exception as e:
                logging.warning(f"Error al inhibir vía Gtk.Application: {e}")

        # 2. Respaldo directo a org.freedesktop.ScreenSaver vía D-Bus
        if HAS_GTK:
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                reply = bus.call_sync(
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver",
                    "Inhibit",
                    GLib.Variant("(ss)", ("HiFiDVDMaker", self.reason)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                self.cookie = reply.unpack()[0]
                logging.info(
                    f"Inhibición activada mediante ScreenSaver D-Bus (Cookie: {self.cookie})"
                )
                return self
            except Exception as e:
                logging.warning(f"Error al inhibir vía ScreenSaver D-Bus: {e}")

        # 3. Respaldo para ejecuciones nativas fuera de Flatpak (CLI)
        if shutil.which("systemd-inhibit"):
            try:
                self.subproc = subprocess.Popen([
                    "systemd-inhibit",
                    f"--why={self.reason}",
                    "--what=sleep:idle",
                    "sleep",
                    "infinity",
                ])
                logging.info("Inhibición activada mediante systemd-inhibit.")
            except Exception as e:
                logging.warning(f"No se pudo iniciar systemd-inhibit: {e}")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Liberar inhibición de Gtk.Application
        if (
            self.cookie
            and HAS_GTK
            and self.app
            and isinstance(self.app, Gtk.Application)
        ):
            try:
                self.app.uninhibit(self.cookie)
                logging.info("Inhibición de Gtk.Application liberada.")
                return
            except Exception as e:
                logging.warning(
                    f"Error al liberar Gtk.Application.inhibit: {e}"
                )

        # Liberar inhibición de ScreenSaver D-Bus
        if self.cookie and HAS_GTK:
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                bus.call_sync(
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver",
                    "UnInhibit",
                    GLib.Variant("(u)", (self.cookie,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                logging.info("Inhibición de ScreenSaver D-Bus liberada.")
            except Exception:
                pass

        # Liberar subproceso systemd-inhibit si se usó
        if self.subproc:
            try:
                self.subproc.terminate()
                self.subproc.wait(timeout=2)
                logging.info("Inhibición de systemd-inhibit liberada.")
            except Exception:
                pass


class DVDEngine:
    """Motor de conversión de Audio HiFi a DVD con navegación por capítulos nativa."""

    def __init__(
        self,
        tv_system="PAL",
        normalize_audio=False,
        audio_format="pcm",
        multithreading=True,
        dvd_type="dvd5",
    ):
        self.tv_system = tv_system.upper()
        self.normalize_audio = normalize_audio
        self.audio_format = str(audio_format).lower()  # "pcm" o "ac3"
        self.multithreading = multithreading
        self.dvd_type = dvd_type.lower()  # "dvd5" o "dvd9"
        self.log_file = os.path.join(
            os.path.expanduser("~"), "dvd_process_error.log"
        )

        # Control de cancelación
        self.is_cancelled = False
        self.current_process = None

        if self.tv_system == "PAL":
            self.fps = "25"
            self.resolution = "720x576"
            self.vid_height = "576"
            self.vid_width = "720"
            self.sar = "64/45"
            self.gop_size = "15"
        else:
            self.fps = "30000/1001"
            self.resolution = "720x480"
            self.vid_height = "480"
            self.vid_width = "720"
            self.sar = "8/9"
            self.gop_size = "18"

    def cancel(self):
        """Mata inmediatamente el subproceso actual y cancela la cola completa."""
        self.is_cancelled = True
        if self.current_process and self.current_process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        [
                            "taskkill",
                            "/F",
                            "/T",
                            "/PID",
                            str(self.current_process.pid),
                        ],
                        capture_output=True,
                    )
                else:
                    os.killpg(
                        os.getpgid(self.current_process.pid), signal.SIGTERM
                    )
            except Exception:
                try:
                    self.current_process.kill()
                except Exception:
                    pass

    def calcular_tamano_estimado_bytes(self, duracion_total_segundos):
        """Calcula el tamaño exacto estimado de la ISO/VIDEO_TS en bytes.

        Basado en las tasas de bits reales asignadas al multiplexador.
        """
        # Vídeo ajustado a 2000 kbps constante (250,000 bytes por segundo para portada estática)
        bytes_video = duracion_total_segundos * 250_000

        if self.audio_format == "ac3":
            # AC3 a 448 kbps (56,000 bytes por segundo)
            bytes_audio = duracion_total_segundos * 56_000
        else:
            # LPCM 16-bit / 48kHz Estéreo sin compresión a 1536 kbps (192,000 bytes por segundo)
            bytes_audio = duracion_total_segundos * 192_000

        # Overhead del contenedor MPEG-PS, tablas IFO/BUP de dvdauthor y filesystem ISO9660/UDF (2%)
        return int((bytes_video + bytes_audio) * 1.02)

    @staticmethod
    def formatear_tamano(bytes_num):
        """Convierte bytes a un string formateado comprensible (MB / GB)."""
        if bytes_num < 1024 * 1024 * 1024:
            return f"{bytes_num / (1024 * 1024):.2f} MB"
        return f"{bytes_num / (1024 * 1024 * 1024):.2f} GB"

    def obtener_analisis_proyecto(self, audio_files):
        """Analiza la lista de audios con ffprobe y calcula el tamaño exacto y el tipo de DVD requerido."""
        if not audio_files:
            return {
                "duracion_segundos": 0.0,
                "tamano_bytes": 0,
                "tamano_formateado": "0.00 MB",
                "tipo_dvd_recomendado": "DVD-5",
                "requiere_dvd9": False,
                "excede_maximo": False,
            }

        # Extracción exacta de duraciones reales vía ffprobe
        duracion_total = sum(
            self._obtener_duracion_audio(f) for f in audio_files
        )
        tamano_bytes = self.calcular_tamano_estimado_bytes(duracion_total)

        # Capacidades físicas reales de discos comerciales (Bytes)
        LIMIT_DVD5 = 4_700_000_000  # ~4.37 GiB
        LIMIT_DVD9 = 8_540_000_000  # ~7.95 GiB

        requiere_dvd9 = tamano_bytes > LIMIT_DVD5
        excede_dvd9 = tamano_bytes > LIMIT_DVD9

        if excede_dvd9:
            tipo_recomendado = "EXCEDE_LIMITES"
        elif requiere_dvd9:
            tipo_recomendado = "DVD-9"
        else:
            tipo_recomendado = "DVD-5"

        return {
            "duracion_segundos": duracion_total,
            "tamano_bytes": tamano_bytes,
            "tamano_formateado": self.formatear_tamano(tamano_bytes),
            "tipo_dvd_recomendado": tipo_recomendado,
            "requiere_dvd9": requiere_dvd9,
            "excede_maximo": excede_dvd9,
        }

    def validar_capacidad_disco(self, audio_files):
        """Verifica que el proyecto no supere la capacidad del soporte físico objetivo elegido."""
        analisis = self.obtener_analisis_proyecto(audio_files)

        LIMIT_DVD5 = 4_700_000_000
        LIMIT_DVD9 = 8_540_000_000
        limite_bytes = LIMIT_DVD9 if self.dvd_type == "dvd9" else LIMIT_DVD5

        if analisis["tamano_bytes"] > limite_bytes:
            horas = int(analisis["duracion_segundos"] // 3600)
            minutos = int((analisis["duracion_segundos"] % 3600) // 60)

            raise ValueError(
                _(
                    "El contenido ({horas}h {minutos}m) supera la capacidad máxima del {tipo}.\n"
                    "Tamaño estimado: {est} | Límite disponible: {lim}.\n"
                    "Sugerencia: Cambie el formato de audio a AC3 o cambie a DVD-9 de Doble Capa."
                ).format(
                    horas=horas,
                    minutos=minutos,
                    tipo=self.dvd_type.upper(),
                    est=analisis["tamano_formateado"],
                    lim=self.formatear_tamano(limite_bytes),
                )
            )
        return True

    def _ejecutar_comando(self, cmd, env=None):
        """Ejecuta comandos del sistema monitoreando banderas de cancelación."""
        if self.is_cancelled:
            raise ProcessCancelledError(_("Proceso cancelado por el usuario."))

        kwargs = {}
        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid

        self.current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            **kwargs,
        )
        stdout, stderr = self.current_process.communicate()

        if self.is_cancelled:
            raise ProcessCancelledError(_("Proceso cancelado por el usuario."))

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=self.current_process.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def verificar_herramientas(self):
        """Verifica que los binarios externos existan en el sistema."""
        herramientas = ["ffmpeg", "ffprobe", "dvdauthor"]
        faltantes = [h for h in herramientas if shutil.which(h) is None]

        if not self._obtener_herramienta_iso():
            faltantes.append("genisoimage/mkisofs (cdrkit)")

        if faltantes:
            raise RuntimeError(
                _(
                    "Faltan herramientas necesarias en el sistema: {herramientas}"
                ).format(herramientas=", ".join(faltantes))
            )

        return True

    def _obtener_herramienta_iso(self):
        """Retorna la ruta de genisoimage o mkisofs real, descartando xorriso."""
        for tool in ["genisoimage", "mkisofs"]:
            path = shutil.which(tool)
            if path and "xorriso" not in os.path.realpath(path):
                return tool
        return None

    def _obtener_duracion_audio(self, audio_path):
        """Obtiene la duración exacta en segundos del archivo de audio usando ffprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ]
        try:
            res = self._ejecutar_comando(cmd)
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    def _obtener_dimensiones_imagen(self, image_path):
        """Obtiene el ancho y alto de la imagen de portada."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            image_path,
        ]
        res = self._ejecutar_comando(cmd)
        try:
            w, h = res.stdout.strip().split("x")
            return int(w), int(h)
        except Exception:
            return 720, 576

    def _optimizar_portada(self, cover_path, temp_dir):
        """Prepara la portada conservando nitidez extrema sin degradación."""
        opt_cover_path = os.path.join(temp_dir, "cover_optimized.jpg")
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            cover_path,
            "-vf",
            f"scale=-1:{self.vid_height}:flags=lanczos",
            "-q:v",
            "1",
            opt_cover_path,
        ]
        res = self._ejecutar_comando(cmd)
        if res.returncode != 0:
            if (
                "No space left on device" in res.stderr
                or "ENOSPC" in res.stderr
            ):
                raise RuntimeError(
                    _(
                        "No hay suficiente espacio libre en disco para completar la operación."
                    )
                )
            raise RuntimeError(
                _("Fallo al optimizar la portada: {error}").format(
                    error=res.stderr
                )
            )
        return opt_cover_path

    def _ejecutar_ffmpeg_con_progreso(
        self,
        cmd,
        duracion_total,
        offset_base,
        escala_pista,
        texto_pista,
        progress_callback,
    ):
        """Ejecuta FFmpeg leyendo stderr en tiempo real para calcular el progreso fluido."""
        if self.is_cancelled:
            raise ProcessCancelledError(_("Proceso cancelado por el usuario."))

        kwargs = {}
        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid

        self.current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            universal_newlines=True,
            **kwargs,
        )

        stderr_log = []
        time_regex = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")

        while True:
            if self.is_cancelled:
                self.cancel()
                raise ProcessCancelledError(
                    _("Proceso cancelado por el usuario.")
                )

            line = self.current_process.stderr.readline()
            if not line and self.current_process.poll() is not None:
                break

            if line:
                stderr_log.append(line)
                match = time_regex.search(line)
                if match and duracion_total > 0 and progress_callback:
                    h, m, s = map(float, match.groups())
                    segundos_actuales = h * 3600 + m * 60 + s
                    progreso_pista = min(
                        segundos_actuales / duracion_total, 1.0
                    )

                    progreso_global = offset_base + (
                        progreso_pista * escala_pista
                    )
                    progress_callback(
                        f"{texto_pista} ({int(progreso_pista * 100)}%)",
                        progreso_global,
                    )

        rc = self.current_process.poll()
        stderr_text = "".join(stderr_log)

        return subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout="", stderr=stderr_text
        )

    def _generar_xml_dvdauthor(
        self,
        audio_files,
        cover_path,
        temp_dir,
        xml_path,
        progress_callback=None,
    ):
        """Codifica cada archivo de audio por separado a VOB con monitoreo continuo."""
        w, h = self._obtener_dimensiones_imagen(cover_path)
        res_fmt = self.resolution.replace("x", ":")

        # Filtro de escala Lanczos + YUV420P + Deflicker para eliminar parpadeo y borrosidad
        if w > h:
            filter_v = (
                f"scale={res_fmt}:flags=lanczos,format=yuv420p,deflicker=s=2,"
                f"setsar={self.sar},setdar=16/9,fps={self.fps}"
            )
        else:
            if self.vid_height == "576":
                filter_v = (
                    f"scale=405:576:flags=lanczos,pad=720:576:(720-405)/2:0:color=black,"
                    f"format=yuv420p,deflicker=s=2,setsar={self.sar},setdar=16/9,fps={self.fps}"
                )
            else:
                filter_v = (
                    f"scale=360:480:flags=lanczos,pad=720:480:(720-360)/2:0:color=black,"
                    f"format=yuv420p,deflicker=s=2,setsar={self.sar},setdar=16/9,fps={self.fps}"
                )

        if self.normalize_audio:
            filter_a = "adelay=500|500,loudnorm=I=-16:TP=-1.0:LRA=11,aformat=sample_fmts=s16:sample_rates=48000"
        else:
            filter_a = (
                "adelay=500|500,aformat=sample_fmts=s16:sample_rates=48000"
            )

        vob_files = []
        total_pistas = len(audio_files)

        if self.audio_format == "ac3":
            codec_args = ["-c:a", "ac3", "-b:a", "448k"]
            texto_fmt = "AC3"
        else:
            codec_args = ["-c:a", "pcm_s16be"]
            texto_fmt = "LPCM"

        escala_por_pista = 0.78 / total_pistas if total_pistas > 0 else 0.78

        for idx, audio in enumerate(audio_files):
            if self.is_cancelled:
                raise ProcessCancelledError(
                    _("Proceso cancelado por el usuario.")
                )

            pista_num = idx + 1
            vob_output = os.path.join(temp_dir, f"track_{pista_num:03d}.mpg")
            duracion_pista = self._obtener_duracion_audio(audio)
            offset_base = 0.02 + (idx * escala_por_pista)
            texto_pista = _(
                "Codificando Pista {actual}/{total} ({formato})"
            ).format(actual=pista_num, total=total_pistas, formato=texto_fmt)

            cmd = (
                [
                    "ffmpeg",
                    "-y",
                    "-nostdin",
                    "-hide_banner",
                    "-loop",
                    "1",
                    "-i",
                    cover_path,
                    "-i",
                    audio,
                    "-vf",
                    filter_v,
                    "-af",
                    filter_a,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "mpeg2video",
                    "-b:v",
                    "2000k",
                    "-maxrate:v",
                    "2000k",
                    "-bufsize:v",
                    "1835k",
                    "-g",
                    self.gop_size,
                    "-bf",
                    "0",
                    "-aspect",
                    "16:9",
                ]
                + codec_args
                + [
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-map_metadata",
                    "-1",
                    "-f",
                    "dvd",
                    "-shortest",
                    vob_output,
                ]
            )

            res = self._ejecutar_ffmpeg_con_progreso(
                cmd=cmd,
                duracion_total=duracion_pista,
                offset_base=offset_base,
                escala_pista=escala_por_pista,
                texto_pista=texto_pista,
                progress_callback=progress_callback,
            )

            with open(self.log_file, "a", encoding="utf-8") as log:
                log.write(f"\n--- CODIFICANDO PISTA {pista_num} ---\n")
                log.write(res.stderr)

            if res.returncode != 0:
                if (
                    "No space left on device" in res.stderr
                    or "ENOSPC" in res.stderr
                ):
                    raise RuntimeError(
                        _(
                            "No hay suficiente espacio libre en disco para completar la operación."
                        )
                    )
                raise RuntimeError(
                    _(
                        "Fallo al codificar la pista {pista_num}:\n{error}"
                    ).format(pista_num=pista_num, error=res.stderr[-500:])
                )

            vob_files.append(vob_output)

        if self.is_cancelled:
            raise ProcessCancelledError(_("Proceso cancelado por el usuario."))

        if progress_callback:
            progress_callback(
                _("Escribiendo tabla de capítulos nativa..."), 0.82
            )

        lines = [
            "<dvdauthor>",
            "  <vmgm />",
            "  <titleset>",
            "    <titles>",
            "      <pgc>",
        ]

        for vob in vob_files:
            clean_vob_path = vob.replace("\\", "/")
            lines.append(
                f'        <vob file="{clean_vob_path}" chapters="00:00:00.000" />'
            )

        lines.extend([
            "        <post>jump title 1 chapter 1;</post>",
            "      </pgc>",
            "    </titles>",
            "  </titleset>",
            "</dvdauthor>",
        ])

        with open(xml_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _crear_iso(self, source_dir, output_iso):
        """Compila la estructura VIDEO_TS/AUDIO_TS a archivo ISO."""
        video_ts_dir = os.path.join(source_dir, "VIDEO_TS")
        audio_ts_dir = os.path.join(source_dir, "AUDIO_TS")
        os.makedirs(video_ts_dir, exist_ok=True)
        os.makedirs(audio_ts_dir, exist_ok=True)

        parent_dir = os.path.dirname(output_iso)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        raw_label = os.path.splitext(os.path.basename(output_iso))[0]
        vol_label = re.sub(r"[^A-Za-z0-9_]", "_", raw_label)[:32] or "HIFI_DVD"

        if os.path.exists(output_iso):
            try:
                os.remove(output_iso)
            except OSError:
                pass

        iso_tool = self._obtener_herramienta_iso()
        if not iso_tool:
            raise FileNotFoundError(
                _(
                    "No se encontró 'genisoimage' ni 'mkisofs' real en el sistema."
                )
            )

        cmd = [
            iso_tool,
            "-dvd-video",
            "-udf",
            "-V",
            vol_label,
            "-o",
            output_iso,
            source_dir,
        ]

        res = self._ejecutar_comando(cmd)

        with open(self.log_file, "a", encoding="utf-8") as log:
            log.write(f"\n--- EJECUCIÓN {iso_tool} ---\n")
            log.write(res.stdout)
            log.write(res.stderr)

        if res.returncode != 0:
            stderr_clean = res.stderr.strip() or res.stdout.strip()
            if (
                "No space left on device" in stderr_clean
                or "ENOSPC" in stderr_clean
            ):
                raise RuntimeError(
                    _(
                        "No hay suficiente espacio libre en disco para completar la operación."
                    )
                )
            error_tail = "\n".join(stderr_clean.splitlines()[-8:])
            raise RuntimeError(
                _(
                    "Fallo al compilar la ISO con {herramienta}.\nDetalle:\n{detalle}"
                ).format(herramienta=iso_tool, detalle=error_tail)
            )

    def procesar_proyecto(
        self,
        audio_files,
        cover_path,
        output_dir,
        iso_name=None,
        progress_callback=None,
    ):
        """Pipeline principal de generación del proyecto DVD."""
        self.is_cancelled = False
        self.verificar_herramientas()

        if not os.path.exists(cover_path):
            raise FileNotFoundError(
                _("Portada no encontrada: {ruta}").format(ruta=cover_path)
            )

        if not audio_files:
            raise ValueError(_("No se especificaron archivos de audio."))

        # Validación estricta previa de límites físicos
        self.validar_capacidad_disco(audio_files)

        if os.path.exists(self.log_file):
            try:
                os.remove(self.log_file)
            except OSError:
                pass

        base_cache = os.path.expanduser("~/.cache/hifidvdmaker")
        os.makedirs(base_cache, exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix="hifi_dvd_engine_", dir=base_cache)

        try:
            if progress_callback:
                progress_callback(_("Optimizando imagen de portada..."), 0.01)
            opt_cover_path = self._optimizar_portada(cover_path, temp_dir)

            xml_path = os.path.join(temp_dir, "dvdauthor.xml")
            self._generar_xml_dvdauthor(
                audio_files,
                opt_cover_path,
                temp_dir,
                xml_path,
                progress_callback,
            )

            target_dir = (
                os.path.abspath(output_dir) if output_dir else os.getcwd()
            )
            dvd_root_dir = os.path.join(temp_dir, "dvd_structure")
            os.makedirs(dvd_root_dir, exist_ok=True)

            if progress_callback:
                progress_callback(
                    _("Compilando estructura VIDEO_TS con dvdauthor..."), 0.85
                )

            env = os.environ.copy()
            env["VIDEO_FORMAT"] = self.tv_system.upper()

            cmd_dvdauthor = ["dvdauthor", "-o", dvd_root_dir, "-x", xml_path]
            res_dvd = self._ejecutar_comando(cmd_dvdauthor, env=env)

            with open(self.log_file, "a", encoding="utf-8") as log:
                log.write("\n--- EJECUCIÓN DVDAUTHOR ---\n")
                log.write(res_dvd.stdout)
                log.write(res_dvd.stderr)

            if res_dvd.returncode != 0:
                detalles = res_dvd.stderr.strip() or res_dvd.stdout.strip()
                if (
                    "No space left on device" in detalles
                    or "ENOSPC" in detalles
                ):
                    raise RuntimeError(
                        _(
                            "No hay suficiente espacio libre en disco para completar la operación."
                        )
                    )
                raise RuntimeError(
                    _(
                        "dvdauthor falló al construir la estructura:\n{detalle}"
                    ).format(detalle=detalles)
                )

            if iso_name:
                if progress_callback:
                    progress_callback(
                        _("Generando ISO... (puede tardar unos minutos)"), 0.90
                    )

                iso_path = (
                    iso_name
                    if os.path.isabs(iso_name)
                    else os.path.join(target_dir, iso_name)
                )
                if not iso_path.endswith(".iso"):
                    iso_path += ".iso"

                self._crear_iso(dvd_root_dir, iso_path)

                final_target = os.path.join(target_dir, "DVD_FINAL")
                if os.path.exists(final_target):
                    shutil.rmtree(final_target, ignore_errors=True)
            else:
                if progress_callback:
                    progress_callback(
                        _("Guardando directorio VIDEO_TS..."), 0.95
                    )

                final_target = os.path.join(target_dir, "DVD_FINAL")
                if os.path.exists(final_target):
                    shutil.rmtree(final_target, ignore_errors=True)

                shutil.copytree(
                    os.path.join(dvd_root_dir, "VIDEO_TS"),
                    os.path.join(final_target, "VIDEO_TS"),
                )

            if progress_callback:
                progress_callback(_("¡Proceso completado con éxito!"), 1.0)

            return True

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def crear_dvd(
        self,
        audio_files=None,
        cover_image_path=None,
        output_path=None,
        is_iso=True,
        progress_callback=None,
        audio_path=None,
        app=None,
        window=None,
    ):
        """Punto de entrada estándar e interfaz compatible con la GUI."""
        lista_entrada = audio_files or audio_path

        if not lista_entrada:
            raise ValueError(_("No se especificaron archivos de audio."))

        if isinstance(lista_entrada, str):
            archivos = [lista_entrada]
        elif (
            isinstance(lista_entrada, list)
            and len(lista_entrada) > 0
            and isinstance(lista_entrada[0], dict)
        ):
            archivos = [
                track["path"]
                for track in lista_entrada
                if isinstance(track, dict) and "path" in track
            ]
        else:
            archivos = list(lista_entrada)

        clean_output = (
            os.path.abspath(output_path) if output_path else os.getcwd()
        )

        if is_iso:
            if clean_output.endswith(".iso"):
                output_dir = os.path.dirname(clean_output) or os.getcwd()
                iso_name = os.path.basename(clean_output)
            else:
                output_dir = clean_output
                iso_name = "hifi_dvd_project.iso"
        else:
            iso_name = None
            output_dir = clean_output

        with InhibitManager(
            reason=_("Creando disco DVD HiFi..."), app=app, window=window
        ):
            return self.procesar_proyecto(
                audio_files=archivos,
                cover_path=cover_image_path,
                output_dir=output_dir,
                iso_name=iso_name,
                progress_callback=progress_callback,
            )
