import sys
import os

# Añade la carpeta del código fuente al path de Python
sys.path.append(os.path.join(os.getcwd(), 'src'))

try:
    from hifidvdmaker.dvd_engine import DVDEngine
except ImportError:
    from dvd_engine import DVDEngine

try:
    engine = DVDEngine()
    engine.verificar_herramientas()
    print("✅ Verificación exitosa: Todas las dependencias del sistema están disponibles.")
except Exception as e:
    print(f"❌ Error: {e}")
