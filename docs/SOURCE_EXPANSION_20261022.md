# Ampliación de fuentes de Yomira — 22 de octubre de 2026

Activa en E:/yomira, http://127.0.0.1:5173/. Se pasa de 12 a 21 fuentes integradas: 20 sitios públicos del motor FMD2 y MangaDex.

## Nueve fuentes añadidas

| Fuente | Idioma |
| --- | --- |
| InfraFandub | Español |
| Inventario Oculto | Español |
| Ragnarok Scanlation | Español |
| Uchuujin Scans | Español |
| LeeMiau | Español |
| TaurusManga | Español |
| Weeb Central | Inglés |
| MangaPill | Inglés |
| ManhuaPlus | Inglés |

Se integran en la búsqueda existente, la consulta de capítulos, la lectura en línea y la adquisición de páginas para descarga. La lista «Fuentes habilitadas» muestra sus nombres e idiomas. El inventario enumera sitios integrados; los resultados confirman disponibilidad por obra.

## Implementación

La lista de fuentes del servidor coincide con los identificadores, nombres y dominios exactos del motor nativo. Se conservan la autenticación del motor, la política de fuentes públicas sin cuentas, las restricciones de URL y los permisos de imágenes por proveedor. Los orígenes de portada adicionales están limitados a los CDN verificados de Weeb Central y MangaPill.

Las búsquedas públicas que requieren red se ejecutan en grupos de hasta cuatro hilos independientes y conservan los límites y la deduplicación anteriores. Las búsquedas en los índices locales permanecen disponibles. La consulta conjunta «Akane» tardó aproximadamente 3,61 segundos en la prueba aislada del motor; no mide el tiempo total de carga de capítulos.

Se adaptaron la búsqueda y la ficha actual de InfraFandub, las búsquedas de Uchuujin/LeeMiau, Taurus y MangaPill, y la disponibilidad de las fuentes que consultan directamente su sitio. Se incluyeron índices públicos iniciales para InfraFandub, Inventario Oculto y Ragnarok, además de los índices oficiales necesarios de Weeb Central y ManhuaPlus.

Inventario Oculto y Ragnarok dependen de sus índices iniciales de 37 y 40 títulos. No se añadió un refresco automático de esos dos índices: deberán actualizarse cuando cambien sus directorios. InfraFandub también dispone de búsqueda pública directa.

Los módulos y las plantillas compartidas Madara/MangaThemesia se guardan con sus hashes. Los scripts de restauración y empaquetado copian y verifican esos archivos y los catálogos iniciales, para que las futuras instalaciones incluyan el soporte probado. No se publicó un instalador nuevo en este cambio.

## Validación

- 134 pruebas del servidor y 171 de la interfaz: 305 en total. Tipos y estilo correctos; motor nativo compilado y fijado por SHA-256. Scripts de distribución analizados sintácticamente.
- Las nueve fuentes pasaron búsqueda, ficha, lista de capítulos y verificación de una imagen real. El registro detallado está en candidate-proof.json.
- Se descargó completo un capítulo de TaurusManga de ocho páginas, 1.495.480 bytes, en un perfil aislado. Se verificaron las imágenes y la integridad de su archivo CBZ; no se agregó a la biblioteca personal.
- La búsqueda conjunta mantuvo el límite de 50 resultados y no devolvió duplicados. Un cliente sin autorización siguió recibiendo 401 y se rechazaron fuentes o límites no permitidos.
- En la instalación principal se verificaron las 21 fuentes, resultados de TaurusManga al buscar «Rayo» y sus 98 capítulos disponibles. La lista se adapta a escritorio y móvil de 320 píxeles sin desbordamiento horizontal.
- El índice de Cartel De Manhwas, una fuente anterior, no estaba listo en el perfil aislado. Esto no impide usar las fuentes nuevas; no se presenta el inventario como garantía de disponibilidad permanente.

## Conservación y respaldo

Tras reiniciar y comprobar la interfaz, las 46 posiciones de lectura y los 896 registros de descargas permanecen intactos. No se escribieron registros en la base personal desde los scripts de activación o las pruebas.

Respaldo en E:/yomira/archive/source-expansion-20261021: profile-before.sqlite3 conserva una copia consistente del perfil; originals/ y files.json conservan el código anterior; motor-originals/ y motor-files.json conservan los archivos anteriores del motor. Los índices existentes del usuario se conservaron y solo se añadieron los que faltaban. No se sustituyó la interfaz estática, que ya soportaba el inventario ampliado.

Versión local 0.1.15. No se hizo commit, PR, publicación de GitHub ni un nuevo instalador.
