# YOMIRA — MASTER EXECUTION PLAN: REBASELINE DE ALCANCE V1

**Documento:** docs/YOMIRA_MASTER_EXECUTION_PLAN_V1_SCOPE.md
**Versión:** 1.0 — sucesor de alcance de YOMIRA_MASTER_EXECUTION_PLAN.md V1.1
**Estado:** alcance Accepted por autorización explícita del usuario, registrado en ADR-0081. Este estado no declara que el código, tareas o release gates hayan pasado.
**Fecha de rebaseline:** 2026-09-27
**Línea base histórica:** docs/YOMIRA_V1_SPEC.md 1.1 APPROVED y docs/YOMIRA_MASTER_EXECUTION_PLAN.md V1.1. Ambos permanecen intactos.
**Regla superior:** integridad > trazabilidad > recuperación > recursos acotados > velocidad.

Este plan sucesor no sobrescribe ni renumera el maestro vigente. La matriz de la sección 5 mapea una sola vez cada ID original; los objetivos, instrucciones, archivos, pruebas y criterios PASS canónicos permanecen en su tarea del plan V1.1. Aplazar una tarea cambia el release gate, no borra ni rebaja el requisito. ADR-0081 gobierna conflictos de alcance/fecha de release; los invariantes técnicos y de privacidad de la SPEC y ADRs siguen vigentes.

## 1. Principios que siguen siendo innegociables

1. Preservar los bytes del usuario. No publicar páginas ni CBZ incompletos, truncar un destino previo, borrar archivos ambiguos o transformar automáticamente imágenes en V1.
2. Integridad antes que velocidad. Una respuesta HTTP exitosa no valida contenido. Se validan tipo, firma, límites, decode, orden, hashes y el CBZ antes de indexarlo.
3. Trazabilidad accionable. Errores relevantes conservan error_code, etapa, trace ID y, si aplica, job/source/chapter ID, intento y retryability. Logs y cachés son acotados y tienen rotación.
4. Fallar cerrado ante datos o identidad inciertos. Preservar evidencia, presentar estado pendiente/bloqueado y evitar nuevas escrituras inseguras.
5. Recuperación progresiva y honesta. V1 reanuda interrupciones ordinarias sólo con evidencia verificable; una fila bloqueada no cuenta como recuperación completa.
6. Recursos acotados desde el diseño. Colas, workers, semáforos, tamaño de respuesta, páginas, caché, logs y preflight de disco tienen límites. Backpressure dinámico llega en V1.1.
7. Idempotencia, CAS y atomicidad. Solicitudes repetidas no crean CBZ duplicados; el destino final se publica después de verificarlo; SQLite se indexa después de publicar.
8. Separación de responsabilidades. React llama APIs tipadas; conectores no escriben DB/biblioteca; downloader no construye CBZ; processor no hace HTTP; builder no consulta fuentes.
9. Windows es plataforma objetivo. Rutas, antivirus/indexadores, permisos, USB/exFAT, suspensión/reinicio, instalador y separación de datos se prueban antes de V1.
10. ADR antes de cambiar comportamiento fundamental. Esta rebaseline se registra en ADR-0081; cambios futuros a API, seguridad, estados o archivos requieren ADR sucesora.
11. Ciclo de bugs: reproducir → test que falla → causa raíz → fix → test pasa → regresión. No eliminar pruebas ni validaciones para satisfacer gates.
12. Todo requisito diferido conserva ID, criterio, historial y vínculo. Cada versión ejecuta sus gates antes de habilitar la capacidad.

## 2. Alcance exacto de Yomira V1

### Incluido y requerido

- Aplicación local Windows, React + TypeScript strict, FastAPI + Python y SQLite WAL con migraciones.
- Biblioteca local, búsqueda, selección de fuente compatible, metadata, serie, consulta/selección de capítulos.
- Arquitectura de conectores aislada; mock determinista y al menos un conector real compatible. Un conector que necesite secreto persistente se bloquea hasta completar K066.
- DownloadJobs persistentes, state machine, cancelación y estados básicos, límites de cola/concurrencia, retry y reanudación razonable.
- Descarga a .part, hash, preflight de disco, guard de duplicados y backoff acotado para locks transitorios de filesystem Windows. Los parciales no se borran hasta verificar que no sean reutilizables o que la limpieza sea autorizada.
- Validación segura de imágenes: límites de tamaño/dimensiones/píxeles, MIME, magic bytes, decode, truncamiento/anomalías básicas. Imagen inválida no entra en la biblioteca.
- V1 conserva la imagen validada original y la empaqueta sin recorte. El modo OFF es default; la frontera del processor admite SAFE futuro.
- CBZ con orden determinista y ComicInfo.xml; build temporal, verificación ZIP/XML/page count/imágenes/hashes, publicación atómica e indexación DB posterior.
- Biblioteca, lector (single/vertical/doble/RTL, zoom/fit/fullscreen, teclado/touch), progreso, virtualización y caché limitada.
- LAN opt-in básica; modo e-reader/Bigme y exportación USB con temporal, hash y rename.
- Instalador Windows con runtimes, separación programa/datos, conservación de biblioteca y prueba en Windows limpio.
- Gates críticos de TypeScript/ESLint/Ruff/Pyright, unit/integration, pipeline de descarga, CBZ, E2E, cierre/reapertura, Bigme, LAN e instalación.

### Diferido, pero conservado

La recuperación automática completa de estados ambiguos, snapshots durables, reparación DB/filesystem, restore general/rebuild, backpressure con telemetría dinámica, SAFE crop, diagnósticos avanzados, benchmarks extremos, fault injection exhaustivo y madurez para distribución pública no bloquean V1. Siguen en su versión destino en la matriz.

V1 enumera jobs activos al iniciar en lotes acotados; actualiza cada fila con CAS individual y guarda interrupted_from_status al transicionar a INTERRUPTED. No usa UPDATE masivo. Sólo reanuda/reencola cuando punto de reinicio y archivos se verifican; ante ambigüedad o cancelación pendiente en etapa no reversible conserva DB/archivos y deja el job INTERRUPTED y bloqueado. No resuelve por heurística toda contradicción DB/filesystem.

V1 no limpia archivos que no puede clasificar. En operación normal, K096 permite iniciar VALIDATING/PACKAGING sólo mediante los validadores y publisher atómico de V1; no requiere tener ya los adapters de replay avanzados. Tras crash sin punto verificable, no replaya esa etapa: K094 preserva artefactos, registra INTERRUPTED/bloqueado por CAS y aísla el job, dejando lectura/biblioteca disponibles si DB y raíz están sanas. P0, corrupción comprobada, CBZ parcial publicado, secreto en claro, pérdida del progreso principal o cualquier P1 abierto bloquean release. Una incidencia ambigua preservada y visible no se llama recovery completado.

## 3. Flujo funcional mínimo completo

El E2E principal corre primero con el mock local y luego con un connector real compatible:

1. Abrir Yomira en Windows; verificar versión/readiness, DB/migraciones y raíz de datos.
2. Buscar obra, filtrar fuente y elegir serie, sin fusionar fuentes automáticamente.
3. Consultar metadata/capítulos, seleccionar capítulo y crear job persistente; doble solicitud no crea duplicado.
4. Descargar páginas a .part con concurrencia acotada, trace/job IDs, retries y progreso.
5. Verificar tipo real, magic bytes, decode, límites y hash por página. Invalidar sin indexar.
6. Construir CBZ temporal, ordenar páginas, incluir ComicInfo.xml, verificar ZIP/metadata/pages/hashes y publicar atómicamente.
7. Indexar en biblioteca después de publicación; mostrar capítulo descargado.
8. Abrir lector, recorrer páginas y guardar/restaurar progreso y modo e-reader; comprobar Bigme.
9. Exportar a USB cuando forme parte de la prueba; la desconexión no reemplaza ni trunca el origen.
10. Cerrar y abrir Yomira. Recuperar biblioteca/progreso; startup enumera activos por lotes, CAS por job y reanuda sólo archivos verificables, sin duplicar publicados.
11. En pruebas controladas cortar internet, llenar volumen, negar permiso y simular lock Windows. Cada fallo queda visible/trazable; no pierde archivos previos.

## 4. Arquitectura mínima necesaria

### Frontend

React + TypeScript strict, diseño cerrado antes de vistas funcionales, UI desktop y modo e-reader. API Client único tipado con trace ID, errores y version/readiness. React no toca filesystem. Listas/lector virtualizados; caché y precarga limitadas.

### Backend

FastAPI con factory/composition root, settings validados, ambientes, liveness/readiness, error/trace middleware, logs estructurados, ciclo controlado startup/shutdown y single instance Windows. Servicios coordinan casos de uso; repositorios separan DB; registry controla conectores.

### Persistencia y jobs

SQLite WAL, foreign keys, busy timeout, SQLAlchemy/Alembic; migraciones aditivas, modelos de series/sources/chapters/assets/jobs/pages/progreso, timestamps UTC e invariantes SPEC. Jobs durables con idempotencia, transiciones CAS, límites estáticos y cancelación. Startup K094 opera en lotes acotados con CAS individual por job, preserva estado origen; no ejecuta UPDATE masivo. Interrupciones ambiguas permanecen INTERRUPTED bloqueadas. No se abre sesión DB durante operaciones largas de filesystem.

### Conectores y descargas

Contratos tipados para search/serie/capacidades/capítulos/pages/health/errors. Mock determinista y al menos un conector real admisible. Connector sólo obtiene/normaliza por API; no tiene DB raw ni acceso directo a library. No se eluden CAPTCHA/DRM/paywall/access controls. SecretVault opaco; settings/logs nunca reciben secreto. Downloader usa cliente HTTP central, streaming, .part, hash, retries y semáforos.

### Imágenes, CBZ y biblioteca

La validación es previa al builder. V1 no auto-crop; empaqueta originales válidos. Builder escribe .cbz.tmp, incluye ComicInfo.xml, verifica archivo desde disco y sólo entonces publica atómicamente. DB se actualiza después. Cleanup no adivina ni elimina orphans inciertos.

### Reader, LAN, Bigme e instalación

ReaderAssetService sirve páginas desde CBZ con caché acotada y ETag; progreso no modifica CBZ. LAN OFF por defecto, se abre sólo tras opt-in con autenticación. Modo e-reader reduce efectos/preload. USB export usa .part, hash y rename. Inno Setup/PyInstaller separan binary y data root, se instalan sin runtimes de desarrollo y conservan datos ante upgrade/uninstall según contrato.

## 5. K-tasks de V1

### 5.1 Orden por dependencias funcionales

Las fases anteriores se convierten en work packages; IDs no cambian.

| Paquete | IDs y objetivo | Dependencia/gate |
|---|---|---|
| WP0 — Constitución, contratos y UX | K001–K013 | SPEC/ADRs y boundaries; UX Review Gate. |
| WP1 — Tooling y app shell | K014–K033, K235 | Builds reproducibles, FastAPI/React tipados, errors/traces; Unified Quality y Backend Core Gates. |
| WP2 — DB, dominio y biblioteca | K034–K058 | Migraciones/invariantes PASS antes de UI; DB Gate y Library Gate básico. |
| WP3 — Connectors y fixtures | K059–K080 | Mock, sandbox, contratos y errores reproducibles antes de downloader. |
| WP4 — Jobs y transporte | K081–K116 | Job limitado → manifest durable → .part → hash/retry/resume; startup K094 CAS por fila, sin UPDATE masivo. Backpressure automático completo se libera en V1.1. |
| WP5 — Validación y publicación | K117–K129, K137, K143–K157 | Página y CBZ validados; fallo de verificación nunca publica/indexa. |
| WP6 — Search/capítulos a DownloadJob | K158–K169, K207–K214 | Mock + Connector A compatible; E2E Search→Job. |
| WP7 — Reader/Bigme/USB | K170–K186, K196–K206 | Consume CBZ publicado, progreso/export; Reader y Export Gates. |
| WP8 — Integración y distribución Windows | K221, K226, K247–K248, K251, K254–K267, K269–K277, K281–K284 | E2E y cierre/reapertura primero; Windows limpio, LAN opt-in, reportes/release al final. |
| V1.1 — Recuperación/diagnóstico | K090, K094–K095, K131–K136, K138–K142, K187–K195, K215–K224, K234, K236–K237 | K094 recibe implementación completa de reconciliación; su bootstrap/CAS seguro ya opera en V1. |
| V1.2 — Integridad/mantenimiento | K047, K225, K227–K231, K268, K280 | Backups/restore/deep verify/rebuild/vacuum/optimize/rollback avanzados. |
| V1.3+ — Hardening | K232–K233, K238–K246, K249–K250, K252–K253, K278–K279 | Fault suite exhaustiva, soporte público, stress/benchmarks. |

K157 conserva en V1 fallos representativos de construcción/publicación para demostrar que no se expone un CBZ parcial; sus combinaciones exhaustivas se difieren. K221 V1 Gate cubre Connector A y el mock; después de implementar Connector B en V1.1, se repite el contrato/gate ampliado para ambas fuentes. WP6 puede comenzar cuando contratos de API/fuentes estén cerrados; WP7 depende de CBZ publicado. Instalador/release van después de E2E y cierre-reapertura.

### 5.2 Matriz íntegra de K001–K284

Cada ID aparece una vez. La tarea del plan vigente se resume a continuación para navegación; su fila canónica conserva objetivo, instrucciones, archivos, pruebas y PASS. “Fundamento” significa que el contrato/diseño entra en V1, y la última columna declara el release cuya entrega/gate lo satisface.

| K | Tarea del plan vigente (resumen) | Clasificación | Release |
| K001 | Aprobar especificación técnica | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K002 | ADRs iniciales | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K003 | Ownership y dependencias | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K004 | Error model, logging y bugs | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K005 | Design System | V1 — imprescindible | V1 |
| K006 | Layout desktop | V1 — imprescindible | V1 |
| K007 | Layout tablet | V1 — imprescindible | V1 |
| K008 | Layout mobile | V1 — imprescindible | V1 |
| K009 | Modo e-reader | V1 — imprescindible | V1 |
| K010 | Componentes | V1 — imprescindible | V1 |
| K011 | Estados extremos | V1 — imprescindible | V1 |
| K012 | Accesibilidad | V1 — imprescindible | V1 |
| K013 | UX Review Gate | V1 — imprescindible | V1 |
| K014 | Estructura física | V1 — imprescindible | V1 |
| K015 | Congelar runtimes | V1 — imprescindible | V1 |
| K016 | Tooling backend | V1 — imprescindible | V1 |
| K017 | FastAPI mínimo | V1 — imprescindible | V1 |
| K018 | Frontend React mínimo | V1 — imprescindible | V1 |
| K019 | App Shell | V1 — imprescindible | V1 |
| K020 | API Client tipado | V1 — imprescindible | V1 |
| K021 | Logging y errores base | V1 — imprescindible | V1 |
| K022 | Unified Quality Gate | V1 — imprescindible | V1 |
| K023 | Application Factory — separar creación de FastAPI de `main.py`. | V1 — imprescindible | V1 |
| K024 | Configuración tipada — Settings central. | V1 — imprescindible | V1 |
| K025 | Environment handling — dev/test/prod. | V1 — imprescindible | V1 |
| K026 | Liveness/Readiness — proceso vivo vs listo. | V1 — imprescindible | V1 |
| K027 | Error middleware — errores API uniformes. | V1 — imprescindible | V1 |
| K028 | Trace middleware — trace ID por request. | V1 — imprescindible | V1 |
| K029 | Structured logging — logs estructurados. | V1 — imprescindible | V1 |
| K030 | Startup lifecycle — inicialización controlada. | V1 — imprescindible | V1 |
| K031 | Shutdown lifecycle — cierre seguro. | V1 — imprescindible | V1 |
| K032 | Single instance baseline — impedir dos backends simultáneos. | V1 — imprescindible | V1 |
| K033 | Backend Core Gate — startup/shutdown repetido y pruebas de errores. | V1 — imprescindible | V1 |
| K034 | SQLite engine con WAL, FK y busy timeout. | V1 — imprescindible | V1 |
| K035 | SQLAlchemy base y separación de modelos. | V1 — imprescindible | V1 |
| K036 | Alembic obligatorio. | V1 — imprescindible | V1 |
| K037 | tabla/dominio Series. | V1 — imprescindible | V1 |
| K038 | Sources. | V1 — imprescindible | V1 |
| K039 | SeriesSource. | V1 — imprescindible | V1 |
| K040 | Chapters sin `float`. | V1 — imprescindible | V1 |
| K041 | ChapterSource. | V1 — imprescindible | V1 |
| K042 | Assets. | V1 — imprescindible | V1 |
| K043 | DownloadJob. | V1 — imprescindible | V1 |
| K044 | DownloadPages. | V1 — imprescindible | V1 |
| K045 | ReaderProgress. | V1 — imprescindible | V1 |
| K046 | Settings y SchemaHistory. | V1 — imprescindible | V1 |
| K047 | Mantenimiento de SQLite (VACUUM y PRAGMA optimize). | V1.2 — mantenimiento/integridad | V1.2 |
| K048 | DB Gate: migraciones, rollback, constraints, FK e integrity check. | V1 — imprescindible | V1 |
| K049 | LibraryService. | V1 — imprescindible | V1 |
| K050 | Library API. | V1 — imprescindible | V1 |
| K051 | portadas como Assets. | V1 — imprescindible | V1 |
| K052 | ThumbnailService small/medium/large. | V1 — imprescindible | V1 |
| K053 | UI de biblioteca tipo Webtoon dark. | V1 — imprescindible | V1 |
| K054 | detalle de serie. | V1 — imprescindible | V1 |
| K055 | filtros Todos/Leyendo/Completos/Incompletos. | V1 — imprescindible | V1 |
| K056 | autoridad de metadata `USER_OVERRIDE > LOCAL > SOURCE`. | V1 — imprescindible | V1 |
| K057 | mapeo de rutas relativas. | V1 — imprescindible | V1 |
| K058 | Library Gate con 100, 1000 y 10000 series sintéticas. | V1 — imprescindible | V1 |
| K059 | interfaz `SourceConnector`. | V1 — imprescindible | V1 |
| K060 | metadata del connector: ID, versión, capacidades. | V1 — imprescindible | V1 |
| K061 | contrato Search. | V1 — imprescindible | V1 |
| K062 | contrato metadata de serie. | V1 — imprescindible | V1 |
| K063 | contrato de capítulos complejos. | V1 — imprescindible | V1 |
| K064 | contrato de pages. | V1 — imprescindible | V1 |
| K065 | errores `SRC_*`. | V1 — imprescindible | V1 |
| K066 | Secret Vault local. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K067 | sandbox: sin DB raw ni escritura directa. | V1 — imprescindible | V1 |
| K068 | registry controlado. | V1 — imprescindible | V1 |
| K069 | MockConnector. | V1 — imprescindible | V1 |
| K070 | Connector SDK Gate con contract tests. | V1 — imprescindible | V1 |
| K071 | servidor falso local. | V1 — imprescindible | V1 |
| K072 | modo normal. | V1 — imprescindible | V1 |
| K073 | modo lento. | V1 — imprescindible | V1 |
| K074 | 404/429/500. | V1 — imprescindible | V1 |
| K075 | desconexión a mitad de página. | V1 — imprescindible | V1 |
| K076 | HTML-as-image y JPEG corrupto. | V1 — imprescindible | V1 |
| K077 | image bomb. | V1 — imprescindible | V1 |
| K078 | metadata inválida. | V1 — imprescindible | V1 |
| K079 | páginas duplicadas. | V1 — imprescindible | V1 |
| K080 | Mock Gate: escenarios deterministas y reproducibles. | V1 — imprescindible | V1 |
| K081 | dominio Job. | V1 — imprescindible | V1 |
| K082 | state machine. | V1 — imprescindible | V1 |
| K083 | guard de transiciones ilegales. | V1 — imprescindible | V1 |
| K084 | persistencia. | V1 — imprescindible | V1 |
| K085 | cola acotada. | V1 — imprescindible | V1 |
| K086 | prioridades HIGH/NORMAL/LOW. | V1 — imprescindible | V1 |
| K087 | fairness global/source/host. | V1 — imprescindible | V1 |
| K088 | cancelación semántica. | V1 — imprescindible | V1 |
| K089 | pause/resume donde sea seguro. | V1 — imprescindible | V1 |
| K090 | Backpressure y monitoreo de recursos. | V1.1 — recovery/diagnóstico | V1.1 |
| K091 | estado INTERRUPTED. | V1 — imprescindible | V1 |
| K092 | manifest versionado. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K093 | manifest atomic persistence. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K094 | metadata de recovery. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K095 | diagnósticos del job. | V1.1 — recovery/diagnóstico | V1.1 |
| K096 | Job Manager Gate con fault tests. | V1 — imprescindible | V1 |
| K097 | `httpx.AsyncClient` central con pooling. | V1 — imprescindible | V1 |
| K098 | streaming por chunks a `.part`. | V1 — imprescindible | V1 |
| K099 | SHA-256 al vuelo. | V1 — imprescindible | V1 |
| K100 | timeouts separados. | V1 — imprescindible | V1 |
| K101 | retry 1s/3s/8s inicial. | V1 — imprescindible | V1 |
| K102 | handling 429/Retry-After. | V1 — imprescindible | V1 |
| K103 | mapeo HTTP → `DL_*`. | V1 — imprescindible | V1 |
| K104 | Circuit Breaker. | V1 — imprescindible | V1 |
| K105 | semaphore por host. | V1 — imprescindible | V1 |
| K106 | semaphore global. | V1 — imprescindible | V1 |
| K107 | safe rename `.part` → final. | V1 — imprescindible | V1 |
| K108 | OS Lock Handling para operaciones críticas de filesystem. | V1 — imprescindible | V1 |
| K109 | resume reutilizando páginas válidas. | V1 — imprescindible | V1 |
| K110 | duplicate job guard. | V1 — imprescindible | V1 |
| K111 | disk preflight. | V1 — imprescindible | V1 |
| K112 | progreso bytes/páginas. | V1 — imprescindible | V1 |
| K113 | cancelación de streams. | V1 — imprescindible | V1 |
| K114 | recovery de `.part`. | V1 — imprescindible | V1 |
| K115 | network fault tests. | V1 — imprescindible | V1 |
| K116 | Download Engine Gate con stress e integridad. | V1 — imprescindible | V1 |
| K117 | MIME. | V1 — imprescindible | V1 |
| K118 | magic bytes. | V1 — imprescindible | V1 |
| K119 | decode con Pillow. | V1 — imprescindible | V1 |
| K120 | límites de dimensiones. | V1 — imprescindible | V1 |
| K121 | límites de píxeles. | V1 — imprescindible | V1 |
| K122 | detección de truncamiento. | V1 — imprescindible | V1 |
| K123 | duplicados por hash. | V1 — imprescindible | V1 |
| K124 | anomalías relativas de resolución. | V1 — imprescindible | V1 |
| K125 | page count. | V1 — imprescindible | V1 |
| K126 | STANDARD/DOUBLE/LONG/UNKNOWN. | V1 — imprescindible | V1 |
| K127 | VERIFIED/WARNINGS/FAILED. | V1 — imprescindible | V1 |
| K128 | fixtures corruptos. | V1 — imprescindible | V1 |
| K129 | Validation Gate con cero falsos PASS críticos. | V1 — imprescindible | V1 |
| K130 | modos OFF/SAFE/E_READER. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K131 | border scanner. | V1.1 — recovery/diagnóstico | V1.1 |
| K132 | confidence score. | V1.1 — recovery/diagnóstico | V1.1 |
| K133 | safety margin. | V1.1 — recovery/diagnóstico | V1.1 |
| K134 | abort rules: duda = no crop. | V1.1 — recovery/diagnóstico | V1.1 |
| K135 | protección de double pages. | V1.1 — recovery/diagnóstico | V1.1 |
| K136 | tratamiento long-strip/webtoon. | V1.1 — recovery/diagnóstico | V1.1 |
| K137 | ruta sin recomprimir. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K138 | reencode controlado solo cuando cambia. | V1.1 — recovery/diagnóstico | V1.1 |
| K139 | golden dataset. | V1.1 — recovery/diagnóstico | V1.1 |
| K140 | visual regression. | V1.1 — recovery/diagnóstico | V1.1 |
| K141 | benchmark RAM/CPU. | V1.1 — recovery/diagnóstico | V1.1 |
| K142 | SAFE Gate: no cortar contenido válido del dataset. | V1.1 — recovery/diagnóstico | V1.1 |
| K143 | CBZBuilder. | V1 — imprescindible | V1 |
| K144 | orden determinista. | V1 — imprescindible | V1 |
| K145 | ComicInfo XML válido. | V1 — imprescindible | V1 |
| K146 | metadata mapping. | V1 — imprescindible | V1 |
| K147 | metadata por página. | V1 — imprescindible | V1 |
| K148 | generar `.cbz.tmp`. | V1 — imprescindible | V1 |
| K149 | verificar ZIP. | V1 — imprescindible | V1 |
| K150 | verificar ComicInfo. | V1 — imprescindible | V1 |
| K151 | verificar page count. | V1 — imprescindible | V1 |
| K152 | decodificar imágenes desde archivo. | V1 — imprescindible | V1 |
| K153 | reconciliar hashes. | V1 — imprescindible | V1 |
| K154 | atomic publish. | V1 — imprescindible | V1 |
| K155 | indexar DB después. | V1 — imprescindible | V1 |
| K156 | limpiar temp solo al final. | V1 — imprescindible | V1 |
| K157 | CBZ Gate con fault injection durante packaging; V1 usa casos críticos representativos y difiere la matriz exhaustiva. | V1 — imprescindible | V1 |
| K158 | Search API. | V1 — imprescindible | V1 |
| K159 | Search UI. | V1 — imprescindible | V1 |
| K160 | filtros por fuente. | V1 — imprescindible | V1 |
| K161 | cards. | V1 — imprescindible | V1 |
| K162 | detalle remoto. | V1 — imprescindible | V1 |
| K163 | lista de capítulos. | V1 — imprescindible | V1 |
| K164 | selección uno/varios/todos. | V1 — imprescindible | V1 |
| K165 | crear jobs de seleccionados. | V1 — imprescindible | V1 |
| K166 | indicador descargado/disponible. | V1 — imprescindible | V1 |
| K167 | multi-source sin fusión automática. | V1 — imprescindible | V1 |
| K168 | estados de error. | V1 — imprescindible | V1 |
| K169 | E2E Search→Job. | V1 — imprescindible | V1 |
| K170 | ReaderAssetService. | V1 — imprescindible | V1 |
| K171 | ETag/Cache-Control. | V1 — imprescindible | V1 |
| K172 | single page. | V1 — imprescindible | V1 |
| K173 | vertical. | V1 — imprescindible | V1 |
| K174 | double page. | V1 — imprescindible | V1 |
| K175 | RTL. | V1 — imprescindible | V1 |
| K176 | zoom. | V1 — imprescindible | V1 |
| K177 | fit width/height. | V1 — imprescindible | V1 |
| K178 | fullscreen. | V1 — imprescindible | V1 |
| K179 | progreso. | V1 — imprescindible | V1 |
| K180 | virtualización. | V1 — imprescindible | V1 |
| K181 | LRU cache. | V1 — imprescindible | V1 |
| K182 | modo e-reader. | V1 — imprescindible | V1 |
| K183 | teclado. | V1 — imprescindible | V1 |
| K184 | touch. | V1 — imprescindible | V1 |
| K185 | memory test 100/500 páginas. | V1 — imprescindible | V1 |
| K186 | Reader Gate desktop/mobile/Bigme-like. | V1 — imprescindible | V1 |
| K187 | UpdateService. | V1.1 — recovery/diagnóstico | V1.1 |
| K188 | diff local/remoto. | V1.1 — recovery/diagnóstico | V1.1 |
| K189 | remoto eliminado no borra local. | V1.1 — recovery/diagnóstico | V1.1 |
| K190 | metadata changed. | V1.1 — recovery/diagnóstico | V1.1 |
| K191 | hints de chapter changed. | V1.1 — recovery/diagnóstico | V1.1 |
| K192 | botón manual. | V1.1 — recovery/diagnóstico | V1.1 |
| K193 | UI `+N capítulos`. | V1.1 — recovery/diagnóstico | V1.1 |
| K194 | fallo de una fuente aislado. | V1.1 — recovery/diagnóstico | V1.1 |
| K195 | Update Gate. | V1.1 — recovery/diagnóstico | V1.1 |
| K196 | modelo de destino. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K197 | detectar drives removibles. | V1 — imprescindible | V1 |
| K198 | space preflight. | V1 — imprescindible | V1 |
| K199 | copiar como `.part`. | V1 — imprescindible | V1 |
| K200 | hash verify. | V1 — imprescindible | V1 |
| K201 | atomic rename. | V1 — imprescindible | V1 |
| K202 | skip existing. | V1 — imprescindible | V1 |
| K203 | disconnect handling. | V1 — imprescindible | V1 |
| K204 | historial. | V1 — imprescindible | V1 |
| K205 | tests USB/exFAT. | V1 — imprescindible | V1 |
| K206 | Export Gate. | V1 — imprescindible | V1 |
| K207 | política de selección. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K208 | investigación de Connector A. | V1 — imprescindible | V1 |
| K209 | fixtures A. | V1 — imprescindible | V1 |
| K210 | búsqueda A. | V1 — imprescindible | V1 |
| K211 | serie A. | V1 — imprescindible | V1 |
| K212 | capítulos A. | V1 — imprescindible | V1 |
| K213 | páginas A. | V1 — imprescindible | V1 |
| K214 | health A. | V1 — imprescindible | V1 |
| K215 | investigación B. | V1.1 — recovery/diagnóstico | V1.1 |
| K216 | Implementar Connector B: parte 1 de 5, mismo contrato aprobado que Connector A | V1.1 — recovery/diagnóstico | V1.1 |
| K217 | Implementar Connector B: parte 2 de 5, mismo contrato aprobado que Connector A | V1.1 — recovery/diagnóstico | V1.1 |
| K218 | Implementar Connector B: parte 3 de 5, mismo contrato aprobado que Connector A | V1.1 — recovery/diagnóstico | V1.1 |
| K219 | Implementar Connector B: parte 4 de 5, mismo contrato aprobado que Connector A | V1.1 — recovery/diagnóstico | V1.1 |
| K220 | Implementar Connector B: parte 5 de 5, mismo contrato aprobado que Connector A | V1.1 — recovery/diagnóstico | V1.1 |
| K221 | Real Connector Gate con fixtures y pruebas pequeñas reales. | V1.1 — recovery/diagnóstico | V1.1 |
| K222 | RecoveryManager. | V1.1 — recovery/diagnóstico | V1.1 |
| K223 | orphan scanner. | V1.1 — recovery/diagnóstico | V1.1 |
| K224 | reconciliar DB ↔ filesystem. | V1.1 — recovery/diagnóstico | V1.1 |
| K225 | Safe Mode. | V1.2 — mantenimiento/integridad | V1.2 |
| K226 | startup self-test. | V1 — imprescindible | V1 |
| K227 | backup DB. | V1.2 — mantenimiento/integridad | V1.2 |
| K228 | restore verificado. | V1.2 — mantenimiento/integridad | V1.2 |
| K229 | quick verify. | V1.2 — mantenimiento/integridad | V1.2 |
| K230 | deep verify. | V1.2 — mantenimiento/integridad | V1.2 |
| K231 | rebuild desde CBZ/ComicInfo. | V1.2 — mantenimiento/integridad | V1.2 |
| K232 | Support Bundle. | V1.3+ — hardening/madurez | V1.3+ |
| K233 | sanitizer. | V1.3+ — hardening/madurez | V1.3+ |
| K234 | Diagnostics UI. | V1.1 — recovery/diagnóstico | V1.1 |
| K235 | log rotation. | V1 — imprescindible | V1 |
| K236 | recovery fault suite. | V1.1 — recovery/diagnóstico | V1.1 |
| K237 | Recovery Gate sin pérdida de datos. | V1.1 — recovery/diagnóstico | V1.1 |
| K238 | dataset 100 capítulos/~2GB. | V1.3+ — hardening/madurez | V1.3+ |
| K239 | concurrency 1/2/4/6/8. | V1.3+ — hardening/madurez | V1.3+ |
| K240 | CPU. | V1.3+ — hardening/madurez | V1.3+ |
| K241 | RAM. | V1.3+ — hardening/madurez | V1.3+ |
| K242 | disk. | V1.3+ — hardening/madurez | V1.3+ |
| K243 | handles. | V1.3+ — hardening/madurez | V1.3+ |
| K244 | reader benchmark. | V1.3+ — hardening/madurez | V1.3+ |
| K245 | library benchmark. | V1.3+ — hardening/madurez | V1.3+ |
| K246 | leak test. | V1.3+ — hardening/madurez | V1.3+ |
| K247 | startup. | V1 — imprescindible | V1 |
| K248 | shutdown. | V1 — imprescindible | V1 |
| K249 | crash stress. | V1.3+ — hardening/madurez | V1.3+ |
| K250 | network stress. | V1.3+ — hardening/madurez | V1.3+ |
| K251 | disk full. | V1 — imprescindible | V1 |
| K252 | DB locked. | V1.3+ — hardening/madurez | V1.3+ |
| K253 | soak test. | V1.3+ — hardening/madurez | V1.3+ |
| K254 | ajustar defaults por eficiencia. | V1 — imprescindible | V1 |
| K255 | RC0 Gate y freeze de features. | V1 — imprescindible | V1 |
| K256 | Vite production build. | V1 — imprescindible | V1 |
| K257 | FastAPI sirve frontend compilado. | V1 — imprescindible | V1 |
| K258 | PyInstaller. | V1 — imprescindible | V1 |
| K259 | `Yomira.exe`. | V1 — imprescindible | V1 |
| K260 | Inno Setup. | V1 — imprescindible | V1 |
| K261 | separar programa/datos. | KEEP ARCHITECTURAL FOUNDATION | V1 — contrato/diseño y uso indicado en secciones 2–5 |
| K262 | upgrade sin borrar biblioteca. | V1 — imprescindible | V1 |
| K263 | desinstalación con conservación opcional. | V1 — imprescindible | V1 |
| K264 | firewall UX solo al activar LAN. | V1 — imprescindible | V1 |
| K265 | single instance empaquetado. | V1 — imprescindible | V1 |
| K266 | instalación en Windows limpio sin Python/Node. | V1 — imprescindible | V1 |
| K267 | upgrade test. | V1 — imprescindible | V1 |
| K268 | rollback test. | V1.2 — mantenimiento/integridad | V1.2 |
| K269 | Packaging Gate. | V1 — imprescindible | V1 |
| K270 | source freeze. | V1 — imprescindible | V1 |
| K271 | TypeScript Gate. | V1 — imprescindible | V1 |
| K272 | ESLint Gate. | V1 — imprescindible | V1 |
| K273 | Ruff Gate. | V1 — imprescindible | V1 |
| K274 | Pyright Gate. | V1 — imprescindible | V1 |
| K275 | Unit Gate. | V1 — imprescindible | V1 |
| K276 | Integration Gate. | V1 — imprescindible | V1 |
| K277 | E2E Gate. | V1 — imprescindible | V1 |
| K278 | Fault Injection Gate. | V1.3+ — hardening/madurez | V1.3+ |
| K279 | Benchmark Gate. | V1.3+ — hardening/madurez | V1.3+ |
| K280 | Migration/Restore Gate. | V1.2 — mantenimiento/integridad | V1.2 |
| K281 | Windows/USB/LAN Gate. | V1 — imprescindible | V1 |
| K282 | Known Issues Review. | V1 — imprescindible | V1 |
| K283 | Release Candidate final. | V1 — imprescindible | V1 |
| K284 | Yomira 1.0. | V1 — imprescindible | V1 |

## 6. Elementos trasladados a V1.1

V1.1 completa RecoveryCoordinator y salidas durables, snapshots de diagnóstico por job, reconciliación DB/filesystem y clasificación de estados ambiguos; replay/checkpoints avanzados; mejoras de observabilidad; backpressure RAM/I/O con pausa segura e histéresis; SAFE crop tras validar golden fixtures; update checker y Connector B. La cuarentena se diferencia de recovery completado.

Pueden esperar porque V1 preserva evidencia ante incertidumbre y resuelve reinicios normales con estado por-job, CAS y archivos verificados. Los límites estáticos mantienen recursos acotados. K094 conserva su ID: el startup CAS obligatorio ya se ejecuta en V1; el análisis de generaciones, sidecars, checkpoints y replay completo se entrega y pasa su gate en V1.1. ADR-0079/0080 y ADR-0075 permanecen vigentes como contratos de la versión asignada; no se marcan PASS por diferimiento.

### Pruebas de ADR-0079/K094/K096 aplazadas a V1.1

La implementación íntegra de estas pruebas no bloquea la primera prueba funcional V1, pero son gates obligatorios antes de declarar completa V1.1 o activar recovery avanzado:

- Enumeración paginada/acotada de active, INTERRUPTED, recovery_blocked, publication_pending y QUEUED con cancelación pendiente; snapshot read-only cerrado antes de adquirir el mutex de cada job y mutar.
- Reinicio antes y después de cada frontera durable: CAS de interrupción, commit de intención, creación/flush de staging, MoveFileExW/ReplaceFileW, verificación, cierre del marcador, backup retenido y cleanup. Repetir recovery debe converger sin perder ni mutar evidencia ya reconciliada.
- Matriz target/backup/staging × DB, incluidos jobs legacy, manifiesto futuro/malformado/truncado, sidecar huérfano/duplicado, reparse point, ruta/permisos/IO ambiguo y mismatch de digest/tamaño. Ningún candidato se escoge por fecha.
- Cada salida de estado: reanudar sólo checkpoint verificado; devolver a QUEUED sólo si no hay contradicción/cancelación; FAILED sólo ante contradicción demostrada con bytes preservados; CANCELLED sólo con evidencia de cancelación segura; terminales nunca se reabren. Cubrir cancelación pendiente en PROCESSING/PACKAGING.
- CAS obsoleto/concurrente compara snapshot completo (status, updated_at_us, cancelación, interrupted_from_status, recovery_blocked y generación/hash/tamaño/pendiente del manifest). Todos los escritores serializan bajo el mismo job mutex.
- SQLITE_FULL/disk full cierran admisión mutante antes de nuevos claims/escrituras, revierten o cierran la transacción actual, esperan quiescencia de workers, conservan archivos, evitan cancelar por error ambiental y reabren sólo con DB/espacio verificados; DiskBudgetManager no sobreasigna reservas entre lotes.
- download_manifest_state sólo autoriza replay si generación y hash/tamaño coinciden exactamente con la proyección canónica de DB. Verificar backup anterior y target nuevo byte/hash; completar mediante CAS. Sin intención durable no se promueve ni se reemplaza staging reconstruyendo desde DB.
- K096/K278/K284 comparan enumerados y clasificados, incluyen bloqueados/no despachables en los conteos de recovery y fallan mientras un caso avanzado carezca de una salida durable, segura y verificable.
### Pruebas de las capacidades diferidas

- K090: inyectar RAM crítica, I/O saturado y monitor no disponible; medir pausa en safe point, no admitir jobs nuevos, reanudar con histéresis, recuperar después de interrupción, conservar fairness y confirmar que no deja artefactos parciales.
- K066, si un connector usa secretos persistentes: probar almacenamiento sin plaintext, lectura autorizada tras reinicio, secreto corrupto o DPAPI no disponible que conserva ciphertext y falla cerrado, y exclusión de secretos de config/log/bundle; verificar expiración y revocación conforme a ADR-0053.
- K131–K142: golden pages y visual regression; páginas dobles, long strips y contenido al borde no se cortan; duda produce NO_CROP; no reencode si no cambia; cotas RAM/CPU y gate sin falsos PASS críticos.
- K187–K195/K215–K221: fallo de una fuente no afecta otra; Connector B cumple los mismos contratos/fixtures y no elude controles de acceso.


K130 conserva la frontera/modos y OFF es el default; K137 preserva bytes originales sin reencode. K131–K136 y K138–K142 requieren SAFE processor, golden dataset, visual regression y prueba contra recorte indebido antes de activarse.

## 7. Elementos trasladados a V1.2

V1.2 entrega K047 VACUUM y PRAGMA optimize en cierre seguro después de detener escrituras/cerrar transacciones o mediante proceso programado con acceso exclusivo; chequeo de espacio, trazas de resultado/fallo, base utilizable ante fallo e integrity_check. También K225 Safe Mode completo, backups recurrentes/retención, restore verificado, quick/deep verify, rebuild desde CBZ/ComicInfo, DiskBudgetManager avanzado, cleanup recovery-aware y rollback avanzado.

El backup verificado pre-upgrade/migración soportada permanece en V1. La instalación V1 no limpia ni reemplaza data root y no degrada destructivamente schema. Herramientas generales de backup/restore y vacuum pueden esperar, porque el flujo nuevo no requiere consola de administración si migraciones, cierre de sesiones, preflight y conservación de bytes son correctos.

Gate V1.2 verifica DB bloqueada/llena y falta de espacio con backup restaurable, evidencia antes/después, integrity_check, retención acotada, rebuild sin duplicar assets y cleanup que no toca elementos ambiguos. K047 añade DB sintética con alta volatilidad de jobs, ciclo de cierre/reinicio, DB bloqueada, espacio insuficiente, registro de resultado/fallo de VACUUM y PRAGMA optimize, y PRAGMA integrity_check posterior; un mantenimiento fallido debe dejar la DB utilizable.

## 8. Elementos trasladados a V1.3+

V1.3+ entrega Support Bundle avanzado y sanitizer extensivo; dataset de 100 capítulos/~2GB; benchmarks concurrentes, CPU/RAM/disco/handles/reader/library; leak/soak/crash/network stress amplios; combinaciones exhaustivas de DB lock y filesystem; fault injection exhaustivo (incluida la matriz ampliada de K157); benchmark gate de bibliotecas gigantes; threat model completo, SBOM, supply-chain hardening, signing, update/rollback sofisticado y observabilidad de producción según tareas y SPEC.

Pueden esperar a evidencia real de uso porque no son necesarios para demostrar el flujo principal en la máquina objetivo. Se conservan como obligaciones. El release V1 aún exige lint/types/tests críticos, fallos representativos, cotas medibles, Windows limpio, cero P0/P1 y known-issues review. No se habilita distribución pública que dependa de signing/SBOM antes de pasar esos gates.

## 9. Dependencias arquitectónicas futuras

1. API/manifest versionados, migraciones aditivas, estados/IDs estables, rutas relativas; nunca inferir checkpoint desde timestamps o orden del filesystem.
2. DownloadJob conserva identity, idempotency, cancelación y interrupted_from_status para que recovery posterior amplíe el esquema sin reinterpretar jobs.
3. Aislar manifest writer, atomic publisher, storage providers y locks; no acoplar la función principal a una forma concreta de sidecar.
4. Separar puntos seguros entre download, validate, transform y packaging. El transformer se conecta después, nunca cambia original ni se activa en OFF.
5. Validadores independientes de connector; los conectores no poseen DB, rutas, secretos ni permiso de escribir library.
6. Reservar interfaz opaca SecretVault. DPAPI decide descifrado; settings guarda sólo referencia y logs/bundles no reciben secreto. Un connector con secreto exige K066 completo antes de habilitarse.
7. Separar ejecutable/config/secrets/DB/library/temporales; versionar API/schema y nunca borrar/downgradear datos en upgrade.
8. Job dispatcher acepta límites estáticos y una interfaz futura para monitor de presión. V1 nunca interpreta telemetría ausente como recursos ilimitados.
9. Catálogo de errores estable con código/etapa/trace; diagnóstico avanzado no expone URLs, tokens, cookies, rutas personales o bytes.
10. Preservar CBZ/ComicInfo/hash/publication boundary/Asset/progreso entre recovery, backups, update y rebuild.
11. Fixtures usan providers de root/clock/HTTP/resource inyectables para pruebas futuras sin meter toda la infraestructura al primer release.

## 10. Nuevo Release Gate de V1

V1 pasa sólo si todos los criterios están PASS con un reporte reproducible (comando/entorno/versión, logs sanitizados, referencia de pruebas; la prueba manual conserva modelo/pasos/resultado).

| Gate | Criterio requerido |
|---|---|
| Lint/build | ESLint, build frontend production, Ruff PASS. |
| Tipos | TypeScript strict y Pyright PASS. |
| Unit tests críticos | Jobs/state, migraciones, límites, errores, connector contracts, hashes, validación, ComicInfo y reader progress PASS. |
| Integración del pipeline | Mock Source → API/Job → descarga .part → hash/validación → CBZ/ComicInfo → verify → publish → DB/library PASS sin internet. |
| CBZ | ZIP/XML/page count/orden/hashes verificados; no publicar ni indexar ante error; atomicidad probada. |
| E2E principal | Buscar → seleccionar obra/capítulo → job → descargar → biblioteca → leer → guardar progreso PASS. |
| K066 Secret Vault (si se usa connector autenticado) | ciphertext DPAPI separado de settings, sin plaintext en logs/config/bundle; lectura tras reinicio, secreto corrupto/no disponible conserva blob y falla cerrado. |
| K094 startup operativo | Mutex singleton antes de DB; enumeración en lotes acotados; CAS individual por job activo con interrupted_from_status; no UPDATE masivo. Queued/terminal se conservan. Reanuda sólo punto/archivos verificados; etapa ambigua o cancelación no reversible queda INTERRUPTED/bloqueada y preservada. |
| K096 admisión y CBZ normal | El primer flujo VALIDATING/PACKAGING puede completar con validación/atomic publisher V1; dispatch excluye bloqueados. Tras reinicio ambiguo no hay replay heurístico, se preserva el job. Pruebas representativas fallan si se expone un CBZ parcial. |
| Cierre/reapertura | Cierre normal y reinicio representativo; biblioteca/progreso persisten. Startup procesa activos en lotes acotados y CAS por fila con origen conservado, sin UPDATE masivo; sólo reanuda artefactos verificados; no duplica publicados. Ambiguos permanecen preservados y visibles. |
| Bigme/e-reader | Prueba manual o automatizada en Bigme compatible; registrar modelo/firmware/pasos/resultado. |
| LAN básica | OFF por default; opt-in habilita lectura/consulta autorizada, autenticación/firewall pasan; fallo LAN no rompe acceso local. |
| Windows limpio | Instalación sin Python/Node, arranque/migraciones, descarga fixture, lectura y cierre/reapertura. |
| Filesystem | En Windows, lock transitorio liberado/persistente y locks de escritura/rename: retry exponencial sólo para sharing/lock hasta cota; permisos denegados/disk full no se reintentan inútilmente. Error trazable, bytes previos intactos y ningún CBZ final parcial. |
| Recursos | Límites de cola/workers/lectura/cache/logs; smoke run sin crecimiento ilimitado. |
| Upgrade soportado | Si V1 soporta upgrade, backup preflight verificado, migración compatible y biblioteca intacta; fallo en preflight no modifica data. |
| Known issues | Limitaciones recovery avanzado documentadas; sin corrupción/P0/P1 abiertos; job ambiguo visible/preservado, no reportado como recovery completo. |
| Decisión final | Fuente/version fijadas, instalable reproducible, review independiente y reporte V1 aprobado. |

No bloquean V1 por sí solos fuzzing amplio, stress gran escala, leak/soak prolongado, benchmark gigante, restore general/rebuild, snapshots persistentes K095 ni clasificación perfecta de sidecars ambiguos. Cualquier regresión que afecte flujo, datos, privacidad o recursos acotados sí bloquea.

## 11. Roadmap

| Etapa | Resultado para el usuario | Capacidades |
|---|---|---|
| V1 | Instalar/probar buscar → descargar → validar → CBZ → biblioteca → leer → reiniciar. | Preservación ante fallos esperables, jobs básicos persistentes, CAS de startup por fila, publicación atómica, límites estáticos, Windows/Bigme/LAN opt-in. |
| ↓ V1.1 | Explica incidentes y resuelve más reinicios; procesamiento seguro opcional. | RecoveryCoordinator/replay, snapshots durables, backpressure dinámico, transforms auditados, Connector B y updates. |
| ↓ V1.2 | Administración verificable de integridad y almacenamiento. | Backup/restore/retention, rebuild/deep verify, DiskBudgetManager, VACUUM/optimize, Safe Mode y rollback. |
| ↓ V1.3+ | Madurez para distribución y uso intensivo. | Fuzz/stress/benchmarks, SBOM/signing/threat model, update sofisticado, support bundle y observabilidad avanzada. |

Secuencia: V1 → V1.1 → V1.2 → V1.3+. Ningún requisito se salta; se conserva en la etapa que activa su funcionalidad. V1 hace correctamente el trabajo principal y permite empezar pruebas sin exigir toda la resistencia empresarial antes del primer producto.






