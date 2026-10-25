# Prioridad de la primera imagen y encuadre — 18 de octubre de 2026

Activo en E:\yomira, http://127.0.0.1:5173/. Solo cambió la interfaz; no se reinició el servicio.

## Resultado

La página visible termina de llegar antes de iniciar solicitudes nuevas de vecinos y miniaturas. En doble página se piden las dos imágenes visibles juntas; la precarga espera a ambas. Las páginas ya guardadas en memoria siguen disponibles sin esperar.

El ajuste automático usa el ancho disponible para imágenes cuya relación ancho/alto es menor de 0,5. Las páginas normales conservan su encuadre, el zoom escala el ancho y los ajustes manuales al alto o al ancho conservan prioridad. No se añaden preferencias nuevas.

## Pruebas

444 casos únicos de interfaz aprobados. En la suite completa pasaron 443: el caso restante esperaba las antiguas tres solicitudes incluso cuando fallaba la primera imagen. Se actualizó para exigir una sola petición, se verificó que no hubiera escritura de progreso y se volvió a ejecutar ReaderExperience.test.tsx completo: 59/59. frontend-passed.xml reúne todos los casos, sin fallos.

Se añadieron comprobaciones de espera de la imagen principal, carga de ambas páginas visibles, ancho de tiras largas y prioridad de un ajuste manual al alto. Tipos, estilo y compilación aprobados. La compilación final coincide con el HTML comprobado visualmente.

Prueba visual aislada: The Boxer 21, página 3, 700 × 19678 píxeles. Se mostró con 1241 píxeles de ancho en ajuste automático, con desplazamiento dentro de la misma página. PageDown desplazó 630 píxeles y mantuvo la página 3. Los eventos del navegador comprobaron que las peticiones de las páginas 4 y 5 comenzaron después de terminar el ciclo de la petición visible; la imagen se comprobó cargada y con dimensiones naturales válidas. La captura está en reader-wide.png.

## Medición aislada de primera imagen sin caché

Motor y perfil privados separados del perfil personal. Medición en milisegundos, por fuente, reanudando en la página 3:

| Fuente | Apertura y progreso | Imagen completa | Fuente/transporte | Validación | Escritura caché | Imagen en RAM |
|---|---:|---:|---:|---:|---:|---:|
| MangaDex: The Boxer 21 | 673.95 | 923.29 | 787.66 | 89.2 | 33.61 | 10.39 |
| Native: The Boxer 16 | 2045.66 | 1461.25 | 1271.35 | 164.38 | 13.62 | 7.62 |

La apertura y la imagen se midieron por separado; estas cifras no son tiempos totales de clic a imagen pintada en el navegador. Son observaciones de una conexión y no una comparación estadística antes/después. Predominó el tiempo de la fuente; no se omitió validación ni se alteraron los límites de solicitudes.

## Activación y respaldo

Copia consistente del perfil, interfaz previa en dist-before, archivos originales y parche en:
E:\yomira\archive\reader-first-frame-20261018-201455

Se conservaron 40 registros de progreso y 896 trabajos de descarga existentes al activar. La activación no escribió progreso. Se mantienen los recursos anteriores para las pestañas abiertas. El inicio principal se comprobó listo después de la activación y mostró el punto de lectura personal actual.

## Pendiente

Extender la preparación anticipada a Continuar leyendo y preparar una versión distribuible de GitHub. Las descargas frías siguen dependiendo de la fuente. El encuadre automático respeta un ajuste manual al alto previamente guardado; Restablecer zoom vuelve al ajuste automático. El guardado de posición dentro de una tira permanece disponible en modo vertical; en página única se conserva la página guardada, sin añadir en este cambio una posición interna persistente.
