# ADR-0111 — Ampliación acotada de búsqueda del catálogo LectorXD

- Estado: ACCEPTED bajo la autorización previa del usuario para corregir el flujo de búsqueda aunque requiriera actualizar el plan técnico.
- Fecha: 2026-10-03 (America/Bogota).
- Reemplaza parcialmente: ADR-0110, únicamente en el presupuesto de páginas de búsqueda.

## Contexto

La Alpha consulta tres páginas al buscar por título. El usuario reportó que la búsqueda de «Mato Seihei» no devolvió resultados. No se dispone de una URL de ficha confirmada ni se comprobó el catálogo en vivo, por lo que no se concluye que el título esté o no esté publicado en LectorXD. La búsqueda admite título literal normalizado; no realiza coincidencia difusa ni convierte errores ortográficos. La grafía correcta del título es «Mato Seihei no Slave» / «Chained Soldier» según la lista pública de derechos de Shueisha, pero esto no demuestra su presencia en LectorXD.

La referencia pública del adaptador de HaruNeko enumera el catálogo paginado `/catalogo?page={page}`. El usuario canceló la comprobación por navegador; en consecuencia, ninguna implementación o verificación de este ADR solicita LectorXD ni sus CDNs por otra herramienta o ruta indirecta.

## Decisión

- La búsqueda rápida conserva el presupuesto de tres páginas para no cambiar su coste habitual.
- La interfaz ofrece una ampliación expresa cuyo presupuesto máximo es veinte páginas consecutivas, comenzando por la primera. El límite se pasa como `page_budget=20`; los valores aceptados por contrato son enteros de 1 a 20. El formulario ofrece solo las opciones rápida (3) y ampliada (20).
- La enumeración sigue siendo secuencial y mantiene el comportamiento de detenerse ante una página válida sin entradas. La búsqueda devuelve como máximo 100 identidades únicas. No se cambia la normalización ni el criterio literal de coincidencia.
- El presupuesto ampliado solo afecta al escaneo de catálogos paginados. Una URL de ficha válida sigue resolviéndose con una sola petición directa. Los conectores que no usan paginación pueden ignorar el campo.
- La UI declara el límite y distingue el resultado vacío de una afirmación de ausencia en el catálogo completo. «Sin resultados» solo describe las páginas que alcanzó esa solicitud; una ficha conocida puede pegarse directamente.

## Límites y privacidad

- Cada página continúa sujeta al máximo existente de 4 MiB, al parser acotado y a las políticas vigentes de TLS, redirects, timeouts, concurrencia y allowlist. Por tanto, una ampliación puede recibir como máximo 80 MiB de cuerpos HTML en total; las respuestas se solicitan y analizan de una en una.
- No se guarda el catálogo o la query en disco. Los logs mantienen campos sanitizados y no registran query, URL, título remoto, body ni cookies. No se añaden cookies, credenciales, browser runtime, POST, reintentos, CDN u orígenes nuevos.
- PAGE_LIST continúa deshabilitado para LectorXD. Esta decisión no habilita ni prueba descargas.

## Consecuencias

Una serie posterior a las tres páginas iniciales puede aparecer al elegir la ampliación, con una cota explícita de peticiones y bytes. La consulta puede ser más lenta y, si alcanza el máximo, todavía omitir títulos más adelante. Si el usuario conoce la URL pública de la ficha, la resolución directa sigue siendo el camino de una petición.

La validación automática usa fixtures locales sintéticos con el único host LectorXD representado por el parser; no genera tráfico de red a ese host. La presencia real de «Mato Seihei no Slave» en LectorXD queda sin verificar hasta que el usuario pruebe manualmente la Alpha o proporcione el enlace de ficha.
