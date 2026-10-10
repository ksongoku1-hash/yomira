# ADR-0008 — Rutas relativas de biblioteca

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

La biblioteca debe poder moverse y la base de datos no debe quedar acoplada a una ruta absoluta con nombre de usuario o letra de unidad.

## Decision

Guardar rutas relativas a `library_root` cuando sea posible. Resolver y validar rutas en el backend antes de acceder al filesystem.

## Alternatives Considered

- Guardar rutas absolutas: simple, pero frágil al mover la biblioteca.
- Reconstruir todos los nombres en cada consulta: pierde la relación estable con el asset.
- Permitir rutas externas arbitrarias: amplía el riesgo de path traversal y lecturas no autorizadas.

## Positive Consequences

- Portabilidad de biblioteca y reconstrucción más sencilla.
- La raíz funciona como límite claro para resolver activos.

## Negative Consequences

- Cambiar `library_root` exige preflight, comprobación de permisos y reconciliación.
- No todos los assets, como una exportación a USB, viven bajo la raíz de biblioteca.

## Failure Modes

- Ruta relativa que escapa de la raíz, raíz desconectada o archivo movido externamente.

## Reversal Strategy

Migrar con tabla de correspondencia y backup; validar cada ruta resuelta antes de actualizar DB. Rechazar rutas fuera del root hasta aprobación de una política nueva.
