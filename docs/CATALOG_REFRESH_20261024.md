# Actualización automática de catálogos — 24 de octubre de 2026

Estado: integrado en la versión local 0.1.15 de `E:/yomira`. Yomira está iniciada en `http://127.0.0.1:5173/`; el arranque normal completó la actualización de los dos catálogos.

## Comportamiento

Inventario Oculto y Ragnarok Scanlation revisan sus directorios públicos en segundo plano. Un catálogo actualizado se conserva durante 24 horas; el estado persiste al cerrar la aplicación. Al volver a abrirla se comprueba si corresponde actualizar. El trabajo de red no bloquea la disponibilidad del lector o de la biblioteca.

Las solicitudes se limitan a los dos orígenes HTTPS declarados, con al menos un segundo entre peticiones, sin cuentas ni cookies. Un fallo deja una espera persistida de una hora antes del siguiente intento. La recolección tiene límites de páginas, títulos, tamaño y tiempo, y descarta enlaces externos, páginas repetidas, navegación incompleta y respuestas que no correspondan al directorio esperado.

El índice nuevo solo se publica después de recorrer y validar el listado completo. Se genera y comprueba un SQLite compatible, se cierran sus conexiones antes del reemplazo y se conserva una copia anterior durante la publicación del índice y su estado. Se probaron la restauración ante fallos y la reparación de índices vacíos, corruptos o de esquema ajeno. La cancelación durante la red conserva el índice anterior.

No cambia el motor nativo, las 21 integraciones declaradas, sus identidades, el diseño de la interfaz ni la base personal de progreso. El motor reconoce el índice nuevo en las siguientes consultas sin reiniciarse.

## Comprobaciones

| Comprobación | Resultado |
| --- | --- |
| Pruebas específicas del servicio | 42 aprobadas |
| Regresión de integración, conectores y empaquetado | 190 aprobadas, 0 fallos |
| Pruebas nuevas sobre los archivos integrados en E | 46 aprobadas, 0 fallos |
| Tipos del servidor y de las dos pruebas nuevas | 0 errores y 0 advertencias |
| Estilo de los archivos afectados | Correcto |
| Revisión independiente de los ocho HTML reales | 37 obras de Inventario y 40 de Ragnarok, sin faltantes ni enlaces del lateral |
| Flujo HTTP real con la implementación integrada | Ocho páginas obtenidas; dos SQLite íntegros; búsquedas nativas correctas en ambos |
| Segunda comprobación dentro del TTL | Cero solicitudes nuevas a los directorios |
| Reemplazo sin reiniciar el motor | Dos versiones de un índice sintético visibles en el mismo proceso |
| Arranque normal de Yomira | Aplicación lista y ambos catálogos actualizados en segundo plano |

Los conteos de las distintas ejecuciones se presentan por separado: las 46 pruebas de los archivos integrados repiten casos incluidos en la comprobación de 190; no deben sumarse como casos únicos.

La primera copia de integración carecía de `alembic.ini`. Sus fallos de preparación no se contaron como resultado válido. La configuración completa se incorporó a la copia, y el resultado definitivo está en `final-junit.xml`; la verificación posterior sobre E está en `active-junit.xml`.

Durante la revisión se corrigieron dos casos de completitud: una tarjeta legítima de Ragnarok de tipo `text`, y una página sin navegación que podía parecer un final válido. Las pruebas usan esos casos y la revisión independiente confirmó los ocho listados completos.

## Cartel De Manhwas

El origen integrado no ofrece actualmente un catálogo utilizable: la raíz redirige a un subdominio de aparcamiento por HTTP; el directorio devuelve una página «Loading…» de 498 bytes y el índice oficial devuelve 404. No se siguió la redirección ni se sustituyó el origen por un dominio comunitario sin identidad confirmada.

Windows bloqueó una captura de esa página como potencialmente no deseada; no se abrió por otro medio y no se archivó. Solo se conservaron los estados de respuesta y el informe. Cartel queda fuera de este refresco automático y su índice sigue no disponible en el runtime observado.

## Archivos y respaldo

El cambio de producción añade `backend/app/services/fmd2_catalog_refresh.py`, integra su inicio y cierre en `lifecycle.py` y `runtime.py`, y elimina el comentario obsoleto de actualización manual de `fmd2_bridge.py`. Se añadieron las pruebas del servicio y de su integración con el ciclo de la aplicación.

- Servicio SHA-256: `9845277ca9f2c176685ab5abf621a3d0ad61dc472afa22fffac65567271f1512`.
- Respaldo de código previo: `E:/yomira/archive/catalog-refresh-20261024/originals`.
- Evidencias y manifiesto: `E:/yomira/archive/catalog-refresh-20261024`.
- Estado operativo: `E:/yomira/user-data/AppData/Yomira/YomiraData/cache/fmd2-catalog-refresh-v1.json`.

Las pruebas utilizaron perfiles aislados. Al finalizar se inició la aplicación normal para comprobar el servicio; sus dos índices públicos y su estado de caché se actualizaron. No se escribieron posiciones de lectura desde las herramientas de prueba.

## Límites

- El servicio se ejecuta mientras Yomira está abierta. No instala un servicio de Windows ni una tarea programada independiente.
- Como protección contra listados truncados, no publica un catálogo válido con menos obras que el anterior. Una reducción legítima requiere revisar esa protección. Esto no afecta la reparación de un índice ilegible o corrupto.
- El refresco incorpora las obras presentes en los directorios de cada proveedor; no garantiza obras ocultas por el sitio ni la disponibilidad futura de todos sus capítulos.
- No se repitieron las suites completas de interfaz y servidor del día anterior: se ejecutaron las comprobaciones afectadas. Los 448 diagnósticos de tipos de las pruebas antiguas no se abordaron; el código de la aplicación y las pruebas nuevas sí pasaron.
- No se creó ni publicó un instalador y no se publicaron cambios en GitHub.
