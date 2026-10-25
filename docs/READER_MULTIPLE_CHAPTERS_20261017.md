# Varios capítulos online abiertos — 2026-10-17

## Problema y alcance

OnlineReaderSessionService conservaba un único capítulo. Al abrir otro, el capítulo anterior perdía sus metadatos temporales, sus páginas verificadas y la disponibilidad requerida por el guardado de progreso. Una pestaña anterior podía recibir 404 aun cuando la fuente tuviera las imágenes. El motor FMD realmente admite solo una sesión online activa; abrir otra sin cerrar la anterior tampoco es válido.

Se modificó backend/app/services/online_reader.py y se agregó backend/tests/test_online_reader_tabs.py. Sin cambios al motor nativo, la interfaz, dependencias ni esquema personal.

## Solución

El servicio conserva hasta 16 capítulos lógicos en memoria, con metadatos, referencias validadas y páginas vistas separados por capítulo. El dueño de la sesión del proveedor se gestiona por separado. Para una imagen no guardada se cierra de forma segura la sesión nativa previa y se reactiva la del capítulo solicitado; se comprueba que conserve la cantidad de páginas. Una lista distinta interrumpe ese capítulo antes de descargar o guardar progreso nuevo.

Las imágenes en RAM o en la caché persistente se sirven sin reactivar el proveedor ni volver a descargarlas. El límite de RAM de 128 MiB es compartido entre todos los capítulos: se eliminan primero páginas del capítulo menos usado y luego sus páginas menos usadas. El límite de disco previo de 1 GiB permanece. Al superar los 16 capítulos retenidos, se olvida el menos usado; el progreso personal sigue en la base de datos y las imágenes siguen sujetas a la caché persistente.

Pausar un capítulo inactivo no cierra el capítulo activo de otra pestaña. Renovar un capítulo tampoco borra las imágenes ni las páginas verificadas de otro. El guardado continúa requiriendo una página verificada del capítulo correspondiente. La adquisición de imágenes del motor sigue siendo serial y el límite de dos streams se conserva.

## Verificación

64 pruebas de backend pasaron, incluidas 9 nuevas sobre dos capítulos, progreso independiente, cambios seriales de sesión nativa, caché RAM y disco sin reactivación innecesaria, límites de recursos, pausa, renovación, cambios de cantidad de páginas, fallos de fuente y cancelación. Ruff y Pyright de los dos archivos modificados pasaron.

Prueba real en perfil y motor aislados: The Boxer 16 y 15 desde FMD/NovelCool, alternando imágenes nuevas y guardadas, mantuvieron la misma identidad de sesión lógica y guardaron progreso independiente en la página 2. Luego se abrió The Boxer 21 desde MangaDex y ambos lectores nativos conservaron metadatos e imágenes. No se crearon descargas. Las primeras imágenes nuevas al alternar tardaron aproximadamente 2.61 y 2.68 segundos; al repetir las mismas páginas se sirvieron en 28.07 y 7.85 milisegundos. Estos tiempos son de esa prueba y dependen de la fuente.

La primera ejecución de pruebas con tmp_path no pudo acceder al directorio temporal global de Windows. Se repitió la suite completa con una carpeta temporal exclusiva dentro del trabajo: 64 casos pasaron. No se cambió ninguna protección del sistema.

Activación verificada en http://127.0.0.1:5173/ después de un reinicio breve: los capítulos 16 y 15 quedaron disponibles simultáneamente y se consultó su progreso correctamente. El activador no envió escrituras de progreso ni creó descargas. Se respaldó el perfil antes del reinicio.

## Límites y respaldo

No garantiza apertura instantánea de páginas nuevas: el cambio de sesión del motor y la descarga siguen dependiendo de la fuente. La recuperación de la interfaz tras reinicios, implementada anteriormente, continúa disponible. Leer el mismo capítulo en varias pestañas comparte una única posición guardada de ese capítulo. No se publicó una versión ni se regeneró el instalador.

Respaldo: archive/reader-multiple-chapters-20261017-173339. Incluye fuente anterior, copia privada del perfil, parche, resultados y verificaciones. No distribuir el perfil personal ni las copias usadas por las pruebas.
