# ADR-0012 — Servicio de assets del lector

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

La UI no debe acceder directamente al filesystem y el lector debe servir páginas sin cargar un capítulo completo en memoria.

## Decision

El backend resolverá y servirá páginas a través del endpoint de lector, con caché limitada y soporte de ETag/Cache-Control; React mantendrá una ventana virtualizada y acotada.

## Alternatives Considered

- Exponer rutas locales a React: viola la frontera UI-filesystem.
- Extraer todas las páginas a memoria: consumo no acotado.
- Extraer una copia permanente completa por cada lectura: duplica almacenamiento.

## Positive Consequences

- Autorización y resolución de rutas permanecen en el backend.
- Se puede limitar memoria y caché según el perfil del dispositivo.

## Negative Consequences

- Abrir ZIP por páginas puede añadir latencia si no se gestiona adecuadamente.
- Cache headers y ETag deben seguir hashes/versiones del asset.

## Failure Modes

- Página inexistente o corrupta, ZIP bloqueado, cache sin límite o request de índice inválido.

## Reversal Strategy

Cambiar el transporte conservando el límite backend-filesystem, acceso por página y presupuesto de memoria. Invalidar caché por versión/hash antes de cambiar formato.
