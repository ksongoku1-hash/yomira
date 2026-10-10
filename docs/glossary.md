# Glosario de Yomira V1

Este glosario fija el significado de los términos usados en `YOMIRA_V1_SPEC.md`. Las definiciones describen el alcance y las reglas ya establecidos allí; no añaden decisiones de implementación.

| Término | Definición |
|---|---|
| **ADR (Architecture Decision Record)** | Documento que registra una decisión arquitectónica, su contexto, alternativas, consecuencias, modos de fallo y estrategia de reversión. |
| **Asset** | Archivo físico gestionado por Yomira, como portada, thumbnail, CBZ, caché de página, temporal o copia de exportación. Se registra con ruta relativa cuando sea posible, hash y tamaño. |
| **Atomicidad / publicación atómica** | Regla por la que un artefacto se construye en una ruta temporal, se verifica por completo y solo entonces se publica mediante cambio atómico de nombre. El archivo parcial no se presenta como final. |
| **Backup** | Copia verificable de la base de datos y la configuración local definida por la política de backups. No incluye todos los CBZ por defecto. |
| **CBZ** | Archivo ZIP de un capítulo, con `ComicInfo.xml` y sus páginas ordenadas. Solo se incorpora a la biblioteca después de verificar su contenido y publicarlo de forma atómica. |
| **Chapter / ChapterSource** | `Chapter` es la identidad lógica local de un capítulo. `ChapterSource` representa la versión de ese capítulo asociada a una fuente remota concreta. |
| **Circuit breaker** | Control de disponibilidad de una fuente con estados `CLOSED`, `OPEN` y `HALF_OPEN`; limita solicitudes cuando se acumulan fallos y permite una prueba controlada de recuperación. |
| **Connector (conector)** | Adaptador aislado que consulta una fuente compatible y normaliza sus resultados. No escribe en la biblioteca, no ejecuta SQL arbitrario, no genera CBZ y no decide estados de trabajos. |
| **DownloadJob / job** | Trabajo persistente que coordina la obtención, validación, procesamiento, empaquetado, verificación e indexación de un capítulo. Su estado debe poder recuperarse tras una interrupción. |
| **Failure boundary (límite de fallo)** | Frontera de responsabilidad de un componente que permite identificar qué entrada procesa, qué resultado produce, de qué depende y cómo contiene o comunica sus fallos. |
| **Hash / SHA-256** | Huella calculada para comprobar que un archivo o contenido coincide con lo esperado. La especificación requiere SHA-256 durante la descarga y reconciliación al verificar el CBZ. |
| **Idempotencia** | Propiedad por la que repetir una operación no crea duplicados arbitrarios ni altera injustificadamente el resultado; por ejemplo, volver a solicitar un capítulo ya descargado. |
| **Integridad de contenido** | Condición de que el contenido haya superado las validaciones aplicables. La finalización HTTP por sí sola no hace válido un archivo o capítulo. |
| **Local-first** | Modelo en que la biblioteca y la base de datos viven en el PC. El acceso desde la red local solo se habilita expresamente por el usuario. |
| **Manifest** | Registro versionado de un trabajo y sus páginas, con datos como orden, ruta temporal, estado, tamaño, hash, dimensiones, formato y número de intentos. |
| **Metadata authority (autoridad de metadatos)** | Prioridad para resolver valores: `USER_OVERRIDE` > `LOCAL_CANONICAL` > `SOURCE_METADATA`. La fuente remota no sobrescribe silenciosamente valores elegidos por el usuario. |
| **Page cache / caché de páginas** | Copia temporal reutilizable de páginas servidas por el lector. Debe tener límite configurable y política de expulsión LRU; no puede crecer sin límite. |
| **Recovery Manager** | Componente que, al arrancar, comprueba la base de datos, migraciones, jobs y temporales, reconcilia filesystem y DB y determina si cada elemento se reanuda, reintenta, conserva como huérfano/corrupto o limpia de forma segura. |
| **SAFE (procesamiento SAFE)** | Modo de procesamiento de imágenes predeterminado. Ante duda sobre un recorte, conserva la página sin recortar; no debe recomprimir páginas que no necesiten transformación. |
| **Safe Mode** | Modo de funcionamiento limitado ante fallos críticos de base de datos, migraciones, permisos, rutas o schema. Permite diagnósticos y recuperación, pero no nuevas descargas. |
| **Source (fuente)** | Servicio remoto compatible del que pueden obtenerse búsqueda, metadatos, capítulos o páginas mediante un connector. Sus respuestas se consideran entradas no confiables. |
| **Support Bundle** | Paquete de diagnóstico con información de versión, entorno, configuración y logs sanitizados. Excluye páginas, CBZ, cookies, tokens y contraseñas. |
| **Trazabilidad / `trace_id`** | Identificador que permite relacionar eventos de un mismo fallo u operación entre componentes. Los registros incluyen además, cuando aplique, job, capítulo, fuente, etapa, código, intento y duración. |
| **Temporal `.part` / `.tmp`** | Archivo aún no válido para uso final. `.part` se usa durante una descarga; `.tmp`, durante una construcción como la del CBZ. Recovery debe determinar su pertenencia y estado antes de limpiarlo. |
| **WAL** | Modo de registro de SQLite (`journal_mode=WAL`) requerido por la especificación, junto con claves foráneas habilitadas y un `busy_timeout` configurado. |

## Estados con significado normativo

- Los estados de `DownloadJob` y sus transiciones se rigen por las secciones 17–19 de la especificación. `INTERRUPTED` es recuperable; `COMPLETED`, `FAILED` y `CANCELLED` son terminales.
- Las versiones de capítulo pueden ser `ACTIVE`, `ALTERNATE`, `SUPERSEDED` o `INVALID`; reemplazar una versión exige validación, trazabilidad y publicación atómica.
- La salud de un connector puede ser `HEALTHY`, `DEGRADED`, `BROKEN`, `DISABLED` o `INCOMPATIBLE`.
- Los errores se agrupan en las familias `SYS_`, `DB_`, `SRC_`, `DL_`, `IMG_`, `CBZ_`, `LIB_`, `READ_`, `EXP_`, `AUTH_` y `NET_`.
