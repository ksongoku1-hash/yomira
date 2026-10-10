# YOMIRA — MASTER EXECUTION PLAN V1.1

**Documento:** `YOMIRA_MASTER_EXECUTION_PLAN.md`  
**Proyecto:** Yomira  
**Objetivo:** convertir la especificación técnica aprobada en un plan de ejecución completo, secuencial y verificable.  
**Modelo de trabajo:** Luna Max / Principal Software Architect & Lead Developer  
**Regla superior:** integridad > trazabilidad > recuperación > recursos > velocidad.  
**Regla de ejecución:** ninguna fase puede comenzar si la anterior no ha alcanzado estado `PASS`.

---

# 0. REGLAS GENERALES DE EJECUCIÓN

## 0.1 Flujo obligatorio por tarea

```text
TAREA
  ↓
PLAN BREVE
  ↓
IMPLEMENTACIÓN
  ↓
TESTS
  ↓
QUALITY GATE
  ↓
REVISIÓN DEL DIFF
  ↓
COMMIT
  ↓
SIGUIENTE TAREA
```

Si cualquiera de los pasos falla, no se avanza.

## 0.2 Regla de bugs

```text
REPRODUCIR
  ↓
CREAR TEST QUE FALLE
  ↓
IDENTIFICAR CAUSA RAÍZ
  ↓
CORREGIR
  ↓
TEST PASA
  ↓
SUITE COMPLETA
  ↓
REGRESSION TEST PERMANENTE
  ↓
COMMIT
```

Queda prohibido aplicar cambios sin reproducir, silenciar excepciones, eliminar tests para hacer pasar un build, desactivar TypeScript strict, introducir `any` indiscriminadamente, añadir `# type: ignore` sin justificar, saltar validaciones de integridad por rendimiento o aceptar “funciona en mi PC” como criterio de aprobación.

## 0.3 Severidad de bugs

| Nivel | Significado | Efecto |
|---|---|---|
| P0 | pérdida/destrucción de datos | bloquea todo |
| P1 | corrupción/integridad | bloquea release |
| P2 | función principal rota | bloquea fase/release |
| P3 | problema menor | puede continuar con ticket |
| P4 | cosmético | no bloqueante |

## 0.4 Convención de tareas

Las tareas usan identificadores permanentes `K001 ... K288`. Una tarea eliminada no reutiliza su ID. Las enmiendas aprobadas pueden agregar IDs al final y declararlos como prerrequisitos de un gate terminal anterior; el orden de ejecución sigue las dependencias, no el valor numérico aislado del ID.

## 0.5 Artefactos de cada fase

Cada fase debe producir `docs/phases/PHASE_XX_REPORT.md` con tareas ejecutadas, PASS/FAIL, tests, lint, typecheck, benchmarks relevantes, bugs encontrados/corregidos, known issues y decisión final.

---

# PARTE 1 — FASES 0 A 3

# FASE 0 — CONSTITUCIÓN, ARQUITECTURA Y REGLAS

**Tareas:** K001–K004  
**Objetivo:** congelar la constitución técnica antes de escribir software funcional.

## K001 — Aprobar especificación técnica

**Objetivo:** convertir `YOMIRA_V1_SPEC.md` en la autoridad máxima del proyecto.

**Archivos permitidos**
```text
docs/YOMIRA_V1_SPEC.md
docs/glossary.md
```

**Instrucciones**
1. Revisar cada requisito.
2. Confirmar que no existan contradicciones.
3. Eliminar TODO arquitectónicos bloqueantes.
4. Identificar requisitos con familias `REQ-CORE`, `REQ-DB`, `REQ-JOB`, `REQ-DL`, `REQ-SRC`, `REQ-IMG`, `REQ-CBZ`, `REQ-UI`, `REQ-READ`, `REQ-LAN`, `REQ-SEC`, `REQ-REC`, `REQ-EXP`, `REQ-DIAG`, `REQ-PERF`, `REQ-REL`.

**Tests:** revisión manual de consistencia, estados, recuperación, seguridad y límites de V1.

**PASS:** `Status: APPROVED`.

## K002 — ADRs iniciales

**Objetivo:** documentar decisiones fundamentales.

Crear ADRs para React+TypeScript, FastAPI+Python, SQLite WAL, CBZ por capítulo, publicación atómica, Connector Isolation, Job State Machine, Relative Paths, SAFE Processing, Local-first/LAN, Recovery Manager, Reader Asset Streaming, Metadata Authority, Chapter Versioning, Disk Budgeting, Safe Mode, Single Instance, Typed Settings, Dependency Policy y Release Strategy.

**PASS:** todos en `Accepted`.

## K003 — Ownership y dependencias

Crear:
```text
docs/ARCHITECTURE.md
docs/COMPONENT_OWNERSHIP.md
docs/DEPENDENCY_RULES.md
```

| Componente | Puede | No puede |
|---|---|---|
| Connector | obtener/normalizar | escribir biblioteca |
| Downloader | transferir | crear CBZ |
| Processor | transformar | hacer HTTP |
| CBZ Builder | empaquetar | consultar web |
| Job Manager | coordinar | parsear HTML |
| React | solicitar | tocar filesystem |

**PASS:** cada módulo define input, output, ownership, dependencias permitidas/prohibidas y failure boundary.

## K004 — Error model, logging y bugs

Crear:
```text
docs/ERROR_CODES.md
docs/BUG_PROTOCOL.md
docs/LOGGING_SPEC.md
docs/SUPPORT_BUNDLE_SPEC.md
```

Familias: `SYS_`, `DB_`, `SRC_`, `DL_`, `IMG_`, `CBZ_`, `LIB_`, `READ_`, `EXP_`, `AUTH_`, `NET_`.

**PASS:** cualquier P0/P1 permite identificar qué, dónde, cuándo, job, componente, etapa, retryable y trace ID.

---

# FASE 1 — DESIGN SYSTEM Y UX

**Tareas:** K005–K013  
**Objetivo:** congelar diseño y comportamiento visual antes de React funcional.

## K005 — Design System
Definir colores, tipografía, spacing, radius, estados, focus, breakpoints y touch targets.

Paleta:
```text
#0B0D10 fondo
#15191F tarjeta
#F4F6F8 texto
#00D66B acento
```

## K006 — Layout desktop
Top bar sticky, Home, Library, Search, Downloads, Settings y Diagnostics. Objetivo 5–7 portadas por fila.

## K007 — Layout tablet
4 portadas por fila, navegación comprimida y funciones completas.

## K008 — Layout mobile
2–3 portadas, navegación adaptable y botones táctiles.

## K009 — Modo e-reader
Sin blur, sin animaciones, transiciones mínimas, alto contraste, targets grandes y precarga reducida.

## K010 — Componentes
Especificar TopNavigation, SearchInput, SectionHeader, MangaCard, MangaCarousel, FilterChip, SourceChip, ChapterRow, DownloadStatus, ProgressBar, EmptyState, ErrorState, SkeletonCard, Modal, Toast y ReaderToolbar.

## K011 — Estados extremos
0 resultados, 5000 capítulos, títulos largos, sin portada, fuente caída, carga lenta, error y empty state.

## K012 — Accesibilidad
Focus visible, teclado, aria-label, contraste, zoom y no depender únicamente de color.

## K013 — UX Review Gate
**PASS:** todos los layouts y estados están definidos sin necesidad de improvisación durante implementación.

---

# FASE 2 — TOOLING Y ESQUELETO EJECUTABLE

**Tareas:** K014–K022  
**Objetivo:** levantar React + FastAPI con quality gate, sin lógica de manga.

## K014 — Estructura física
Crear `frontend/`, `backend/`, `fixtures/`, `benchmark/`, `docs/`, `scripts/`, `installer/`, `tests/`.

## K015 — Congelar runtimes
Definir Python estable, Node LTS, npm, lockfiles y archivos de versión. Build reproducible.

## K016 — Tooling backend
Configurar Ruff, Pyright y Pytest. Todos deben pasar.

## K017 — FastAPI mínimo
Crear:
```text
GET /api/v1/system/liveness
GET /api/v1/system/version
```
Tests 200, schemas y 404.

## K018 — Frontend React mínimo
React, TypeScript strict, Vite, Router, Query, Zustand, CSS Modules, ESLint y Vitest.

## K019 — App Shell
Navegación base y tema oscuro. Sin funciones de manga.

## K020 — API Client tipado
Centralizar base URL, JSON, errores, trace ID y version check. Prohibido `fetch` disperso.

## K021 — Logging y errores base
Crear `SYS_INTERNAL_ERROR`, `SYS_STARTUP_FAILED`, `SYS_INVALID_CONFIG`, `API_VALIDATION_FAILED`.

## K022 — Unified Quality Gate
Ejecutar TypeScript, ESLint, Vitest, build frontend, Ruff, Pyright, Pytest y smoke integration.

**PHASE 2 PASS:** cero errores críticos y cero tests fallando.

---

# FASE 3 — BACKEND CORE

**Tareas:** K023–K033  
**Objetivo:** crear infraestructura interna del backend.

- **K023 — Application Factory:** separar creación de FastAPI de `main.py`.
- **K024 — Configuración tipada:** Settings central.
- **K025 — Environment handling:** dev/test/prod.
- **K026 — Liveness/Readiness:** proceso vivo vs listo.
- **K027 — Error middleware:** errores API uniformes.
- **K028 — Trace middleware:** trace ID por request.
- **K029 — Structured logging:** logs estructurados.
- **K030 — Startup lifecycle:** inicialización controlada.
- **K031 — Shutdown lifecycle:** cierre seguro.
- **K032 — Single instance baseline:** impedir dos backends simultáneos.
- **K033 — Backend Core Gate:** startup/shutdown repetido y pruebas de errores.

---

# PARTE 2 — FASES 4 A 7

# FASE 4 — BASE DE DATOS Y DOMINIO

**Tareas:** K034–K048

- **K034:** SQLite engine con WAL, FK y busy timeout.
- **K035:** SQLAlchemy base y separación de modelos.
- **K036:** Alembic obligatorio.
- **K037:** tabla/dominio Series.
- **K038:** Sources.
- **K039:** SeriesSource.
- **K040:** Chapters sin `float`.
- **K041:** ChapterSource.
- **K042:** Assets.
- **K043:** DownloadJob.
- **K044:** DownloadPages.
- **K045:** ReaderProgress.
- **K046:** Settings y SchemaHistory.
- **K047:** Mantenimiento de SQLite (VACUUM y PRAGMA optimize).
  - Implementar ambos comandos para mitigar la fragmentación causada por la alta volatilidad de los estados de jobs.
  - Integrarlos al cierre seguro después de detener escrituras y cerrar transacciones, o ejecutarlos mediante un proceso programado con acceso exclusivo.
  - Comprobar espacio disponible antes de VACUUM, registrar resultado/fallo y conservar la DB utilizable si el mantenimiento no puede completarse.
  - Tests: DB sintética con alta volatilidad, cierre/reinicio, DB bloqueada, espacio insuficiente y PRAGMA integrity_check posterior.
- **K048:** DB Gate: migraciones, rollback, constraints, FK e integrity check.

# FASE 5 — BIBLIOTECA LOCAL

**Tareas:** K049–K058

- **K049:** LibraryService.
- **K050:** Library API.
- **K051:** portadas como Assets.
- **K052:** ThumbnailService small/medium/large.
- **K053:** UI de biblioteca tipo Webtoon dark.
- **K054:** detalle de serie.
- **K055:** filtros Todos/Leyendo/Completos/Incompletos.
- **K056:** autoridad de metadata `USER_OVERRIDE > LOCAL > SOURCE`.
- **K057:** mapeo de rutas relativas.
- **K058:** Library Gate con 100, 1000 y 10000 series sintéticas.

# FASE 6 — SOURCE CONNECTOR SDK

**Tareas:** K059–K070

- **K059:** interfaz `SourceConnector`.
- **K060:** metadata del connector: ID, versión, capacidades.
- **K061:** contrato Search.
- **K062:** contrato metadata de serie.
- **K063:** contrato de capítulos complejos.
- **K064:** contrato de pages.
- **K065:** errores `SRC_*`.
- **K066:** Secret Vault local.
  - Implementar almacenamiento local cifrado en reposo, protegido por Windows (DPAPI o Windows Credential Manager; decisión concreta registrada en ADR).
  - Guardar tokens de sesión y cookies de larga duración cuando una fuente los requiera; configuración y settings en texto plano contienen solo referencias opacas.
  - Exponer acceso únicamente mediante la interfaz del Vault; secretos no aparecen en logs, exports de configuración ni Support Bundle y nunca hay fallback a texto plano.
  - Definir manejo de expiración, revocación, backup/restauración y fallo de descifrado sin exponer el secreto.
  - Tests: inspección de almacenamiento cifrado, lectura autorizada, reinicio, secreto corrupto/no disponible y exclusión del Support Bundle.
- **K067:** sandbox: sin DB raw ni escritura directa.
- **K068:** registry controlado.
- **K069:** MockConnector.
- **K070:** Connector SDK Gate con contract tests.

# FASE 7 — MOCK MANGA SERVER

**Tareas:** K071–K080

- **K071:** servidor falso local.
- **K072:** modo normal.
- **K073:** modo lento.
- **K074:** 404/429/500.
- **K075:** desconexión a mitad de página.
- **K076:** HTML-as-image y JPEG corrupto.
- **K077:** image bomb.
- **K078:** metadata inválida.
- **K079:** páginas duplicadas.
- **K080:** Mock Gate: escenarios deterministas y reproducibles.

---

# PARTE 3 — FASES 8 A 11

# FASE 8 — JOB MANAGER

**Tareas:** K081–K096

- **K081:** dominio Job.
- **K082:** state machine.
- **K083:** guard de transiciones ilegales.
- **K084:** persistencia.
- **K085:** cola acotada.
- **K086:** prioridades HIGH/NORMAL/LOW.
- **K087:** fairness global/source/host.
- **K088:** cancelación semántica.
- **K089:** pause/resume donde sea seguro.
- **K090:** Backpressure y monitoreo de recursos.
  - Medir RAM disponible e I/O del disco con consumo de monitoreo acotado y umbrales críticos definidos.
  - Al alcanzar saturación crítica, pausar proactivamente jobs en curso en puntos seguros y limitar admisión de nuevo trabajo hasta recuperar margen.
  - Registrar métrica, umbral, job, etapa y razón de pausa; reanudar mediante transiciones seguras cuando los recursos vuelvan a niveles operables.
  - Tests: presión de RAM, saturación I/O, pausa/reanudación, recuperación tras interrupción, ausencia de artefactos parciales y comprobación de fairness.
- **K091:** estado INTERRUPTED.
- **K092:** manifest versionado.
- **K093:** manifest atomic persistence.
- **K094:** metadata de recovery.
- **K095:** diagnósticos del job.
- **K096:** Job Manager Gate con fault tests.

# FASE 9 — MOTOR DE DESCARGAS

**Tareas:** K097–K116

- **K097:** `httpx.AsyncClient` central con pooling.
- **K098:** streaming por chunks a `.part`.
- **K099:** SHA-256 al vuelo.
- **K100:** timeouts separados.
- **K101:** retry 1s/3s/8s inicial.
- **K102:** handling 429/Retry-After.
- **K103:** mapeo HTTP → `DL_*`.
- **K104:** Circuit Breaker.
- **K105:** semaphore por host.
- **K106:** semaphore global.
- **K107:** safe rename `.part` → final.
- **K108:** OS Lock Handling para operaciones críticas de filesystem.
  - Aplicar retry con exponential backoff acotado a todas las operaciones críticas de filesystem, incluidas escritura/flush, rename/move y publicación atómica, cuando fallen por locks transitorios de Windows.
  - Clasificar errores de sharing/lock (por antivirus o indexadores) como transitorios; permisos, disk full y rutas inválidas fallan sin reintentos inútiles.
  - Preservar temporales y contenido existente hasta que la operación tenga éxito; emitir error trazable al agotar intentos.
  - Tests Windows: lock transitorio liberado, lock persistente, rename y escritura bloqueados, antivirus/indexador simulado, permiso denegado y disco lleno sin pérdida ni publicación parcial.
- **K109:** resume reutilizando páginas válidas.
- **K110:** duplicate job guard.
- **K111:** disk preflight.
- **K112:** progreso bytes/páginas.
- **K113:** cancelación de streams.
- **K114:** recovery de `.part`.
- **K115:** network fault tests.
- **K116:** Download Engine Gate con stress e integridad.

# FASE 10 — VALIDACIÓN DE IMÁGENES

**Tareas:** K117–K129

- **K117:** MIME.
- **K118:** magic bytes.
- **K119:** decode con Pillow.
- **K120:** límites de dimensiones.
- **K121:** límites de píxeles.
- **K122:** detección de truncamiento.
- **K123:** duplicados por hash.
- **K124:** anomalías relativas de resolución.
- **K125:** page count.
- **K126:** STANDARD/DOUBLE/LONG/UNKNOWN.
- **K127:** VERIFIED/WARNINGS/FAILED.
- **K128:** fixtures corruptos.
- **K129:** Validation Gate con cero falsos PASS críticos.

# FASE 11 — PROCESAMIENTO SAFE

**Tareas:** K130–K142

- **K130:** modos OFF/SAFE/E_READER.
- **K131:** border scanner.
- **K132:** confidence score.
- **K133:** safety margin.
- **K134:** abort rules: duda = no crop.
- **K135:** protección de double pages.
- **K136:** tratamiento long-strip/webtoon.
- **K137:** ruta sin recomprimir.
- **K138:** reencode controlado solo cuando cambia.
- **K139:** golden dataset.
- **K140:** visual regression.
- **K141:** benchmark RAM/CPU.
- **K142:** SAFE Gate: no cortar contenido válido del dataset.

---

# PARTE 4 — FASES 12 A 15

# FASE 12 — CBZ + COMICINFO

**Tareas:** K143–K157

- **K143:** CBZBuilder.
- **K144:** orden determinista.
- **K145:** ComicInfo XML válido.
- **K146:** metadata mapping.
- **K147:** metadata por página.
- **K148:** generar `.cbz.tmp`.
- **K149:** verificar ZIP.
- **K150:** verificar ComicInfo.
- **K151:** verificar page count.
- **K152:** decodificar imágenes desde archivo.
- **K153:** reconciliar hashes.
- **K154:** atomic publish.
- **K155:** indexar DB después.
- **K156:** limpiar temp solo al final.
- **K157:** CBZ Gate con fault injection durante packaging.

# FASE 13 — BÚSQUEDA Y CAPÍTULOS

**Tareas:** K158–K169

- **K158:** Search API.
- **K159:** Search UI.
- **K160:** filtros por fuente.
- **K161:** cards.
- **K162:** detalle remoto.
- **K163:** lista de capítulos.
- **K164:** selección uno/varios/todos.
- **K165:** crear jobs de seleccionados.
- **K166:** indicador descargado/disponible.
- **K167:** multi-source sin fusión automática.
- **K168:** estados de error.
- **K169:** E2E Search→Job.

# FASE 14 — READER

**Tareas:** K170–K186

- **K170:** ReaderAssetService.
- **K171:** ETag/Cache-Control.
- **K172:** single page.
- **K173:** vertical.
- **K174:** double page.
- **K175:** RTL.
- **K176:** zoom.
- **K177:** fit width/height.
- **K178:** fullscreen.
- **K179:** progreso.
- **K180:** virtualización.
- **K181:** LRU cache.
- **K182:** modo e-reader.
- **K183:** teclado.
- **K184:** touch.
- **K185:** memory test 100/500 páginas.
- **K186:** Reader Gate desktop/mobile/Bigme-like.

# FASE 15 — UPDATE CHECKER

**Tareas:** K187–K195

- **K187:** UpdateService.
- **K188:** diff local/remoto.
- **K189:** remoto eliminado no borra local.
- **K190:** metadata changed.
- **K191:** hints de chapter changed.
- **K192:** botón manual.
- **K193:** UI `+N capítulos`.
- **K194:** fallo de una fuente aislado.
- **K195:** Update Gate.

---

# PARTE 5 — FASES 16 A 19

# FASE 16 — EXPORTACIÓN AL E-READER

**Tareas:** K196–K206; extensión CBR K285–K288 (ADR-0121/0122)

- **K196:** modelo de destino.
- **K197:** detectar unidades removibles con letra y almacenamiento de lectores Windows WPD/MTP sin letra.
- **K198:** space preflight.
- **K199:** copiar como `.part`.
- **K200:** hash verify.
- **K201:** publicación sin reemplazo para unidades montadas; en WPD/MTP, publicar solo después de verificar el objeto temporal y cambiar su nombre visible.
- **K202:** skip existing.
- **K203:** manejar desconexión preservando el temporal y el origen para unidades montadas y WPD/MTP.
- **K204:** historial.
- **K205:** tests de USB/exFAT y transporte WPD/MTP simulado, incluidos timeout, error de protocolo y caída del proceso COM aislado.
- **K206:** Export Gate de los dos transportes; enumeración y exportación WPD/MTP se aíslan del proceso API, y el gate MTP requiere prueba física registrada en lector compatible.
- **K285:** cerrar revisión de formato, dependencias, licencia y distribución según ADR-0122 antes de generar CBR; confirmar que el build no requiere WinRAR/RAR instalado ni redistribuye código o ejecutables de RARLAB.
- **K286:** implementar exportación agregada `.cbr` mediante escritor propio RAR5 store-only, con páginas PNG, máximo 2 GiB y 10.000 páginas; reservar hasta 2 GiB para PNG temporales y 2 GiB para el archivo final, conservar intactos CBZ canónicos, aplicar orden estable, staging y publicación atómica, y abortar sin publicar ante cualquier fallo.
- **K287:** CBR Integrity Gate: inspeccionar formato/cabeceras/CRC con el verificador interno; extraer con lector independiente en el entorno de desarrollo; verificar miembros, nombres, cantidad, orden, decodificación y hash RGBA; cubrir interrupción, disco lleno, colisión y recuperación sin mutar orígenes. Las herramientas independientes son solo de QA, nunca requisito de ejecución del usuario.
- **K288:** Bigme B6 Physical Compatibility Gate: abrir el CBR generado en el dispositivo y comprobar primera/última página, navegación, recuento de 111 páginas, orden, reinicio del lector y aceptación de RAR5/PNG; registrar evidencia. Sin hardware conectado, permanece `PENDING` y no se declara compatible.

# FASE 17 — CONNECTORS REALES

**Tareas:** K207–K221

- **K207:** política de selección.
- **K208:** investigación de Connector A.
- **K209:** fixtures A.
- **K210:** búsqueda A.
- **K211:** serie A.
- **K212:** capítulos A.
- **K213:** páginas A.
- **K214:** health A.
- **K215:** investigación B.
- **K216–K220:** implementar B siguiendo exactamente el mismo contrato.
- **K221:** Real Connector Gate con fixtures y pruebas pequeñas reales.

Si una fuente exige bypass de CAPTCHA, DRM, paywall o controles de acceso, se marca `INCOMPATIBLE`.

# FASE 18 — DIAGNÓSTICO Y RECUPERACIÓN

**Tareas:** K222–K237

- **K222:** RecoveryManager.
- **K223:** orphan scanner.
- **K224:** reconciliar DB ↔ filesystem.
- **K225:** Safe Mode.
- **K226:** startup self-test.
- **K227:** backup DB.
- **K228:** restore verificado.
- **K229:** quick verify.
- **K230:** deep verify.
- **K231:** rebuild desde CBZ/ComicInfo.
- **K232:** Support Bundle.
- **K233:** sanitizer.
- **K234:** Diagnostics UI.
- **K235:** log rotation.
- **K236:** recovery fault suite.
- **K237:** Recovery Gate sin pérdida de datos.

# FASE 19 — BENCHMARK + RC0

**Tareas:** K238–K255

- **K238:** dataset 100 capítulos/~2GB.
- **K239:** concurrency 1/2/4/6/8.
- **K240:** CPU.
- **K241:** RAM.
- **K242:** disk.
- **K243:** handles.
- **K244:** reader benchmark.
- **K245:** library benchmark.
- **K246:** leak test.
- **K247:** startup.
- **K248:** shutdown.
- **K249:** crash stress.
- **K250:** network stress.
- **K251:** disk full.
- **K252:** DB locked.
- **K253:** soak test.
- **K254:** ajustar defaults por eficiencia.
- **K255:** RC0 Gate y freeze de features.

---

# PARTE 6 — FASES 20 Y 21

# FASE 20 — PACKAGING + CLEAN MACHINE

**Tareas:** K256–K269

- **K256:** Vite production build.
- **K257:** FastAPI sirve frontend compilado.
- **K258:** PyInstaller.
- **K259:** `Yomira.exe`.
- **K260:** Inno Setup.
- **K261:** separar programa/datos.
- **K262:** upgrade sin borrar biblioteca.
- **K263:** desinstalación con conservación opcional.
- **K264:** firewall UX solo al activar LAN.
- **K265:** single instance empaquetado.
- **K266:** instalación en Windows limpio sin Python/Node.
- **K267:** upgrade test.
- **K268:** rollback test.
- **K269:** Packaging Gate.

# FASE 21 — V1.0 RELEASE GATE

**Tareas:** K270–K284

- **K270:** source freeze.
- **K271:** TypeScript Gate.
- **K272:** ESLint Gate.
- **K273:** Ruff Gate.
- **K274:** Pyright Gate.
- **K275:** Unit Gate.
- **K276:** Integration Gate.
- **K277:** E2E Gate.
- **K278:** Fault Injection Gate.
- **K279:** Benchmark Gate.
- **K280:** Migration/Restore Gate.
- **K281:** Windows/USB/LAN Gate; el subgate de CBR/Bigme requiere K288 PASS.
- **K282:** Known Issues Review.
- **K283:** Release Candidate final.
- **K284:** Yomira 1.0, autorización final posterior a todos los prerrequisitos, incluidas K285–K288.

V1 solo se aprueba si:

```text
TypeScript PASS
ESLint PASS
Ruff PASS
Pyright PASS
Unit PASS
Integration PASS
E2E PASS
Fault Injection PASS
Benchmark PASS
Migration PASS
Restore PASS
Library Rebuild PASS
PC PASS
Mobile PASS
Bigme PASS
Bigme CBR (K288) PASS
USB Export PASS
LAN Auth PASS
Installer PASS
Upgrade PASS
Uninstaller PASS
Clean Machine PASS
```

---

# REGLAS DE EJECUCIÓN POR BLOQUES DE 4 FASES

## Parte 1
Fases 0–3: Constitución, Diseño, Tooling, Backend Core.

## Parte 2
Fases 4–7: Base de datos, Biblioteca, Connector SDK, Mock Server.

## Parte 3
Fases 8–11: Job Manager, Download Engine, Image Validation, SAFE Processing.

## Parte 4
Fases 12–15: CBZ, Search, Reader, Updates.

## Parte 5
Fases 16–19: Export, Connectors reales, Recovery, Benchmark/RC0.

## Parte 6
Fases 20–21: Packaging y Release.

Al finalizar cada parte:
1. ejecutar Quality Gate completo;
2. ejecutar regresión;
3. revisar documentación;
4. producir reporte acumulativo;
5. verificar 0 P0/P1/P2;
6. crear checkpoint Git;
7. avanzar.

# CHECKPOINTS

```text
checkpoint/phase-03
checkpoint/phase-07
checkpoint/phase-11
checkpoint/phase-15
checkpoint/phase-19
v1.0.0
```

# POLÍTICA DE COMMITS

Formato:
```text
type(scope): descripción
```

Ejemplos:
```text
chore(repo): create project structure
feat(system): add liveness endpoint
test(downloads): cover interrupted stream recovery
fix(cbz): reject incomplete archive
docs(adr): document source connector isolation
```

Evitar `update`, `fix stuff`, `changes`, `final`.

# QUALITY GATE GLOBAL

Frontend:
- TypeScript
- ESLint
- Vitest
- build

Backend:
- Ruff
- Pyright
- Pytest

Integration:
- API smoke
- startup/shutdown

Security:
- dependency audit cuando aplique

Resultado:
```text
PASS / FAIL
```

Si es `FAIL`:
```text
NO NEXT TASK
```

# CRITERIO FINAL

Yomira no se considera terminado porque abre o descarga un manga. Solo está listo cuando demuestra integridad, recuperación, trazabilidad, estabilidad, consumo acotado, compatibilidad, instalación limpia y cero bugs críticos conocidos.

# SEGUIMIENTO ALPHA — 2026-10-01

Por autorización del usuario («solucionalo aunque tengas que cambiar el plan principal»), se incorpora el reintento explícito de descargas FAILED definido en ADR-0107. Se reutiliza la creación idempotente de un nuevo DownloadJob y se conserva íntegra la evidencia del trabajo anterior. La enmienda CBR ADR-0121/0122 agrega K285–K288 sin renumerar tareas existentes; K284 conserva su función de aprobación final y queda condicionado a que estos prerrequisitos también pasen. Evidencia y verificación del reintento: `docs/reports/FAILED_DOWNLOAD_RETRY_ALPHA_2026-10-01.md`.

La prueba real expuso una discrepancia entre MIME HTTP y PNG válidos. ADR-0108 aclara K117 y la validación de SPEC §27: ambos tipos deben pertenecer al catálogo permitido; el formato real comprobado por firma y decodificación determina MIME/extensión. Se mantienen límites, hash, identidad y rechazo de contenido inválido, y se registra la anomalía.

## Enmienda CBR para Bigme — 2026-10-04

ADR-0121 añadió a SPEC 1.2 la exportación de varios capítulos a un solo CBR orientado a Bigme B6. ADR-0122 selecciona un escritor propio RAR5 store-only, sin redistribuir ni depender de RARLAB/WinRAR. K285–K287 deben pasar antes de evaluar compatibilidad física; K288 exige prueba en el dispositivo real. K281 y K284 permanecen pendientes de K288 PASS para este requisito. No se cambia ni elimina ningún CBZ por la exportación CBR.

# FIN DEL MASTER EXECUTION PLAN
