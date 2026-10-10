# YOMIRA V1 TECHNICAL SPECIFICATION 1.1 — REVIEW DRAFT

**Documento:** YOMIRA_V1_SPEC_1.1_DRAFT.md
**Proyecto:** Yomira  
**Versión de especificación:** 1.1
**Estado:** IN REVIEW
**Línea base vigente:** La versión 1.0 (APPROVED) permanece vigente hasta la aprobación explícita de esta revisión.
**Alcance de revisión:** §115 alinea el mapa de fases con el plan maestro V1.1; §121 aclara versión y secuencia. No se proponen cambios funcionales.
**Propietaria arquitectónica:** Luna — Principal Software Architect & Lead Developer  
**Plataforma objetivo:** Windows 10/11 x64  
**Modelo de operación:** Local-first, con acceso LAN opcional  
**Release objetivo:** Yomira 1.0  
**Fecha de congelación prevista:** Antes de iniciar Fase 2  
**Regla superior:** Si el código contradice esta especificación, el código está equivocado.

---

# 0. Propósito de este documento

Este documento es la **constitución técnica de Yomira V1**. Su objetivo es evitar decisiones improvisadas durante el desarrollo y convertir los requisitos funcionales, de resiliencia, rendimiento, seguridad y experiencia de usuario en reglas verificables.

Ninguna implementación funcional de React, Python, SQLite, conectores, lector, descargador o empaquetado podrá comenzar hasta que esta especificación sea aprobada.

La filosofía de Yomira es innegociable:

1. **No perder ni corromper contenido.**
2. **Trazabilidad absoluta de fallos.**
3. **Recuperación automática ante errores.**
4. **Consumo de recursos predecible y acotado.**
5. **Velocidad solamente después de las cuatro prioridades anteriores.**

La arquitectura debe estar preparada para sobrevivir a:

- cortes de Internet;
- errores HTTP;
- páginas inválidas;
- imágenes corruptas;
- cambios de estructura en fuentes;
- suspensión o reinicio de Windows;
- cierre inesperado del backend;
- disco lleno;
- fallos de permisos;
- CBZ incompletos;
- corrupción de la base de datos;
- archivos temporales huérfanos;
- desconexión de un e-reader durante exportación;
- biblioteca muy grande;
- navegador lento en e-reader;
- dependencias o migraciones incompatibles.

---

# 1. Visión del producto

Yomira V1 será una aplicación local para Windows orientada a organizar, descargar, verificar, procesar, empaquetar y leer manga/manhwa en formato CBZ.

El usuario final no necesitará instalar Python, Node, npm, pip ni herramientas de desarrollo.

El flujo principal será:

```text
Buscar
  ↓
Elegir fuente compatible
  ↓
Elegir obra
  ↓
Consultar capítulos
  ↓
Seleccionar uno, varios o todos
  ↓
Crear Download Jobs
  ↓
Descargar a temporales
  ↓
Verificar contenido
  ↓
Procesar imágenes si es seguro
  ↓
Generar ComicInfo.xml
  ↓
Construir CBZ temporal
  ↓
Verificar CBZ completo
  ↓
Publicación atómica
  ↓
Indexar en biblioteca
  ↓
Leer localmente o exportar al e-reader
```

Yomira será **local-first**. La biblioteca y la base de datos vivirán en el PC. El acceso desde celular o e-reader se realizará únicamente a través de la red local cuando el usuario lo habilite explícitamente.

---

# 2. Alcance de Yomira V1

## 2.1 Incluido en V1

Yomira V1 debe incluir:

- aplicación local para Windows;
- frontend React + TypeScript;
- backend FastAPI + Python;
- SQLite en modo WAL;
- biblioteca de series;
- búsqueda mediante conectores compatibles;
- consulta de metadatos;
- consulta de capítulos;
- selección individual o masiva;
- motor de descargas resiliente;
- cola de trabajos;
- reanudación de trabajos interrumpidos;
- validación de imágenes;
- detección de anomalías;
- recorte de bordes en modo SAFE;
- clasificación de páginas estándar, dobles y tiras largas;
- generación de CBZ;
- generación de `ComicInfo.xml`;
- publicación atómica de CBZ;
- verificación posterior a empaquetado;
- lector integrado;
- progreso de lectura;
- modo e-reader;
- revisión manual de nuevos capítulos;
- exportación segura a USB/e-reader;
- diagnósticos;
- logs estructurados;
- Support Bundle;
- backup de base de datos/configuración;
- recuperación de biblioteca;
- benchmark;
- instalador Windows;
- pruebas de actualización y rollback.

## 2.2 Fuera de V1

Yomira V1 no incluirá:

- servidor público;
- nube;
- cuentas externas;
- sincronización por Internet;
- aplicación móvil nativa;
- aplicación e-reader nativa;
- descarga automática permanente;
- scraping que eluda DRM;
- bypass de CAPTCHA;
- bypass de paywalls;
- evasión de controles de acceso;
- edición manual avanzada de páginas;
- OCR;
- edición gráfica;
- sincronización multiusuario;
- servidor remoto multiusuario.

---

# 3. Principios arquitectónicos

## 3.1 Integridad por encima de velocidad

**REQ-CORE-001:** Ningún archivo se considerará válido únicamente porque su descarga HTTP terminó.

**REQ-CORE-002:** Ningún capítulo podrá entrar en la biblioteca si no ha pasado todas las fases de validación correspondientes.

**REQ-CORE-003:** Ninguna optimización de rendimiento podrá eliminar validaciones de integridad.

## 3.2 Atomicidad

Las operaciones críticas deben ser atómicas.

Ejemplo:

```text
chapter.cbz.tmp
  ↓
verificación completa
  ↓
atomic rename
  ↓
chapter.cbz
```

Nunca se debe exponer un CBZ parcial con extensión definitiva.

## 3.3 Idempotencia

Repetir una operación no debe crear duplicados arbitrarios.

Pulsar dos veces “Descargar capítulo 50” no debe producir:

```text
50.cbz
50 (1).cbz
50 (2).cbz
```

Debe detectar que el job ya existe o que el capítulo ya está descargado.

## 3.4 Recuperabilidad

Todo trabajo persistente debe poder reconstruir su estado después de:

- cierre de navegador;
- cierre del backend;
- suspensión;
- reinicio;
- fallo de red;
- excepción durante procesamiento.

## 3.5 Observabilidad

Todo error relevante debe incluir, cuando aplique:

```text
timestamp
component
trace_id
job_id
series_id
chapter_id
source_id
stage
error_code
retryable
attempt
duration
```

## 3.6 Recursos acotados

Queda prohibido diseñar:

- colas infinitas;
- cachés infinitas;
- logs infinitos;
- workers ilimitados;
- precarga ilimitada;
- decodificación masiva de imágenes;
- renderizado de miles de tarjetas simultáneamente.

---

# 4. Stack tecnológico oficial

## 4.1 Frontend

- React
- TypeScript `strict`
- Vite
- React Router
- TanStack Query
- Zustand
- CSS Modules
- Variables CSS
- Vitest
- React Testing Library
- Playwright

## 4.2 Backend

- Python
- FastAPI
- Pydantic
- httpx `AsyncClient`
- Pillow
- SQLAlchemy 2
- Alembic
- Pytest
- Ruff
- Pyright

## 4.3 Datos

- SQLite
- `journal_mode=WAL`
- `foreign_keys=ON`
- `busy_timeout` configurado
- migraciones Alembic obligatorias

## 4.4 Packaging

- PyInstaller
- Inno Setup
- frontend compilado por Vite
- runtime empaquetado
- sin dependencia de Python/Node instalados por el usuario

---

# 5. Estructura obligatoria del repositorio

```text
yomira/
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── components/
│   │   ├── features/
│   │   ├── api/
│   │   ├── hooks/
│   │   ├── styles/
│   │   ├── types/
│   │   └── utils/
│   └── tests/
│
├── backend/
│   ├── yomira/
│   │   ├── api/
│   │   ├── database/
│   │   ├── models/
│   │   ├── services/
│   │   ├── sources/
│   │   ├── jobs/
│   │   ├── downloads/
│   │   ├── images/
│   │   ├── cbz/
│   │   ├── metadata/
│   │   ├── reader/
│   │   ├── export/
│   │   ├── diagnostics/
│   │   ├── security/
│   │   ├── recovery/
│   │   └── settings/
│   └── tests/
│
├── fixtures/
│   ├── images/
│   ├── chapters/
│   ├── source-pages/
│   ├── corrupted/
│   └── golden/
│
├── benchmark/
├── docs/
│   ├── adr/
│   ├── architecture/
│   ├── testing/
│   └── releases/
├── scripts/
├── installer/
└── tests/
```

Quedan prohibidos archivos monolíticos sin responsabilidad clara.

---

# 6. ADR — Architecture Decision Records

Toda decisión arquitectónica que cambie comportamiento fundamental requerirá ADR.

Formato mínimo:

```text
ADR-XXXX-title.md

Status
Context
Decision
Alternatives Considered
Positive Consequences
Negative Consequences
Failure Modes
Reversal Strategy
```

ADRs iniciales obligatorios:

- ADR-0001 React + TypeScript
- ADR-0002 FastAPI + Python
- ADR-0003 SQLite WAL
- ADR-0004 CBZ por capítulo
- ADR-0005 Publicación atómica
- ADR-0006 Source Connector Isolation
- ADR-0007 Persistent Job State Machine
- ADR-0008 Relative Library Paths
- ADR-0009 SAFE Image Processing
- ADR-0010 Local-first / Opt-in LAN
- ADR-0011 Recovery Manager
- ADR-0012 Reader Asset Streaming
- ADR-0013 Metadata Authority
- ADR-0014 Chapter Versioning
- ADR-0015 Disk Budgeting
- ADR-0016 Safe Mode
- ADR-0017 Single Instance
- ADR-0018 Typed Settings
- ADR-0019 Dependency Policy
- ADR-0020 Release/Update Strategy

---

# 7. Modelo de dominio

## 7.1 Series

Representa una obra local.

Campos conceptuales:

- `series_id`
- `canonical_title`
- `display_title`
- `description`
- `status`
- `language`
- `cover_asset_id`
- `created_at`
- `updated_at`

## 7.2 Source

Representa una fuente compatible.

- `source_id`
- `connector_id`
- `connector_version`
- `display_name`
- `enabled`
- `health_status`

## 7.3 SeriesSource

Relaciona una serie local con una obra remota.

- `series_source_id`
- `series_id`
- `source_id`
- `remote_id`
- `remote_url`
- `remote_title`
- `last_checked_at`

No se permite fusión automática únicamente por título.

## 7.4 Chapter

Representa el capítulo lógico.

No utilizar `float` como identidad.

Campos:

- `chapter_id`
- `series_id`
- `chapter_raw_label`
- `chapter_major`
- `chapter_minor`
- `chapter_variant`
- `chapter_type`
- `chapter_sort_key`
- `volume_number`
- `language`
- `display_label`

Ejemplo:

```text
raw_label: "12.10"
major: 12
minor: 10
variant: null
sort_key: [12, 10, 0]
```

Extras:

```text
raw_label: "Extra verano"
major: null
minor: null
chapter_type: EXTRA
```

## 7.5 ChapterSource

Representa una versión concreta del capítulo en una fuente.

- `chapter_source_id`
- `chapter_id`
- `source_id`
- `remote_id`
- `remote_url`
- `remote_label`
- `available`
- `last_seen_at`

## 7.6 Asset

Representa archivos físicos.

Tipos:

- COVER
- THUMBNAIL
- CBZ
- PAGE_CACHE
- TEMP
- EXPORT_COPY

Campos:

- `asset_id`
- `asset_type`
- `relative_path`
- `sha256`
- `size_bytes`
- `created_at`
- `verified_at`

## 7.7 DownloadJob

Representa una ejecución de descarga.

## 7.8 ReaderProgress

- `series_id`
- `chapter_id`
- `page_index`
- `percentage`
- `updated_at`

## 7.9 ExportJob

Representa una exportación a dispositivo.

---

# 8. Autoridad de metadatos

**REQ-META-001:** La fuente remota nunca puede sobrescribir silenciosamente datos seleccionados explícitamente por el usuario.

Orden de autoridad:

```text
USER_OVERRIDE
   >
LOCAL_CANONICAL
   >
SOURCE_METADATA
```

Cada campo debe poder distinguir:

- valor remoto;
- valor local;
- override del usuario.

Campos sensibles:

- título;
- portada;
- autor;
- descripción;
- géneros;
- estado;
- idioma.

---

# 9. Versiones de capítulos

Un mismo capítulo puede existir en múltiples fuentes.

Estados de versión:

- ACTIVE
- ALTERNATE
- SUPERSEDED
- INVALID

Yomira nunca debe reemplazar una versión descargada sin:

- conservar trazabilidad;
- comprobar nueva versión;
- validar completamente;
- publicar de forma atómica;
- registrar versión anterior como reemplazada o alternativa.

---

# 10. Base de datos

Tablas mínimas:

```text
series
sources
series_sources
chapters
chapter_sources
assets
download_jobs
download_pages
reader_progress
exports
connector_health
settings
schema_history
metadata_overrides
chapter_versions
```

Cada tabla debe tener:

- PK estable;
- FK explícita;
- constraints;
- índices;
- reglas de cascada;
- timestamps UTC;
- campos `created_at` y/o `updated_at` donde corresponda.

---

# 11. SQLite WAL

Configuración obligatoria:

```text
journal_mode=WAL
foreign_keys=ON
busy_timeout=<configurado>
```

Las escrituras deben centralizarse y mantenerse cortas.

La arquitectura no debe depender de múltiples escritores simultáneos.

---

# 12. Migraciones

Toda modificación de esquema requiere:

1. preflight;
2. backup;
3. migración Alembic;
4. `integrity_check`;
5. validación de schema;
6. commit.

Si falla:

- detener arranque normal;
- restaurar backup si corresponde;
- iniciar Safe Mode;
- registrar `DB_MIGRATION_FAILED`.

---

# 13. Backups

## 13.1 Backup estándar

Incluye:

- SQLite;
- settings;
- metadata local;
- overrides;
- configuración de biblioteca;
- opcionalmente thumbnails.

No incluye todos los CBZ por defecto.

## 13.2 Retención

Configurable.

Valor inicial sugerido:

- 5 backups automáticos;
- 3 backups pre-migration;
- manuales nunca eliminados automáticamente.

## 13.3 Restauración

Debe verificar backup antes de reemplazar datos activos.

---

# 14. Sistema de archivos

```text
YomiraData/
├── database/
├── library/
├── cache/
├── temp/
├── logs/
├── backups/
├── exports/
└── diagnostics/
```

SQLite almacenará rutas relativas cuando sea posible.

---

# 15. Biblioteca portátil

Raíz configurable:

```text
library_root
```

Ejemplo DB:

```text
Naruto/Naruto - 001.cbz
```

No:

```text
C:\Users\Hydr4\...
```

disperso por la base.

---

# 16. Sanitización de nombres

Debe soportar:

- Unicode;
- español;
- japonés;
- coreano;
- chino;
- emojis cuando el filesystem lo permita.

Debe impedir:

- `../`
- `..\`
- nombres reservados Windows;
- caracteres inválidos;
- rutas absolutas externas;
- nombres excesivamente largos.

Nombres Windows reservados:

```text
CON
PRN
AUX
NUL
COM1...
LPT1...
```

---

# 17. Job Manager

Estados válidos:

```text
QUEUED
FETCHING_METADATA
DOWNLOADING
VALIDATING
PROCESSING
PACKAGING
VERIFYING
INDEXING
COMPLETED
FAILED
CANCELLED
INTERRUPTED
```

Ruta normal:

```text
QUEUED
 ↓
FETCHING_METADATA
 ↓
DOWNLOADING
 ↓
VALIDATING
 ↓
PROCESSING
 ↓
PACKAGING
 ↓
VERIFYING
 ↓
INDEXING
 ↓
COMPLETED
```

---

# 18. Matriz de estados

Transiciones ilegales deben producir excepción.

Ejemplos:

| Desde | Hacia | Permitido |
|---|---|---|
| QUEUED | FETCHING_METADATA | Sí |
| FETCHING_METADATA | DOWNLOADING | Sí |
| DOWNLOADING | VALIDATING | Sí |
| DOWNLOADING | COMPLETED | No |
| PROCESSING | PACKAGING | Sí |
| PACKAGING | VERIFYING | Sí |
| VERIFYING | INDEXING | Sí |
| INDEXING | COMPLETED | Sí |

Estados terminales:

- COMPLETED
- FAILED
- CANCELLED

INTERRUPTED debe ser recuperable.

---

# 19. Cancelación semántica

La cancelación depende de la etapa.

- QUEUED: inmediata.
- FETCHING_METADATA: inmediata.
- DOWNLOADING: detener nuevas solicitudes y cerrar stream de forma limpia.
- VALIDATING: puede diferirse hasta punto seguro.
- PROCESSING: puede diferirse entre páginas.
- PACKAGING: no interrumpir operación de escritura crítica sin cleanup.
- VERIFYING: permitir cancelación diferida.
- INDEXING: no cancelar una transacción DB a mitad.
- COMPLETED: no aplica.

---

# 20. Prioridades y fairness

Jobs:

- HIGH
- NORMAL
- LOW

Un job HIGH no debe causar starvation permanente.

Debe existir fairness:

- límite global;
- límite por source;
- límite por host.

Una fuente con 500 jobs no debe bloquear una fuente con 2.

---

# 21. Manifest de capítulo

Cada job tiene manifest versionado.

Ejemplo conceptual:

```json
{
  "schema_version": 1,
  "job_id": "...",
  "source_id": "...",
  "chapter_id": "...",
  "expected_pages": 20,
  "pages": []
}
```

Cada página:

```text
ordinal
remote_reference
temporary_path
status
size
sha256
width
height
mime
attempts
```

---

# 22. Motor de descargas

## 22.1 Streaming

Nunca usar `response.content` para imágenes grandes.

Flujo:

```text
HTTP stream
  ↓
chunk
  ↓
hash update
  ↓
write .part
```

## 22.2 Temporales

Durante descarga:

```text
001.jpg.part
```

Solo después de validar contenido básico:

```text
001.jpg
```

## 22.3 Hash

SHA-256 calculado durante la descarga.

## 22.4 Timeouts

Separados:

- connect;
- read;
- write;
- pool.

## 22.5 Retries

Política inicial:

```text
1s
3s
8s
```

Errores reintentables:

- timeout;
- reset;
- 500;
- 502;
- 503;
- 504;
- 429 con espera apropiada.

No reintentar indiscriminadamente:

- 401;
- 403 persistente;
- 404 estable;
- input inválido.

---

# 23. Circuit Breaker

Estados:

```text
CLOSED
OPEN
HALF_OPEN
```

Ventana inicial sugerida:

- últimas 20 operaciones;
- abrir si 14 o más fallan;
- cooldown;
- prueba limitada en HALF_OPEN.

Los valores finales se ajustarán mediante tests.

---

# 24. Conectores de fuentes

Contrato conceptual:

```text
health_check()
search()
get_series()
get_chapters()
get_chapter()
get_pages()
normalize_metadata()
```

Capacidades declaradas:

- SEARCH
- METADATA
- CHAPTER_LIST
- PAGE_LIST
- UPDATE_CHECK

Los conectores no pueden:

- escribir directamente en `library/`;
- modificar DB mediante SQL arbitrario;
- generar CBZ;
- decidir estados de Job Manager;
- eludir DRM;
- saltarse CAPTCHA;
- evadir paywall;
- burlar autenticación.

---

# 25. Fixtures de conectores

Cada connector debe tener:

```text
search fixture
series fixture
chapter list fixture
chapter fixture
failure fixture
```

Los tests deben funcionar sin Internet.

---

# 26. Health Check de conectores

Cada conector tendrá:

- version;
- last_success;
- last_failure;
- current_status;
- circuit state.

Estados:

- HEALTHY
- DEGRADED
- BROKEN
- DISABLED
- INCOMPATIBLE

---

# 27. Validación de contenido descargado

No confiar únicamente en HTTP 200.

Debe comprobar:

1. status HTTP;
2. content-length si existe;
3. MIME;
4. magic bytes;
5. formato permitido;
6. decodificación;
7. dimensiones;
8. límites;
9. hash.

---

# 28. Formatos de imagen V1

Inicialmente:

- JPEG
- PNG
- WEBP

AVIF se evaluará después.

No admitir formatos innecesarios en V1.

---

# 29. Protección contra image bombs

Antes de decodificar por completo:

- tamaño de archivo;
- dimensiones;
- píxeles totales;
- tipo;
- límites de Pillow.

Debe existir:

```text
MAX_IMAGE_BYTES
MAX_IMAGE_PIXELS
MAX_IMAGE_WIDTH
MAX_IMAGE_HEIGHT
```

---

# 30. Clasificación de páginas

Tipos:

- STANDARD_PAGE
- DOUBLE_PAGE
- LONG_STRIP
- UNKNOWN

La clasificación puede influir en lector y procesamiento.

---

# 31. Procesamiento SAFE

Modos:

- OFF
- SAFE
- E_READER

Default: SAFE.

Regla fundamental:

> Si existe duda, no recortar.

SAFE debe analizar:

- uniformidad;
- luminosidad;
- variación;
- continuidad;
- contenido cercano al borde;
- porcentaje total de crop.

Debe existir margen de seguridad.

---

# 32. Golden dataset de recorte

Se debe mantener:

```text
fixtures/golden/
```

con:

- páginas blancas;
- grises;
- dobles;
- dibujos hasta el borde;
- bocadillos cerca del margen;
- webtoon;
- páginas con borde negro;
- páginas color.

Cada imagen tendrá resultado esperado:

- NO_CROP;
- CROP esperado;
- tolerancia permitida.

---

# 33. No recomprimir innecesariamente

Si una página no requiere transformación:

```text
archivo original
  ↓
CBZ
```

No pasar por re-encode.

---

# 34. Thumbnail Pipeline

Las portadas originales no deben usarse directamente en cada grid.

Servicio dedicado:

```text
ThumbnailService
```

Tamaños sugeridos:

- small;
- medium;
- large.

Cache por hash.

Si cambia la portada, cambia el hash y se invalida thumbnail.

---

# 35. Generación de CBZ

Estructura:

```text
ComicInfo.xml
001.jpg
002.jpg
003.jpg
...
```

Un CBZ por capítulo.

---

# 36. ComicInfo.xml

Campos iniciales:

- Title
- Series
- Number
- Volume
- Summary
- Writer
- Genre
- PageCount
- LanguageISO
- Manga
- Web

No inventar metadatos desconocidos.

---

# 37. CBZ determinista

Cuando sea razonable:

- orden estable;
- nombres estables;
- encoding estable;
- estructura consistente;
- timestamps controlados o documentados.

---

# 38. Publicación atómica del CBZ

```text
chapter.cbz.tmp
 ↓
ZIP open
 ↓
ComicInfo parse
 ↓
page count
 ↓
decode pages
 ↓
hash reconciliation
 ↓
close
 ↓
atomic rename
 ↓
chapter.cbz
```

Solo después se indexa.

---

# 39. File locking

Un CBZ publicado es inmutable mientras se lee.

Si debe reemplazarse:

```text
new.cbz.tmp
 ↓
verify
 ↓
atomic swap
```

No modificar archivo abierto.

Dos jobs no pueden escribir el mismo target simultáneamente.

---

# 40. ReaderAssetService

React no leerá directamente filesystem.

API conceptual:

```text
GET /reader/{chapter_id}/pages/{page}
```

El backend:

1. resuelve asset;
2. abre CBZ;
3. extrae página solicitada;
4. usa caché limitada;
5. devuelve respuesta HTTP.

Debe considerar:

- ETag;
- Cache-Control;
- Range si es útil;
- límites de memoria.

---

# 41. Lector

Modos:

- Página única
- Vertical
- Doble página
- RTL

Funciones:

- zoom;
- fit width;
- fit height;
- fullscreen;
- progreso;
- teclado;
- touch.

---

# 42. Virtualización del lector

No cargar todo.

Ejemplo:

```text
38 liberada
39 cache
40 visible
41 preload
42 preload
```

Ventana configurable.

---

# 43. Caché del lector

Límite inicial sugerido:

```text
500 MB
```

Debe ser configurable.

Eviction:

- LRU.

Nunca crecer sin límite.

---

# 44. UI/UX

Inspiración estructural: Webtoon.

No copiar:

- logo;
- assets;
- identidad;
- branding;
- tipografía propietaria.

Mantener:

- portadas protagonistas;
- navegación limpia;
- secciones horizontales;
- chips;
- densidad visual;
- jerarquía clara.

Paleta:

```text
background-primary  #0B0D10
background-secondary #101318
surface-card        #15191F
surface-elevated    #1A1F26
text-primary        #F4F6F8
text-secondary      #98A1AD
accent              #00D66B
```

---

# 45. Responsive

Objetivo:

- Desktop: 5–7 portadas.
- Tablet: 4.
- Mobile: 2–3.
- Bigme: 2–3.

No depender de nombres de dispositivos.

---

# 46. Modo e-reader

Debe desactivar o minimizar:

- animaciones;
- blur;
- transparencias;
- transiciones;
- sombras;
- efectos hover.

Debe aumentar:

- contraste;
- touch targets;
- claridad;
- estabilidad visual.

---

# 47. Accesibilidad

Obligatorio:

- focus visible;
- navegación teclado;
- `aria-label`;
- tamaño táctil adecuado;
- contraste suficiente;
- no depender exclusivamente de color;
- compatibilidad con zoom.

---

# 48. Frontend Error Boundaries

Errores en una sección no deben tumbar toda la app.

Boundary mínimo:

- app shell;
- library;
- reader;
- downloads.

---

# 49. Optimistic UI

Permitido para cambios menores no críticos.

No permitido para:

- descarga completada;
- borrado;
- exportación;
- publicación CBZ;
- migraciones.

La UI puede mostrar PENDING, nunca fingir COMPLETED.

---

# 50. API FastAPI

Namespace:

```text
/api/v1
```

Endpoints previstos:

```text
GET    /api/v1/system/liveness
GET    /api/v1/system/readiness
GET    /api/v1/system/version
GET    /api/v1/system/diagnostics

GET    /api/v1/library
GET    /api/v1/series/{series_id}

GET    /api/v1/sources
GET    /api/v1/sources/{source_id}/health

GET    /api/v1/search

POST   /api/v1/downloads
GET    /api/v1/downloads
GET    /api/v1/downloads/{job_id}
POST   /api/v1/downloads/{job_id}/cancel
POST   /api/v1/downloads/{job_id}/retry

POST   /api/v1/library/check-updates

GET    /api/v1/reader/{chapter_id}
GET    /api/v1/reader/{chapter_id}/pages/{page}

POST   /api/v1/diagnostics/bundle
```

---

# 51. Contrato de respuestas

Éxito:

```json
{
  "data": {},
  "meta": {}
}
```

Error:

```json
{
  "error": {
    "code": "DL_HTTP_429",
    "message": "Rate limit reached",
    "trace_id": "...",
    "retryable": true
  }
}
```

La lógica nunca debe depender de comparar mensajes humanos.

---

# 52. Pydantic

Todo input debe validarse.

Incluido input procedente de React.

Schemas:

- SearchRequest
- SearchResultResponse
- SeriesResponse
- ChapterResponse
- DownloadCreateRequest
- DownloadJobResponse
- ReaderProgressRequest
- SettingsUpdateRequest
- ExportRequest
- ErrorResponse

Cada schema debe definir:

- tipos;
- rangos;
- enums;
- longitudes;
- normalización;
- opcionalidad.

---

# 53. TypeScript

`strict = true`.

Evitar `any`.

Los contratos frontend deben derivarse o mantenerse sincronizados con API.

---

# 54. Configuración tipada

Settings centrales.

Ejemplos:

```text
library_root
download_concurrency
per_host_concurrency
connect_timeout
read_timeout
retry_count
cache_limit_mb
crop_mode
lan_enabled
lan_port
reader_mode
reader_rtl
thumbnail_quality
log_level
```

Cada setting:

- tipo;
- default;
- min/max;
- requiere reinicio o no;
- user editable o internal.

---

# 55. Schema versioning

Debe existir versión en:

- DB;
- manifest;
- settings;
- support bundle;
- internal metadata formats.

Ejemplo:

```text
schema_version: 1
```

---

# 56. API versioning

Frontend debe comprobar:

```text
/api/v1/system/version
```

Respuesta conceptual:

```text
backend_version
api_version
schema_version
min_frontend_version
max_frontend_version
```

Si no son compatibles, mostrar error claro.

---

# 57. Recovery Manager

Al arrancar:

1. comprobar DB;
2. comprobar migraciones;
3. comprobar temporales;
4. localizar jobs no terminales;
5. inspeccionar manifests;
6. identificar `.part`;
7. identificar `.tmp`;
8. reconciliar filesystem ↔ DB;
9. decidir recuperación.

Estados posibles:

- resumable;
- retryable;
- orphaned;
- corrupted;
- cleanup candidate.

---

# 58. Crash cleanup

No borrar temporales ciegamente.

Primero determinar si pertenecen a:

- job recuperable;
- job inexistente;
- job terminado;
- archivo corrupto;
- archive pendiente de verify.

---

# 59. Safe Mode

Si falla:

- migración;
- integridad DB;
- permisos críticos;
- rutas;
- schema;

Yomira debe abrir modo limitado.

Safe Mode permite:

- diagnósticos;
- restaurar backup;
- cambiar library root;
- reconstruir biblioteca;
- exportar Support Bundle.

No permite nuevas descargas.

---

# 60. Self-test de arranque

Debe verificar:

- DB accesible;
- schema compatible;
- filesystem accesible;
- temp writable;
- temp deletable;
- library readable;
- disk space;
- configuración válida.

---

# 61. DiskBudgetManager

Antes de un lote:

- espacio libre;
- estimación descarga;
- overhead temporal;
- overhead CBZ;
- reserva mínima.

No iniciar si el presupuesto es inseguro.

Debe haber reservas para:

- DB/WAL;
- logs;
- temporales;
- CBZ;
- thumbnails.

---

# 62. Gestión de almacenamiento

UI debe poder mostrar:

- biblioteca;
- cache;
- temp;
- logs;
- backups;
- espacio libre.

Limpieza segura:

- cache: automática;
- logs: rotación;
- temp huérfano: recovery-aware;
- backups: según política.

---

# 63. Verificación periódica de biblioteca

Botón:

```text
Verificar biblioteca
```

Modos:

- QUICK
- DEEP

QUICK:

- existencia;
- tamaño;
- registro DB.

DEEP:

- ZIP;
- ComicInfo;
- hashes;
- imágenes;
- correspondencia DB.

---

# 64. Reconstrucción de biblioteca

Si DB se pierde:

```text
scan CBZ
 ↓
ComicInfo
 ↓
filename fallback
 ↓
rebuild series
 ↓
rebuild chapters
```

Debe ser una función soportada.

---

# 65. Actualizaciones de capítulos

Revisión manual.

Nunca descargar automáticamente.

Si una fuente elimina un capítulo remoto:

- no borrar local;
- marcar remote unavailable;
- conservar biblioteca.

---

# 66. Detección de cambio remoto

Yomira puede detectar cambios de:

- label;
- page count;
- metadata;

pero no redescargará todo automáticamente solo para comparar hash.

Debe ofrecer acción si detecta cambio.

---

# 67. Exportación al e-reader

Pipeline:

```text
detect destination
 ↓
space check
 ↓
copy as .part
 ↓
hash verify
 ↓
rename .cbz
 ↓
record export
```

Si se desconecta:

- destino parcial no se considera válido;
- job queda FAILED/INTERRUPTED según caso.

---

# 68. Acceso LAN

Default:

```text
127.0.0.1
```

LAN se activa explícitamente.

Al activar:

- elegir puerto;
- comprobar firewall;
- autenticación local;
- session.

---

# 69. Autenticación LAN

V1 debe usar mecanismo sencillo.

Propuesta:

- PIN;
- sesión temporal;
- rate limit.

Nunca exponer sin autenticación una instancia que permite acciones sensibles.

---

# 70. Puerto

Si el puerto configurado está ocupado:

- detectar;
- intentar alternativa segura o pedir acción;
- no fallar silenciosamente.

---

# 71. Firewall Windows

No pedir regla LAN en instalación si LAN está desactivado.

Solicitar únicamente al habilitar acceso LAN.

---

# 72. Single Instance

Solo una instancia backend puede gestionar la DB.

Segunda ejecución:

- detecta instancia existente;
- abre UI existente;
- o se cierra con mensaje.

---

# 73. Suspensión y energía

Yomira debe detectar reanudación.

Jobs activos:

- conservar estado;
- validar conexiones;
- reanudar si corresponde.

Opción futura/configurable:

```text
Prevent sleep while active jobs exist
```

Nunca activada sin consentimiento.

---

# 74. Threat Model

## 74.1 Activos

- biblioteca;
- DB;
- configuración;
- historial de lectura;
- credenciales futuras;
- integridad del PC;
- archivos exportados.

## 74.2 Entradas no confiables

- HTML/JSON remoto;
- imágenes;
- nombres;
- metadata;
- URLs;
- dispositivos USB;
- requests LAN;
- archivos importados futuros.

## 74.3 Límites de confianza

```text
Internet → Connector
Connector → Core
Browser → API
API → Filesystem
USB → Export
CBZ → Reader
```

## 74.4 Amenazas

- path traversal;
- image bomb;
- zip bomb;
- malformed archive;
- HTML-as-image;
- filename injection;
- oversized request;
- invalid IDs;
- arbitrary file read;
- CSRF/LAN abuse;
- malicious metadata.

---

# 75. Secretos

Credenciales futuras no se almacenarán en texto plano.

Preferencias:

- Windows Credential Manager;
- DPAPI.

Nunca en:

- logs;
- support bundle;
- settings plain text.

---

# 76. Support Bundle

Incluye:

- app version;
- OS;
- hardware summary;
- dependency versions;
- connector versions;
- settings sanitizados;
- logs sanitizados;
- DB schema;
- recent errors;
- benchmark summary.

No incluye:

- páginas de manga;
- CBZ;
- cookies;
- tokens;
- contraseñas.

---

# 77. Sanitización de Support Bundle

Debe eliminar o reducir:

- rutas con username;
- tokens;
- cookies;
- query params sensibles;
- IP innecesarias;
- secrets;
- contenido remoto privado.

---

# 78. Logs

Formato estructurado.

Ejemplo:

```text
timestamp
level
component
trace_id
job_id
series_id
chapter_id
error_code
attempt
duration
message
```

---

# 79. Rotación de logs

Límite sugerido:

- 10 MB por archivo;
- 5 archivos.

Configurable.

---

# 80. Catálogo de errores

Familias:

```text
SYS_
DB_
SRC_
DL_
IMG_
CBZ_
LIB_
READ_
EXP_
AUTH_
NET_
```

Ejemplos:

```text
DL_HTTP_429
DL_STREAM_INTERRUPTED
DL_HASH_MISMATCH
IMG_DECODE_FAILED
IMG_PIXEL_LIMIT
CBZ_VERIFY_FAILED
DB_MIGRATION_FAILED
DB_INTEGRITY_FAILED
SRC_SCHEMA_CHANGED
EXP_DISK_FULL
```

---

# 81. Severidad de bugs

- P0: pérdida o destrucción de datos.
- P1: corrupción/integridad.
- P2: funcionalidad mayor rota.
- P3: problema menor.
- P4: visual/cosmético.

Release V1:

- 0 P0 conocidos;
- 0 P1 conocidos;
- 0 P2 conocidos sin aceptación explícita.

---

# 82. Regla de resolución de bugs

Obligatoria:

```text
REPRODUCIR
 ↓
ESCRIBIR TEST QUE FALLE
 ↓
CAUSA RAÍZ
 ↓
FIX
 ↓
TEST PASA
 ↓
SUITE COMPLETA
 ↓
REGRESSION TEST PERMANENTE
 ↓
COMMIT
```

Queda prohibido:

> “cambié cosas hasta que funcionó”.

---

# 83. Mock Manga Server

Debe simular:

- normal;
- slow;
- timeout;
- disconnect;
- 404;
- 429;
- 500;
- malformed HTML;
- fake image;
- corrupt JPEG;
- duplicate image;
- huge image;
- image bomb;
- wrong page count;
- malformed metadata.

---

# 84. Fault Injection

Casos:

- matar backend a mitad de descarga;
- matar backend durante ZIP;
- cortar red;
- bloquear DB;
- llenar disco;
- negar permisos;
- desconectar USB;
- cambiar hora del sistema;
- suspender/reanudar Windows.

---

# 85. Testing

Capas:

```text
UNIT
INTEGRATION
E2E
FAULT INJECTION
BENCHMARK
```

---

# 86. Unit tests

Backend:

- chapter parser;
- sort key;
- filenames;
- manifests;
- retries;
- circuit breaker;
- state machine;
- hash;
- ComicInfo;
- crop;
- page classification;
- settings;
- migrations.

Frontend:

- selección;
- filtros;
- estados;
- error UI;
- downloads;
- reader controls;
- responsive logic.

---

# 87. Integration tests

Pipeline:

```text
Mock Source
 ↓
Download
 ↓
Validate
 ↓
Process
 ↓
CBZ
 ↓
DB
```

Sin Internet.

---

# 88. E2E

Playwright:

```text
open
 ↓
search
 ↓
select series
 ↓
select chapters
 ↓
download
 ↓
wait
 ↓
library
 ↓
open
 ↓
read
```

---

# 89. Property-based / fuzz testing

Aplicar especialmente a:

- nombres;
- rutas;
- capítulos;
- XML;
- manifests;
- ZIP;
- parser inputs;
- invalid JSON.

Objetivo: descubrir casos no previstos.

---

# 90. Benchmark

Dataset estándar:

```text
100 capítulos
20 páginas promedio
≈ 2 GB sintéticos
```

Debe ser reproducible.

Medir:

- throughput;
- CPU;
- RAM;
- peak RAM;
- disk writes;
- open handles;
- latency;
- temp cleanup;
- failure rate.

---

# 91. Benchmark de concurrencia

Probar:

- 1;
- 2;
- 4;
- 6;
- 8 workers.

No gana el más rápido.

Gana el punto de eficiencia.

---

# 92. Presupuestos de rendimiento

Valores iniciales, sujetos a validación:

- Backend idle: <150 MB RAM.
- CPU idle: ~0–1%.
- Backend en descarga normal: <300 MB ideal.
- Descarga normal: <10% CPU media, cuando sea posible.
- Arranque: objetivo <3 s en hardware de referencia.
- Biblioteca 1000 series: interacción perceptiblemente inmediata.
- Búsqueda local: objetivo <200 ms.
- Reader: RAM estable con virtualización.
- No memory leak después de 2000 páginas.

Estos valores son criterios de ingeniería, no promesas absolutas.

---

# 93. Perfil de hardware

## Minimum

PC Windows x64 modesto.

## Reference

Equipo real principal del usuario.

## Stress

Perfil limitado artificialmente.

Benchmark no debe validarse únicamente en hardware potente.

---

# 94. Leak tests

Secuencia:

```text
100 páginas
500
1000
2000
```

RAM debe estabilizarse.

Fallará si crece de forma sostenida sin liberar.

---

# 95. Benchmark de biblioteca

Generar:

- 100 series;
- 1000;
- 10000.

Con muchos capítulos.

Medir:

- render;
- filtros;
- búsqueda;
- scroll;
- navegación.

---

# 96. Virtualización de listas

React no debe renderizar miles de tarjetas simultáneamente.

Aplicar virtualización cuando el benchmark lo justifique.

---

# 97. Dependency Policy

Añadir dependencia requiere justificar:

- necesidad;
- mantenimiento;
- licencia;
- peso;
- seguridad;
- alternativa estándar.

Lockfiles obligatorios.

---

# 98. Supply Chain

Cada release debe:

- fijar versiones;
- auditar dependencias;
- registrar versiones;
- generar SBOM.

---

# 99. SBOM

Debe incluir:

- Python packages;
- Node packages;
- runtime;
- build tools relevantes.

---

# 100. Signing

Antes de distribución pública se evaluará:

- firma de ejecutable;
- firma de instalador;
- impacto SmartScreen.

No bloquea desarrollo inicial, sí debe quedar previsto.

---

# 101. Actualizaciones

V1 puede usar actualización manual.

Flujo:

```text
download installer
 ↓
preflight
 ↓
backup
 ↓
install
 ↓
migrate DB
 ↓
self-test
```

---

# 102. Rollback

Una nueva versión no debe dejar al usuario atrapado si falla.

Debe existir:

- backup pre-upgrade;
- compatibilidad declarada;
- estrategia de restore;
- bloqueo de downgrade destructivo.

---

# 103. i18n

V1 puede mostrarse solo en español.

Pero textos deben estar centralizados.

Evitar hardcode disperso que impida futura traducción.

---

# 104. Hora y timestamps

DB:

- UTC.

UI:

- timezone local.

Timeouts:

- reloj monotónico cuando corresponda.

Cambiar hora de Windows no debe romper retries.

---

# 105. Windows specifics

Probar:

- NTFS;
- exFAT para USB;
- rutas largas;
- nombres reservados;
- antivirus;
- archivos temporalmente bloqueados;
- permisos;
- dispositivo removible.

---

# 106. Health Checks

Separar:

## Liveness

Proceso está vivo.

## Readiness

Puede atender operaciones normales.

## Deep Diagnostics

Comprueba:

- DB;
- filesystem;
- disk;
- connectors;
- CBZ subsystem;
- image subsystem.

---

# 107. Diagnóstico UI

Pantalla:

```text
Backend              ✓
Database             ✓
Library              ✓
Disk                 ✓
Image Processor      ✓
CBZ Builder          ✓
Connector A          ✓
Connector B          ⚠
LAN                  ✓
Version              x.y.z
```

---

# 108. Instalación

Yomira Setup debe instalar:

- backend;
- frontend compilado;
- runtime;
- assets;
- configuración inicial.

No requiere herramientas de desarrollo.

---

# 109. Desinstalación

Debe preguntar:

- conservar biblioteca;
- conservar configuración;
- eliminar todo.

Nunca borrar la biblioteca automáticamente.

---

# 110. Clean Machine Test

Antes de V1:

- Windows limpio;
- sin Python;
- sin Node;
- instalar;
- ejecutar;
- descargar desde Mock;
- leer;
- cerrar;
- reiniciar;
- desinstalar.

---

# 111. Upgrade Testing

Debe probar:

- alpha → beta;
- beta → RC;
- RC → V1;
- migración fallida;
- rollback;
- reinstalación misma versión.

---

# 112. Definition of Done

Una tarea solo termina si:

- implementación completa;
- tests;
- lint;
- types;
- documentación;
- logs;
- errores;
- regresión;
- revisión diff.

---

# 113. Release Gate

V1 requiere:

```text
TypeScript PASS
ESLint PASS
Ruff PASS
Pyright PASS
Backend Unit PASS
Frontend Unit PASS
Integration PASS
E2E PASS
Fault Injection PASS
Download Stress PASS
Memory Leak PASS
CBZ Verification PASS
DB Migration PASS
DB Restore PASS
Library Rebuild PASS
PC Browser PASS
Mobile PASS
Bigme PASS
USB Export PASS
LAN Auth PASS
Installer PASS
Upgrade PASS
Uninstaller PASS
Clean Machine PASS
```

Un P0/P1 = NO RELEASE.

---

# 114. Matriz de trazabilidad

Los requisitos tendrán IDs.

Familias:

```text
REQ-CORE
REQ-DB
REQ-JOB
REQ-DL
REQ-SRC
REQ-IMG
REQ-CBZ
REQ-UI
REQ-READ
REQ-LAN
REQ-SEC
REQ-REC
REQ-EXP
REQ-DIAG
REQ-PERF
REQ-REL
```

Cada requisito deberá poder relacionarse con:

```text
Requirement
 ↔
Implementation
 ↔
Test
```

---

# 115. Fases del proyecto

Mapa de rangos propuesto conforme al plan maestro V1.1. Se conservan los nombres de fase de la versión 1.0.

## Fase 0 — Constitución y reglas
K001–K004

## Fase 1 — Design System y UX
K005–K013

## Fase 2 — Tooling y esqueleto ejecutable
K014–K022

## Fase 3 — Backend Core
K023–K033

## Fase 4 — DB y dominio
K034–K048

## Fase 5 — Biblioteca local
K049–K058

## Fase 6 — Source Connector SDK
K059–K070

## Fase 7 — Mock Manga Server
K071–K080

## Fase 8 — Job Manager
K081–K096

## Fase 9 — Download Engine
K097–K116

## Fase 10 — Validación de imágenes
K117–K129

## Fase 11 — Procesamiento SAFE
K130–K142

## Fase 12 — CBZ + ComicInfo
K143–K157

## Fase 13 — Search + Chapter UX
K158–K169

## Fase 14 — Reader
K170–K186

## Fase 15 — Update Checker
K187–K195

## Fase 16 — Export + e-reader
K196–K206

## Fase 17 — Connectors reales
K207–K221

## Fase 18 — Diagnóstico y Recovery
K222–K237

## Fase 19 — Benchmark + RC0
K238–K255

## Fase 20 — Packaging + Clean Machine
K256–K269

## Fase 21 — V1 Release Gate
K270–K284
---

# 116. K001 — Constitución

## Objetivo

Aprobar este documento.

## Archivos permitidos

```text
docs/YOMIRA_V1_SPEC.md
docs/glossary.md
```

## Tests

Revisión documental:

- todos los pilares representados;
- estados definidos;
- amenazas cubiertas;
- no contradicciones;
- no TODO críticos.

## Aprobación

`YOMIRA_V1_SPEC.md = APPROVED`

---

# 117. K002 — ADRs iniciales

## Objetivo

Documentar decisiones fundamentales.

## Archivos

```text
docs/adr/*
```

## Tests

- consistencia;
- alternativas;
- consecuencias;
- rollback.

## Aprobación

Todos los ADRs iniciales en `Accepted`.

---

# 118. K003 — Ownership y dependency rules

## Objetivo

Definir fronteras.

Tabla mínima:

| Componente | Puede | No puede |
|---|---|---|
| Connector | obtener/normalizar | escribir library |
| Downloader | transferir | generar CBZ |
| Processor | transformar | hacer HTTP |
| CBZ Builder | empaquetar | consultar fuentes |
| Job Manager | coordinar | parsear HTML |
| React | solicitar | manejar filesystem |

## Archivos

```text
docs/ARCHITECTURE.md
docs/COMPONENT_OWNERSHIP.md
docs/DEPENDENCY_RULES.md
```

## Aprobación

Cada módulo tiene:

- input;
- output;
- responsabilidad;
- dependencias;
- failure boundary.

---

# 119. K004 — Error model y bug protocol

## Objetivo

Crear:

```text
docs/ERROR_CODES.md
docs/BUG_PROTOCOL.md
docs/LOGGING_SPEC.md
docs/SUPPORT_BUNDLE_SPEC.md
```

## Tests

Simulación completa:

```text
chapter
 ↓
timeout
 ↓
retry
 ↓
failure
 ↓
log
 ↓
UI
 ↓
support bundle
```

## Aprobación

Todo P0/P1 previsto tiene trazabilidad suficiente.

---

# 120. K005 — Design System

## Objetivo

Congelar identidad visual antes de React funcional.

## Archivos

```text
docs/DESIGN_SYSTEM.md
docs/UI_LAYOUTS.md
docs/E_READER_UX.md
```

## Debe definir

- paleta;
- tipografía;
- spacing;
- cards;
- chips;
- estados;
- breakpoints;
- focus;
- mobile;
- e-reader.

## Tests documentales

Casos:

- título largo;
- portada ausente;
- 0 resultados;
- 5000 capítulos;
- fuente caída;
- loading;
- error;
- empty.

## Aprobación

Diseño suficiente para implementar UI sin improvisar arquitectura.

---

# 121. Estado de aprobación, línea base y secuencia de ejecución

Mientras no exista una línea base aprobada de esta especificación, está prohibido:

- crear frontend funcional;
- crear backend funcional;
- crear DB real;
- crear connectors;
- escribir descargador;
- escribir lector.

La línea base normativa vigente es la versión 1.0, cuyo estado es APPROVED. La presente revisión 1.1 está IN REVIEW: no suspende ni sustituye esa aprobación. Hasta que el usuario apruebe explícitamente la V1.1, los requisitos funcionales y la restricción general de no comenzar implementación sin especificación aprobada se evalúan contra la V1.0 y los ADR aceptados. Este borrador no autoriza cambios basados exclusivamente en sus modificaciones propuestas. La selección del plan maestro V1.1 no equivale a aprobar la SPEC 1.1.

Los rangos de §115 se actualizan para reflejar el mapa de fases y tareas del plan maestro V1.1. Esta actualización documental no incorpora requisitos funcionales nuevos ni autoriza omitir los existentes.

La ejecución debe respetar la secuencia y los quality gates del plan maestro: ninguna fase comienza hasta que la fase anterior tenga PASS, y no se omiten tareas, revisiones ni criterios de cierre. La siguiente tarea se determina a partir del plan y del estado documentado del proyecto; esta sección no designa Fase 1 como siguiente actividad ni permite saltar fases o gates.
---

# 122. Criterio final de aprobación de esta SPEC

La especificación se considera congelada cuando:

- el usuario aprueba alcance;
- el usuario aprueba arquitectura;
- no quedan decisiones bloqueantes;
- los 6 pilares están cubiertos;
- las reglas de recuperación están definidas;
- los estados están definidos;
- seguridad y rendimiento tienen criterios;
- los límites de V1 están claros;
- el nombre Yomira está aprobado o reemplazado antes de crear namespaces y binarios.

Una vez congelada, cualquier cambio arquitectónico posterior requerirá ADR y revisión de impacto.

---

# FIN DE YOMIRA V1 TECHNICAL SPECIFICATION 1.1 — REVIEW DRAFT


