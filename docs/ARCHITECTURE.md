# Arquitectura de Yomira V1

**Estado:** APPROVED — K003 aprobado formalmente por el usuario el 2026-09-25.  
**Base normativa:** `YOMIRA_V1_SPEC.md` y los ADR iniciales en estado `Accepted`.

## 1. Propósito y reglas de diseño

Este documento establece las fronteras que debe respetar la implementación. Cada incremento se autoriza y verifica mediante las tareas y gates del Master Execution Plan.

- La UI solicita operaciones mediante `/api/v1`; no accede al filesystem ni a SQLite.
- Los conectores obtienen y normalizan datos remotos; no escriben en la biblioteca ni coordinan jobs.
- Los artefactos no alcanzan estado final hasta verificar contenido y publicar atómicamente.
- El estado de jobs y las decisiones de recuperación son persistentes y trazables.
- La escritura SQLite se centraliza y se mantiene breve.
- La raíz operativa local y la biblioteca visible de CBZ son independientes; la biblioteca predeterminada está en `%USERPROFILE%\Documents\Mangas` y publica dentro de su árbol validado (ADR-0120).
- Workers, cachés, precarga, logs, buffers y decodificación tienen límites configurados.
- Ante una ambigüedad de integridad, se detiene o conserva el elemento para inspección; no se elimina contenido válido por suposición.

## 2. Capas y responsabilidades

| Capa | Responsabilidad | Puede depender de | No puede hacer |
|---|---|---|---|
| React UI | Mostrar biblioteca, búsqueda, jobs, lector y diagnósticos; validar interacción | Contratos API y estado UI | Leer/escribir rutas locales, SQLite o declarar una operación crítica como completada de forma optimista |
| API FastAPI | Validar petición, autorizar operación local/LAN, llamar servicios y serializar respuesta | Schemas, aplicación, servicios | Parsear HTML de una fuente dentro del endpoint o implementar reglas críticas de filesystem en la UI |
| Application / Job Manager | Coordinar casos de uso, prioridad, transición de estado y cancelación segura | Dominio, interfaces de servicio y repositorios | Parsear páginas de una fuente o empaquetar CBZ directamente |
| Dominio | Tipos, invariantes, estados, autoridad de metadata y reglas de identidad | Tipos y contratos | Hacer HTTP, manipular UI o abrir rutas arbitrarias |
| Source Connectors | Consultar fuentes y normalizar resultados | Contratos tipados, contexto de host con capacidades declaradas y cliente HTTP acotado | Escribir biblioteca/DB, acceder a repositorios, generar CBZ o cambiar estados de jobs |
| Download Engine | Streaming a temporales, timeouts, retries, hashes y límites de concurrencia | Cliente HTTP, Job Manager y manifiesto mediante interfaces | Publicar CBZ o saltarse validación de contenido |
| Validators / Image Processor | Validar formato/dimensiones y aplicar procesamiento SAFE | Archivos temporales verificados y límites configurados | Hacer HTTP, acceder a metadatos remotos sin validar o recomprimir páginas sin transformación necesaria |
| CBZ Builder / Verifier | Empaquetar orden estable, crear ComicInfo, verificar archive y solicitar publicación atómica | Páginas validadas, metadata aceptada y servicio de archivos | Consultar fuentes o marcar un job completado por sí solo |
| Repositories / SQLite | Persistir entidades, estados y relaciones mediante transacciones breves | Modelos y capa de persistencia | Aceptar SQL arbitrario de connectors o convertirse en un segundo coordinador de jobs |
| Filesystem services | Resolver paths bajo roots permitidos, temporales, publicación, backup y export | Configuración validada, DiskBudgetManager y Recovery Manager | Seguir rutas remotas sin sanitizar o borrar temporales sin reconciliar |
| Recovery / Diagnostics | Comprobar, reconciliar y explicar estado al arranque y bajo demanda | DB, manifests, filesystem, logging | Reanudar un artefacto no verificado como `COMPLETED` |

## 3. Flujo crítico de capítulo

```text
React
  → API valida petición
  → Job Manager crea/encuentra job persistente
  → FMD2 consulta la fuente pública y resuelve páginas
  → Yomira transfiere páginas al staging bajo el ID durable del job
  → Download Engine transmite a .part y calcula SHA-256
  → Validator verifica HTTP, MIME, magic bytes, formato, decode y límites
  → Processor aplica SAFE solo si es seguro
  → CBZ Builder crea .tmp y agrega páginas/ComicInfo
  → Verifier valida ZIP, páginas, metadata y hashes
  → Filesystem service publica con rename atómico
  → Repository indexa el asset y cierra el job
  → API informa COMPLETED
```

Una respuesta HTTP exitosa no reemplaza validación. Una descarga o empaquetado parcial no usa la extensión publicada. Si falla una etapa, el Job Manager registra el estado/error y Recovery decide si puede reanudarse o si debe conservarse para diagnóstico.

La adquisición usa FMD2 como proceso local separado por una API autenticada en loopback. El puente pertenece al núcleo (`app/fmd2_bridge.py`); el registro de conectores reutiliza esa instancia sin crear procesos ni escribir archivos. La lista autorizada comprende WebToons, CapibaraTraductor, Cartel de Manhwas, Apoll Comics, Asura Scans y TuMangaOnline. Cada fuente requiere un catálogo disponible; su inclusión no garantiza disponibilidad remota. Yomira valida y conserva la autoridad sobre páginas, staging, manifiestos, ComicInfo.xml, CBZ, biblioteca y lector. Las referencias compactas de capítulos v2 conservan compatibilidad de lectura con v1.

FMD2 conserva los bytes originales de las páginas en modo Yomira y no ejecuta las conversiones nativas de PNG/WebP. Limita el archivo crudo guardado de cada página a 64 MiB y cada capítulo a 512 MiB. El núcleo contabiliza ese staging al reservar el presupuesto de temporales y libera únicamente los archivos de un job completado después de confirmar la publicación e indexación del CBZ verificado. Una adquisición sin progreso durante 180 segundos solicita cancelación y deja un error reintentable. Un conflicto de idempotencia conserva el trabajo previo. La búsqueda conjunta intercala resultados de las fuentes disponibles dentro del límite total; el estado de salud se degrada si falta un catálogo autorizado.

Las portadas usan los dominios de imágenes autorizados de cada fuente. MangaDex aporta únicamente metadatos y portadas de respaldo, tanto para resultados de búsqueda como para la biblioteca local. Las portadas de TuMangaOnline admiten exclusivamente su marcador numérico estático `v` en las rutas autorizadas; los parámetros firmados siguen rechazados.

SQLite, manifests, páginas temporales y logs permanecen bajo la raíz operativa (`YomiraData` en instalaciones nuevas; `AlphaData` se reutiliza si ya contiene la base existente y no hay otra raíz configurada). Los CBZ nuevos y su staging usan `library_root`, cuyo valor predeterminado es `%USERPROFILE%\Documents\Mangas` (ADR-0120). Los assets nuevos guardan rutas relativas a esa raíz; las rutas antiguas `library/chapters/...` siguen resolviéndose bajo la raíz operativa para conservar la biblioteca ya indexada, según ADR-0115.

## 4. Límites de fallo y datos

### Fuente remota

HTML, JSON, URLs, nombres, metadata y bytes de imagen se consideran no confiables. Un fallo de connector debe producir error tipado, health status y trazabilidad; nunca un cambio parcial de biblioteca.

### Descarga y archivo

Cada página tiene referencia, ordinal, temporal, estado, tamaño, hash, tipo, dimensiones e intentos en el manifest versionado. El archivo se promueve de `.part` solo tras validación básica. Límites previos a decode protegen contra imágenes enormes.

### Procesamiento y empaquetado

El procesador recibe contenido local validado y no conoce HTTP. Si no puede probar que el recorte es seguro, conserva la imagen. El builder consume una secuencia validada y el verifier decide si el archivo puede publicarse.

### SQLite y filesystem

Cada publicación sigue: crear temporal, verificar, rename atómico, transacción de indexación. Si el rename funciona pero falla el índice, Recovery reconcilia el CBZ con DB. Si el índice no se confirmó, la UI no informa `COMPLETED`.

### Exportación

Las enumeraciones y operaciones COM de WPD/MTP se ejecutan en un proceso auxiliar de una sola solicitud. El backend principal conserva la autoridad sobre el historial y el estado persistente; el auxiliar recibe DTO JSON UTF-8 acotados y no abre SQLite. Una caída nativa o timeout del auxiliar se convierte en error trazable y no debe terminar la API. Enumerar destinos tiene timeout de 120 s y permite reintento; exportar tiene timeout de 1 h y falla de forma terminal si el resultado remoto puede ser ambiguo. Los logs no incluyen stderr del auxiliar ni identificadores privados de dispositivo. Este aislamiento contiene la caída del proceso, pero no demuestra que la causa nativa esté corregida ni reemplaza la prueba física K206.

La exportación detecta unidades removibles con letra y almacenamiento portátil Windows (WPD/MTP) sin letra. En unidades usa su propio `.part`, comprobación de espacio, hash y publicación sin reemplazo. En WPD/MTP crea un objeto temporal, verifica su lectura completa por hash y solo entonces cambia el nombre visible al nombre final; no lo presenta como rename atómico del sistema de archivos. Un conflicto conserva el archivo existente y una desconexión o resultado ambiguo conserva el temporal y deja un fallo trazable. El CBZ de la biblioteca nunca se modifica.

## 5. Persistencia y recuperación

- SQLite usa WAL, claves foráneas, timeout configurado y un escritor coordinado.
- DB guarda rutas relativas cuando sea posible; la raíz y cada ruta se validan antes del acceso.
- Migración requiere preflight, backup, migración, `integrity_check`, validación de schema y commit.
- En fallo crítico se entra en Safe Mode; nuevas descargas quedan bloqueadas.
- Recovery inspecciona manifests y temporales antes de limpiar o reintentar.
- Rebuild de biblioteca puede reconstruir índice desde CBZ y `ComicInfo.xml`, con fallback de filename documentado.

## 6. Observabilidad y privacidad

Los límites de componente propagan `trace_id`, código de error y contexto aplicable (`job_id`, `chapter_id`, `source_id`, etapa, intento y duración). El usuario recibe mensaje y código; Support Bundle conserva diagnóstico sanitizado y excluye contenido CBZ, páginas, cookies, tokens, contraseñas, blobs del Vault y sus referencias opacas.

El SDK entrega a los connectors first-party un `ConnectorHostContext` inmutable que expone inicialmente solo `SecretVault`. Un chequeo AST bloquea imports de persistencia/filesystem y llamadas directas de escritura. Es una frontera de capacidades para código incluido por Yomira, no un sandbox de SO contra plugins Python hostiles; el producto no carga connectors de terceros.

## 7. Límites de red

- Default loopback: `127.0.0.1`.
- LAN solo con opt-in explícito, autenticación y sesión temporal.
- La petición LAN no accede directamente al filesystem; pasa por la misma API y validación.
- No se incluye servidor público, nube, bypass de DRM/CAPTCHA/paywall ni evasión de acceso.
- Para búsquedas y descargas, `YOMIRA_FMD2_EXECUTABLE` apunta a la ruta absoluta del runtime completo de FMD2. Si no está configurado, Yomira mantiene biblioteca y lector disponibles y omite las fuentes de adquisición. Si está configurado pero falla el arranque del puente, registra su indisponibilidad, conserva biblioteca y lector, y permite reintentar al consultar la fuente.

## 8. Aprobaciones y control de cambios

K001–K004 están formalmente aprobadas. Los 20 ADR iniciales están en estado `Accepted`; la arquitectura y sus fronteras de responsabilidad también fueron aprobadas. Las Fases 1, 2 y 3 constan como `PASS` en sus informes. El trabajo posterior continúa de forma secuencial bajo los gates del plan v1.1.

La secuencia de implementación es: Fase 0 (K001–K004), Fase 1 (K005–K013), Fase 2 (K014–K022), Fase 3 (K023–K033) y después las fases posteriores del plan v1.1, sin saltarse gates. La frase anterior de la sección 121 de la especificación que saltaba de Fase 0 a Fase 2 era una inconsistencia editorial con la tabla de fases de la propia especificación (§115) y el plan v1.1. Se aclaró en la copia de trabajo de la especificación y se dejó registrada en el informe de Fase 0.

Cualquier cambio posterior que altere comportamiento, límites, seguridad, persistencia o recuperación requiere un ADR aceptado antes de implementarse. Este documento fija fronteras y no autoriza saltarse los gates.

