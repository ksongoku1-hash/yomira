# ADR-0138 — Reapertura segura del lector online

## Estado

Accepted, 2026-10-11. Complementa ADR-0135 y ADR-0137. Incremento de recuperación; no declara completada la Fase 5.

## Decisión

El botón existente «Reintentar página» reabre el capítulo online mediante el endpoint de sesión existente, con `restart: true`. El reintento local conserva su recorrido. Cada acción del usuario inicia una reapertura, sin reintentos automáticos de la consulta de apertura, sin cambiar de fuente y sin crear trabajos de descarga.

La posición actual permanece en el componente durante la reapertura. La consulta bloquea páginas y progreso mientras adquiere el plan. La caché anterior se vacía y libera sus blobs y solicitudes; las páginas se vuelven a validar por el transporte y el decodificador existentes. El progreso pendiente solo se guarda tras decodificar la página de la nueva sesión. El enlace para volver a la serie conserva su destino a partir de la metadata previa, incluso durante la espera o un fallo.

La reapertura debe conservar la identidad canónica de la serie y la cantidad de páginas del capítulo observado. Ante diferencias se muestra `READER_CHAPTER_CHANGED` y no se solicita ni guarda una página de ese plan. La persona puede volver a la ficha para revisar el capítulo. El límite de memoria, los controles de origen, los secretos del puente, las comprobaciones de imágenes y el pipeline CBZ no cambian.

## Verificación

Referencia: 16 pruebas de experiencia y 11 del motor. Resultado final: 157 pruebas en cuatro archivos de interfaz y 12 del motor; TypeScript, ESLint, Ruff y Pyright sobre el archivo de pruebas del motor pasan. Se verifican sesión perdida, espera sin solicitudes prematuras, liberación de caché, decodificación antes de guardar, fallo de reapertura, nuevos reintentos explícitos, aumento/disminución de páginas, serie discordante, recorrido local y respuesta después de abandonar el lector.

Una prueba en navegador real con biblioteca sintética demuestra: plan perdido y progreso anterior intacto; reapertura en la misma página con nueva sesión; cambio de tres a dos páginas bloqueado y progreso conservado. En las tres comprobaciones hay cero trabajos y cero archivos de páginas o CBZ. La prueba del motor vuelve a exigir imagen verificada antes de aceptar progreso tras la reapertura.

## Límites

La comprobación de cantidad no detecta una reordenación de imágenes que mantenga el mismo número de páginas. El proveedor no ofrece una identidad estable del contenido entre planes; no se afirma que esa identidad haya sido certificada. La prueba sintética simula pérdida del plan, no un reinicio real del proceso nativo ni disponibilidad de fuentes externas. La reapertura requiere que el proveedor acepte cerrar o reconocer ausente la sesión anterior; un fallo de liberación sigue bloqueando la operación por integridad.

La aplicación activa conserva el backend 0.1.12 y recibe únicamente assets de interfaz, con copia previa y publicación atómica del índice. No hay migraciones ni instalador nuevo para este incremento. `H4b` identifica este parche local de interfaz, no una fase completada.
