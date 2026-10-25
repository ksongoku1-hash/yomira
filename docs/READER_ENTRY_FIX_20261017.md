# Reparación de reapertura del lector — 17 de octubre de 2026

Caso real: The Boxer, capítulo 16, identidad 01ef3e7a-9aac-4a9f-a97a-ca2ed038c7f4. El enlace sin online=1 devolvía 404 al no existir una copia local verificada. El progreso sí estaba guardado. La traza de la captura correspondía a ese 404.

- El lector comprueba la fuente cuando falla una entrada local con 404. Si existe, cambia a online=1 y abre una sesión real. Conserva el estado de navegación y los demás parámetros. Un 404 sin fuente sigue mostrando el error; no se convierte un error 503 local en una apertura online ni se modifica el progreso mientras se resuelve.
- La consulta del origen FMD2 usa la referencia de obra incluida en el capítulo. Permite distinguir varias fuentes del mismo conector vinculadas al manga y mantiene el rechazo ante referencias malformadas o ambiguas.
- El motor instalado era 413b110941a106e44504f23e2cccc69759910c4f4ace5d739da1c193a84a1db8, sin la API /v1/reader. Se reemplazó por el motor verificado del paquete local 0.1.14, SHA-256 98ec7539f9f82e4edc1a149605039c591c071a524ecc7573788ad8a2279956fa. El motor anterior se conserva en la copia de recuperación.
- El enlace antiguo de NovelCool redirigía a su portada. Al reabrir referencias antiguas /go/rds/ se consulta el catálogo de la misma obra y se exige una única coincidencia exacta de título e idioma. Se abre la referencia actual en memoria manteniendo el identificador del capítulo y la referencia guardada, sin migrar ni crear posiciones de lectura.
- NovelCool prepara su catálogo en memoria antes de abrir el plan online y reutiliza su caché de 60 segundos. Sus identificadores estables de Lua llevan el prefijo de ruta que exige la API del motor. La modificación de Lua está en user-data/fmd2-runtime/lua/templates/NovelCool.lua y se incluye expresamente como código en el ZIP; se debe conservar al preparar el siguiente paquete del motor.
- Los fallos del proveedor registran códigos, operación y estado HTTP sin guardar URLs ni credenciales.

Validación: 77 casos únicos del servidor y 249 de la interfaz aprobados. Incluyen referencia exacta de la fuente, recuperación de URL sin online=1, posición guardada no inicial, rechazo de ambigüedad, catálogo reutilizado, lector y controles existentes. Ruff, Pyright de aplicación, TypeScript, ESLint y compilación correctos.

Prueba real: apertura 200, plan de seis páginas y primera imagen válida de 172308 bytes (SHA-256 3b396d2a89b278d3dc656b3e02cac2eb7f05225cde038e7efebae89a1cf843b6). La sesión se cerró y después se abrió el enlace original desde el navegador: se resolvió automáticamente a online=1 y mostró Capítulo 16, página 1 de 6, con imagen cargada de 690 × 8229 píxeles. La pestaña quedó disponible para continuar leyendo.

Comparación posterior a toda la revisión: las 34 posiciones de lectura, todos los ajustes y los trabajos de descarga son idénticos a los de antes de restablecer el servicio. No se crearon descargas de capítulos ni CBZ. Se guardaron copias privadas de recuperación en el archivo local; no están en el ZIP de código.

Reparación activa localmente en E:. No se recompiló el instalador ni se publicó una Release de GitHub. La disponibilidad futura de las imágenes sigue dependiendo del proveedor.
