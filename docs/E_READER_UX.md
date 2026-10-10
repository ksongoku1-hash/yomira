# Yomira V1 — E-reader UX

**Estado:** APPROVED — Fase 1 alcanzó PASS en K013; ver PHASE_01_REPORT.md.
**Trazabilidad:** K009, K010, K012 y K013.  
**Fuentes normativas:** YOMIRA_V1_SPEC.md §§40–43, 45–47 y 67–71; ADR-0012; YOMIRA_MASTER_EXECUTION_PLAN.md, K009 y K010.  
**Alcance:** experiencia del lector en el navegador de un e-reader y en dispositivos de pantalla limitada; no crea servidor, exportador ni lector funcional.

## 1. Objetivo y perfil

El lector debe permitir lectura local estable en el PC y en un navegador de e-reader cuando el usuario habilite LAN de manera explícita. Bigme es una clase de dispositivo de referencia de la SPEC, no una excepción codificada por nombre de modelo. El layout se adapta al viewport y a capacidades observables sin depender de una lista rígida de marcas.

El contenido CBZ publicado es inmutable mientras se lee. El cliente pide páginas al ReaderAssetService; React no recibe rutas locales ni accede directamente al filesystem. Caché, extracción y renderizado respetan límites configurables. Si una página no puede verificarse o servirse, se informa el fallo y no se sustituye silenciosamente por una página distinta.

## 2. Modos y presentación

El control de modo ofrece los cuatro modos normativos:

| Modo | Presentación |
|---|---|
| Página única | Una página completa; navegación anterior/siguiente |
| Vertical | Páginas en secuencia vertical con scroll controlable |
| Doble página | Dos páginas cuando viewport y orientación lo permiten; respetar páginas dobles clasificadas |
| RTL | Dirección de lectura derecha a izquierda; no reordena ni modifica los bytes del CBZ |

Controles normativos: zoom, fit width, fit height, fullscreen, progreso, teclado y touch. El modo actual y los ajustes activos permanecen visibles o se pueden revelar con una acción consistente. Ningún control cubre texto o área importante de la página de forma permanente.

Propuesta de ajuste inicial: iniciar una obra en Página única, conservar la última preferencia explícita del lector y recordar progreso por capítulo solo después de confirmación del backend. Esta preferencia y su alcance requieren aprobación; no modifican imágenes, CBZ ni orden almacenado.

## 3. Perfil e-reader

Al activarlo, el perfil:

- elimina blur, animaciones, transparencias y sombras;
- reduce transiciones al mínimo y evita movimiento automático de página;
- eleva contraste y tamaño táctil;
- usa superficies planas y bordes nítidos para estabilizar refresco;
- reduce precarga y evita refrescos visuales innecesarios;
- conserva todos los controles esenciales, su foco visible y texto de estado;
- ofrece salida clara al modo normal sin recargar ni alterar el capítulo.

Propuesta visual para revisión: superficie neutra de alto contraste y controles con contorno, sin exigir color para identificar modo, selección o fallo. Los valores de contraste deben verificarse antes de aprobar K013.

## 4. Navegación y entrada

La interfaz soporta las entradas de teclado y touch exigidas por la SPEC. El mapeo exacto de teclas siguiente es una propuesta para revisión, no una convención congelada:

| Acción | Mapeo propuesto |
|---|---|
| Página siguiente/anterior | Flechas y Page Down/Page Up, respetando la dirección de lectura en modos paginados |
| Salir de fullscreen | Escape |
| Alternar fullscreen | F |
| Acercar/alejar | + / - |
| Ajuste inicial de zoom | 0 |

Touch: tap en zonas laterales para anterior/siguiente en modos paginados, controles visibles para zoom y gesto de desplazamiento en Vertical. No asignar gesto destructivo a un toque accidental. Los controles deben tener alternativa visible para quien no pueda usar gestos.

El diálogo de controles debe ser accesible por teclado. El foco no se pierde al cambiar de página o cerrar la barra. En 200 % de zoom deben seguir disponibles salida, progreso y controles principales.

## 5. Ventana de páginas, caché y memoria

La SPEC prohíbe cargar todo el capítulo, pide ventana configurable y propone un límite inicial de caché de 500 MB con eviction LRU. El ejemplo normativo de ventana es: página anterior liberada, una anterior en caché, página actual visible y dos páginas siguientes precargadas. Se toma como perfil inicial revisable; el lector debe poder reducir precarga para dispositivos lentos.

Reglas de diseño:

- La página visible no se expulsa mientras esté en uso.
- Una página fuera de ventana puede liberarse y solicitarse otra vez desde el CBZ verificado.
- La caché es temporal y reconstruible; nunca contiene la única copia de una página.
- La presión de memoria reduce precarga antes de comprometer la página visible.
- Un fallo al precargar no bloquea la lectura actual y aparece como estado recuperable al llegar a esa página.
- La caché respeta presupuesto global configurable y LRU; nunca crece sin límite.

La política de página visible fijada en caché y la reacción exacta a presión de memoria se validan en implementación/benchmarks, no se dan por probadas con este documento.

## 6. Progreso, errores y reconexión

El lector distingue carga inicial, página cargando, página visible, página fallida y reconexión. Si LAN se interrumpe, mantener el contexto en el servidor y permitir retomar la misma obra/página cuando vuelva el enlace, sujeto a la sesión autorizada. No suponer que el dispositivo guarda una copia local permanente.

El progreso presentado como guardado debe estar confirmado por el sistema. Un error al persistir progreso no debe impedir seguir leyendo el contenido disponible y debe indicarse por separado; al recuperar conexión, se ofrece reintentar sin inventar una posición guardada. La página se identifica por el índice/orden validado del capítulo, no por un nombre de archivo presentado como autoridad.

LAN permanece desactivada por defecto. Cuando se habilite, requiere autenticación local y sesión temporal según la SPEC. Nunca exponer lector o acciones sensibles sin autenticación. No incluir cookies, tokens o secretos en logs ni Support Bundle.

## 7. Accesibilidad y estabilidad

- Foco visible y orden de tabulación predecible.
- Etiqueta accesible para cada control, modo y estado de conexión.
- Mensajes de error anunciados sin mover foco de forma inesperada.
- No depender solo de color, vibración, hover ni animación.
- Touch targets grandes y separados para reducir acciones involuntarias.
- Zoom del navegador y controles del lector no deben volver inaccesible la salida.
- Respetar preferencia de movimiento reducido; el perfil e-reader siempre mantiene transiciones mínimas.

## 8. Criterios K009 y K013

Este diseño cubre modos de lectura, navegación, perfil e-reader, zoom, progreso, acceso LAN, errores, virtualización, caché y límites de memoria. La revisión K013 debe confirmar que los cuatro modos y estados descritos bastan para implementar sin decisiones de arquitectura improvisadas.

**Base de implementación aprobada:** modo inicial, persistencia de preferencias, mapeo de teclas y gestos, presentación monocroma, comportamiento de reconexión y parámetros iniciales de ventana/caché se aceptaron como valores iniciales. Los límites de seguridad, aislamiento de archivos y presupuesto configurable provienen de la especificación y no se relajan.
