# Apertura del lector — 17 de octubre de 2026

Mejora local sobre Yomira 0.1.15. Activa en E:\yomira y http://127.0.0.1:5173/. El instalador 0.1.15 anterior no contiene estos cambios; no se construyó ni publicó otro instalador.

La interfaz pedía reconstruir el plan online en cada apertura, incluso con una sesión completa todavía válida. Ahora la apertura normal reutiliza esa sesión. Reintentar página solicita una renovación explícita y conserva las comprobaciones de identidad, serie y cantidad de páginas. Una sesión pausada o fallida vuelve a abrirse, sin reutilizar el estado incompleto.

Las imágenes originales verificadas se conservan en RAM dentro del capítulo activo, con un máximo de 128 MiB de bytes comprimidos y expulsión de las usadas menos recientemente. No se guardan páginas online en disco ni se crean trabajos de descarga. El presupuesto de imágenes decodificadas del navegador conserva su límite existente. Las respuestas mantienen las cabeceras privadas y no-store. La pausa, la renovación explícita y el cambio de capítulo liberan la caché anterior. Solo se almacenan imágenes que pasan todas las validaciones.

La página restaurada en lectura vertical se solicita antes de las páginas posteriores y de la anterior. Las imágenes ya almacenadas y la reapertura de la sesión válida pueden responder sin esperar detrás de una imagen vecina lenta. El máximo de dos transmisiones retenidas sigue vigente; las validaciones de páginas y el guardado del progreso no se han eliminado.

## Mediciones reales

Fuente: NovelCoolES, The Boxer 16, página 3. Dos aplicaciones y motores de prueba separados de la instalación personal, con la misma imagen de 207056 bytes y SHA-256 c3595bc47dd4a9630ca8e58bc439356cf5c002afc6ae544f6a6fed1874306326.

| Operación | Antes | Después |
|---|---:|---:|
| Pedir de nuevo la imagen ya vista | 2927.70 ms | 10.94 ms |
| Primera apertura y primera imagen sin caché | 4985.88 ms | 5338.65 ms |

La primera recepción de esa imagen sigue requiriendo aproximadamente tres segundos de la fuente y el plan cerca de dos. La mejora de caché afecta las reaperturas del capítulo que continúa activo y las páginas ya verificadas; no garantiza una apertura instantánea de capítulos nuevos. Las mediciones son muestras, no un promedio estadístico ni una prueba de todas las fuentes. La nueva prioridad evita solicitar una página anterior antes de la posición restaurada, sin atribuir a esa parte un ahorro medido que no se haya registrado.

## Validación y activación

27 casos del servidor y 221 de la interfaz aprobados. Incluyen límite de bytes y LRU, reutilización de imágenes, renovación explícita, pausa y reapertura, imagen inválida, cancelación, capacidad de transmisión, prioridad ante una precarga lenta y orden de petición de la página guardada. TypeScript, ESLint, Ruff, Pyright de aplicación y compilación de la interfaz correctos.

La activación comprobó el HTML servido comparando bytes. La primera comprobación anterior utilizó texto con normalización de saltos de línea, rechazó esa diferencia de formato y restauró automáticamente la aplicación. La activación final es correcta. El servicio está listo y sirve la compilación nueva. Las tablas de progreso, ajustes, trabajos de descarga, series y capítulos son idénticas a la copia privada anterior al reinicio; conserva 34 posiciones y 896 trabajos. La base de datos pasó quick_check.

Recargar el navegador para usar el cliente nuevo; una pestaña con el JavaScript anterior seguirá pidiendo la renovación de sesión en cada apertura.
