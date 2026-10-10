# ADR-0101 — Paginación keyset para historial USB

## Estado

Accepted — 2026-09-28.

## Contexto

ADR-0097 requiere historial consultable con un máximo de 100 registros por respuesta. El endpoint existente usa `limit` y `offset`; desplazar filas con inserciones o retención concurrentes puede repetir u omitir registros al recorrer varias páginas. La interfaz V1 necesita recorrer el historial durable sin cambiar los clientes existentes.

## Decisión

1. Se conserva sin cambios el contrato `GET /api/v1/usb-exports?limit=&offset=`.
2. Se agrega `GET /api/v1/usb-exports/pages?limit=&cursor=` para la interfaz. Ordena de forma descendente por `(created_at_us, export_id)` y continúa con una comparación lexicográfica estricta anterior al último par recibido.
3. El cursor es una cadena base64url opaca, versionada como `v=1`, que contiene únicamente `created_at_us` y el UUIDv4 `export_id`. Se rechazan campos extra, versiones desconocidas, valores fuera de rango y codificaciones no canónicas.
4. `next_cursor` sólo se devuelve si se observó otra fila después de la página actual. `total` es el conteo observado por esa petición, no una promesa de snapshot entre páginas.
5. Las filas terminales pueden desaparecer por la retención aprobada entre peticiones. El cliente no vuelve a recibir una fila ya atravesada por el cursor cuando aparecen filas más nuevas; las eliminaciones aún visibles por retención no se restauran ni se saltan hacia atrás. La paginación no congela una snapshot del historial.

## Consecuencias

- La UI obtiene páginas acotadas y conserva el endpoint offset para compatibilidad.
- La consulta keyset evita el desplazamiento causado por inserciones más nuevas y mantiene el orden determinista mediante UUID como desempate.
- `total` puede cambiar entre páginas; los registros antiguos eliminados por retención no aparecen en páginas posteriores. No se promete consistencia de snapshot ante cambios concurrentes.
- El cursor no contiene rutas de montaje, datos de archivo ni secretos y no concede autorización para exportar.

## Verificación

- Probar límites de página y rechazo de cursores inválidos/no canónicos.
- Probar empate de timestamps con UUID como desempate, inserción más nueva entre páginas y poda de una fila terminal entre páginas.
- Confirmar que la ruta offset conserva su respuesta existente y que la UI usa la ruta cursor.
