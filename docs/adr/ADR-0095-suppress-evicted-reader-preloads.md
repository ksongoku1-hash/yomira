# ADR-0095 — Supresión de páginas de precarga expulsadas

## Estado

Accepted — 2026-09-27. Aclara K181 y ADR-0092.

## Contexto

Al reducir el límite LRU, el lector puede expulsar una página que sigue dentro de la ventana vertical, aunque no esté visible. Si el efecto de recursos interpreta esa expulsión como un recurso ausente normal, vuelve a solicitar inmediatamente la misma página. Con páginas decodificadas mayores que el presupuesto, una nueva carga puede volver a expulsarse; invalidaciones posteriores de ventana o ajustes repiten la operación y desperdician I/O sin beneficiar la página actual.

## Decisión

1. Cuando el LRU expulsa una página vertical no visible, el lector aborta cualquier solicitud pendiente, revoca su object URL, la retira de recursos activos y marca `(chapter_id, page_number)` como suprimida durante la residencia actual en la ventana.
2. Una página suprimida no se solicita otra vez mientras siga fuera del viewport en la parte de retención/precarga, incluso si cambian los ajustes sin cambiar la ventana.
3. Si la página entra al viewport o se convierte en la página actual, se elimina la supresión y se solicita bajo demanda desde el CBZ verificado. Si sale por completo de la ventana, también se elimina la marca; al reingresar puede volver a precargarse.
4. La marca es efímera, limitada al capítulo activo y se descarta al cambiar o cerrar el capítulo. No se persisten páginas, blobs, errores ni marcas de supresión.
5. Las páginas visibles/actuales continúan fijadas según ADR-0092. No cambian `preload_ahead`, el límite de cuatro páginas, el presupuesto de caché ni los ajustes guardados.

## Consecuencias

- Una página fuera de presupuesto puede mostrarse como espacio de carga mientras permanece únicamente en precarga; se obtiene cuando el usuario la alcanza.
- Cada entrada a una ventana de precarga tiene como máximo una carga no visible que puede expulsarse; una expulsión no inicia una cadena de solicitudes y decodificaciones.
- La lectura de la página actual permanece disponible conforme a las excepciones visibles acotadas de ADR-0092.

## Reversión

Quitar esta supresión permite volver a cargar páginas LRU expulsadas inmediatamente desde el efecto de recursos. Cualquier cambio que reactive precargas expulsadas debe conservar una cota verificable de solicitudes y evitar ciclos de I/O ante imágenes cuyo coste decodificado exceda el límite.
