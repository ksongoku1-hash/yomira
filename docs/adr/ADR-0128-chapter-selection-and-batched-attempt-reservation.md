# ADR-0128: Selección visible de capítulos y reserva conjunta de intentos

## Estado

Aceptado en el alcance de la solicitud del usuario del 2026-10-07: reestructurar la elección de capítulos y agilizar la descarga.

## Decisión

- Mostrar casillas para capítulos individuales, filtro por etiqueta, selección de visibles y selección por rango. Conservar el orden y las referencias originales de la fuente y el máximo de 100 capítulos por petición. El filtro no elimina selecciones ocultas; «Seleccionar visibles» reemplaza la selección y señala explícitamente el tope de 100.
- Reservar los intentos y rutas independientes de las dos páginas del lote en una única publicación durable antes de solicitar cualquiera. Usar el mismo mecanismo de intención, transacción, manifiesto, verificación y cierre de publicación existente.
- Actualizar únicamente las filas que cambiaron, también cuando cambian dos páginas; conservar la comprobación de estado esperado y la verificación de la proyección completa.
- Ejecutar la persistencia de selecciones y altas de biblioteca fuera del bucle de solicitudes de la API, mediante el ejecutor de hilos existente, para permitir consultar la cola mientras se guardan selecciones grandes.
- Mantener las confirmaciones de descarga y validación por página, los límites de concurrencia global y por host de 2, los reintentos y las comprobaciones de pausa, cancelación, presupuesto, imágenes y publicación de CBZ.

## Evidencia y comprobación

La descarga aislada de nueve páginas de Apoll Comics antes del cambio realizó 27 publicaciones de estado de página, que sumaron 8,88 s. La reserva conjunta reduce ese número a 23: cinco reservas, nueve confirmaciones y nueve validaciones. No omite escrituras necesarias para recuperar una página confirmada.

Verificar que ambas rutas estén reservadas antes de la primera solicitud, que la concurrencia y el orden se mantengan, y que cancelación, pausa, reintentos, recuperación y publicación sigan pasando sus pruebas existentes. Comparar la misma descarga real en una biblioteca aislada; no atribuir diferencias de red al cambio local.
