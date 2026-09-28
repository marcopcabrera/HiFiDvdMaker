def __init__(self, **kwargs):
    super().__init__(**kwargs)

    # 1. Obtener el botón desde el Builder
    self.btn_crear_dvd = builder.get_object("btn_crear_dvd")

    # 2. Inicializar variables de control en None (vacías)
    self.ruta_imagen_portada = None
    self.ruta_destino_dvd = None

    # Asegurarnos de que el botón comience deshabilitado
    self.btn_crear_dvd.set_sensitive(False)
