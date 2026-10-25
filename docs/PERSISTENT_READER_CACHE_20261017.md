# Caché persistente del lector — 2026-10-17

Activada en la aplicación local http://127.0.0.1:5173/ desde E:/yomira.

## Cambios

- Caché SQLite separada de la biblioteca y del historial personal, en user-data/AppData/Yomira/YomiraData/cache/online-images/images.sqlite3.
- Límite de 1 GiB de imágenes y 1000 entradas, con eliminación de las menos usadas al admitir nuevas imágenes. Validez de 7 días. El archivo SQLite admite hasta 128 MiB adicionales de estructura; las escrituras pueden necesitar un diario temporal.
- Las imágenes validadas se conservan al cerrar el lector y la aplicación. Se distingue capítulo, conector, versión y referencia de la fuente mediante una clave resumida. No se guardan URLs de origen ni credenciales en esta caché.
- Se comprueba integridad y formato antes de servir imágenes. La caché no es requisito para leer una imagen recién descargada. Se rechazan rutas enlazadas y bases de datos ajenas.
- MangaDex reutiliza durante 5 minutos las fichas y listas de capítulos correctas, con límites de memoria y consultas simultáneas compartidas. La actualización manual invalida ambos resultados.
- Opciones de lectura ofrece «Renovar imágenes del capítulo». Usa la recuperación existente, solicita contenido nuevo y conserva la página. También se renueva al reintentar una página fallida.

## Verificación

176 casos únicos de backend y 77 de interfaz: 253 pasaron. Los XML finales consolidan la ejecución principal y los casos corregidos o añadidos posteriormente; no representan una única ejecución conjunta. Comprobaciones de formato, tipos, lint y compilación de interfaz correctas.

Prueba real aislada de The Boxer 21, con reinicio completo entre sesiones: imagen de 670824 bytes, 863.51 ms en la primera descarga y 117.11 ms tras reiniciar, sin ninguna descarga de imagen en la segunda sesión (el transporte de imágenes estaba deshabilitado para demostrarlo). Renovar hizo una descarga nueva; la página 3 se conservó en las tres sesiones. El plan de páginas aún requirió 527.80 ms tras reiniciar.

Activación local verificada con la interfaz compilada y una imagen real guardada en la caché de E. Se conservaron los 39 registros de progreso y 896 registros de descargas presentes al activar. No hubo migración del esquema personal ni creación de descargas por la caché. El botón se verificó visualmente en la aplicación activa.

## Límites

No es un modo sin conexión: abrir un capítulo todavía consulta el plan de páginas. Las imágenes nuevas, vencidas o eliminadas dependen de la conexión y de la fuente. Una imagen de aviso válida no se puede distinguir automáticamente de una página del manga; debe renovarse cuando la fuente se corrija o elegirse otra fuente.

Estos cambios son locales. No se publicó una versión nueva en GitHub ni se regeneró el instalador 0.1.15.

## Respaldo

Respaldo: archive/persistent-reader-cache-20261017-163835. Incluye fuentes anteriores, dist-before, profile-before.sqlite3, changes.patch, resultados finales y captura de la interfaz. El perfil contiene datos personales y no debe distribuirse. Las pruebas reales usaron perfiles aislados; no se incluyen en ningún paquete público.
