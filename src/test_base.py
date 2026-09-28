from dvd_engine import DVDEngine

try:
  engine = DVDEngine()
  engine.verificar_herramientas()
  print("✅ Verificación exitosa: Todas las dependencias del sistema están disponibles.")
except Exception as e:
  print(f"❌ Error de dependencias: {e}")
