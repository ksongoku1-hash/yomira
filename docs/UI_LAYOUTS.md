# Yomira V1 — UI Layouts

**Estado:** APPROVED — Fase 1 alcanzó PASS en K013; ver PHASE_01_REPORT.md.
**Trazabilidad:** K006, K007, K008, K010, K011, K012 y K013.  
**Fuentes normativas:** YOMIRA_V1_SPEC.md §§44–48, 96 y 107; YOMIRA_MASTER_EXECUTION_PLAN.md, K006–K008 y K010–K013.  
**Alcance:** distribución, componentes y estados de interfaz; no implementa frontend ni define comportamiento de backend fuera de las fronteras aprobadas.

## 1. Marco común

La interfaz usa navegación superior fija en desktop. En tablet y móvil se comprime y conserva acceso a Home, Library, Search, Downloads, Settings y Diagnostics. La navegación no oculta la acción primaria ni el estado de un job activo. El área central usa el espacio restante y evita scroll horizontal salvo dentro de una región deliberadamente horizontal, como un carrusel.

Las obras se muestran como tarjetas con portadas protagonistas. La cuadrícula apunta a 5–7 portadas por fila en desktop, 4 en tablet y 2–3 en móvil. Bigme y otros navegadores de e-reader apuntan a 2–3 portadas y al perfil definido en E_READER_UX.md. Los anchos de corte iniciales son propuestas de Design System y se validan con contenido real.

## 2. Distribución por superficie

### Home

- Encabezado de bienvenida y acceso a Search.
- Secciones de biblioteca reciente y lectura reciente cuando exista información local.
- Avisos de capítulos nuevos como revisión manual; la interfaz no implica descarga automática permanente.
- Estado visible de servicios locales y acceso a Diagnostics cuando exista un fallo relevante.
- Estado sin contenido con instrucciones para añadir una obra o buscar una fuente, sin fingir que existe biblioteca.

### Library

- Controles de búsqueda local y filtros por estado/fuente cuando los datos estén disponibles.
- Cuadrícula responsive de MangaCard y encabezados de sección.
- Progreso de lectura y estado de capítulos presentados como datos separados.
- Portadas faltantes usan placeholder estable con título y estado; no bloquean apertura del detalle.
- Una biblioteca grande mantiene scroll y respuesta controlados mediante virtualización/paginación según se defina en implementación y benchmark. Nunca se crean miles de nodos visuales a la vez.

### Search

- SearchInput prominente con acción explícita de buscar y estado de fuentes.
- Resultados distinguen obra, fuente y estado de disponibilidad.
- Una fuente caída se muestra como fallo de esa fuente con código/diagnóstico accesible; no convierte resultados de otras fuentes en error global.
- Cero resultados informa la consulta y ofrece modificarla o limpiar filtros.
- No repetir consultas de red indefinidamente sin una acción visible o política aprobada.

### Detalle de obra y capítulos

- Presenta título completo, fuente y metadata disponible sin inventar valores ausentes.
- ChapterRow contiene selección, nombre, estado local y estado de descarga si aplica.
- La selección de uno, varios o todos debe mostrar su alcance y cantidad de forma verificable.
- La lista de 5000 capítulos debe usar virtualización y búsqueda/filtros. La selección no puede limitarse silenciosamente a las filas que están renderizadas.
- Si un título es largo, usar salto controlado o truncado visual con nombre accesible completo; los botones no se desplazan fuera de pantalla.

### Downloads

- Lista de jobs con serie/capítulo, etapa, progreso, estado, código de error y acciones disponibles.
- Separar en curso, en espera, completados, interrumpidos y fallidos si esa separación ayuda a recuperar el estado.
- El porcentaje no sustituye a la etapa; un job detenido explica si espera red, disco, reintento o intervención.
- Un error de disco lleno no aparece como completado ni inicia nuevos trabajos que excedan el presupuesto.
- Las acciones de cancelar, reintentar o borrar se muestran solo cuando las reglas de estado permitan hacerlo; no se inventa una acción destructiva en esta fase.

### Settings y Diagnostics

- Settings agrupa preferencias locales, biblioteca, red/LAN, seguridad y lector con valores actuales explícitos.
- LAN aparece desactivada por defecto y su activación explica autenticación y exposición de red antes de habilitarla.
- Diagnostics presenta liveness/readiness, DB, library, disk, image processor, CBZ, conectores, LAN y versión como lista legible y accionable.
- El detalle de fallo incluye código estable y opción de crear Support Bundle sanitizado; nunca muestra secretos.

### Reader

Su estructura y controles detallados están en E_READER_UX.md. ReaderToolbar mantiene navegación de capítulos, modo y ajustes sin cubrir páginas más de lo necesario.

## 3. Catálogo de componentes K010

| Componente | Contenido y estado que debe comunicar |
|---|---|
| TopNavigation | Destinos principales, destino activo y estado del servicio cuando sea necesario |
| SearchInput | Texto de consulta, filtros, búsqueda en curso, limpiar y error de entrada |
| SectionHeader | Título de sección y acción asociada, si existe |
| MangaCard | Portada/placeholder, título, fuente y estado mínimo relevante |
| MangaCarousel | Grupo horizontal con control accesible y sin robar navegación al resto de la página |
| FilterChip | Nombre, seleccionado/no seleccionado y acción de quitar filtro |
| SourceChip | Nombre de fuente y estado; no representa una garantía de disponibilidad |
| ChapterRow | Identidad de capítulo, selección, estado local y progreso pertinente |
| DownloadStatus | Estado textual estable, etapa y error/reintento disponible |
| ProgressBar | Progreso visual acompañado por texto; indeterminado cuando no se conoce porcentaje |
| EmptyState | Explica que no hay elementos y da siguiente acción no destructiva |
| ErrorState | Qué falló, código consultable y opciones de recuperación seguras |
| SkeletonCard | Esqueleto de carga inicial sin parecer contenido real |
| Modal | Contexto y decisión requerida; foco contenido y devuelto correctamente |
| Toast | Confirmación breve no crítica y anunciada de forma accesible |
| ReaderToolbar | Capítulo, modo de lectura y controles principales accesibles |

Cada componente debe funcionar con teclado y touch, tener nombre accesible y estado textual. No contiene reglas de filesystem ni acceso directo a DB.

## 4. Estados extremos K011

| Caso | Presentación requerida | Protección de integridad y recursos |
|---|---|---|
| 0 resultados | EmptyState ligado a consulta/filtros y acción para corregirlos | No mostrar error genérico ni crear resultados falsos |
| 5000 capítulos | Búsqueda/filtro, conteo, virtualización y selección cuyo alcance queda explícito | No renderizar todos ni perder selecciones al desplazarse |
| Título largo | Ajuste o truncado accesible con título íntegro disponible | No solapar controles ni alterar nombre persistido |
| Portada ausente | Placeholder consistente y legible | No bloquear el detalle ni sustituir metadata |
| Fuente caída | Error ligado a la fuente, código y recuperación explícita | Otras fuentes y biblioteca local siguen visibles |
| Carga lenta | Skeleton inicial o estado de espera con acción de cancelar si procede | Evitar duplicar solicitudes/reintentos invisibles |
| Error | ErrorState junto al contexto afectado, código y acción segura | Nunca presentar operación parcial como exitosa |
| Lista vacía | Estado vacío distinto de carga y error | Mantener navegación y opciones disponibles |
| Internet desconectado | Diagnóstico claro; biblioteca local sigue utilizable | No borrar ni invalidar contenido descargado |
| Disco lleno | Estado de fallo con etapa y explicación | Preservar temporales/artefactos para reconciliación; no marcar completado |
| Backend no listo | Pantalla de recuperación con diagnóstico | No enviar operaciones hasta readiness |

## 5. Reglas de comportamiento visual

- Distinguir carga inicial, refresh en curso, lista vacía y error.
- Mantener el contenido local anterior durante refresh cuando sea válido, señalando su antigüedad/estado si corresponde.
- Mostrar confirmación después de la respuesta del sistema para acciones que escriben o cambian estado persistente.
- No esconder error crítico detrás de Toast temporal.
- Conservar orden visual y foco después de filtrar, cerrar un modal o volver del detalle.
- No usar animaciones como único indicador de progreso.

## 6. Criterios K006–K013

Los layouts cubren desktop, tablet y móvil con los conteos objetivo de portada; el perfil e-reader se especifica aparte. Están definidos los componentes pedidos y estados extremos, incluida biblioteca grande, fallos de fuente, red y disco. La aprobación de Fase 1 requiere revisar este documento junto con DESIGN_SYSTEM.md y E_READER_UX.md.

**Base de implementación aprobada:** los anchos de corte, navegación compacta en móvil y tablet, ubicación de filtros y distribución de secciones se aceptaron como valores iniciales. Las decisiones sobre integridad, aislamiento de componentes, LAN opt-in, privacidad y límites de memoria siguen sujetas a la SPEC y ADR aceptados.
