import sys
import os

sys.path.append(os.path.join(os.getcwd(), 'src'))

try:
    from hifidvdmaker.dvd_engine import DVDEngine
except ImportError:
    from dvd_engine import DVDEngine

import tempfile

engine = DVDEngine()

# Crea archivos vacíos / temporales de prueba para verificar ejecución de ffprobe y ffmpeg
print("🔍 Probando renderizado a VOB...")
