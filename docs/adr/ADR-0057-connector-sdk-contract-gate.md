# ADR-0057 — Connector SDK Gate y contratos ejecutables

## Status

Accepted for K070 implementation — 2026-09-26.

## Context

K059–K068 definen el contrato y la frontera del SDK; K069 añade un fixture offline. Antes de desarrollar servidores de prueba y conectores de fuente, Fase 6 requiere una verificación ejecutable de los contratos, preservación sin pérdida, errores trazables, allowlist y límites del host.

## Decision

- `backend/tests/test_connector_sdk_gate.py` es la suite contractual del SDK. Pytest la ejecuta como parte del quality gate completo; la suite también cubre la resolución del `MockConnector` por el registry explícito.
- Los tests comprueban compatibilidad estructural estática con `SourceConnector`, metadata/capacidades declaradas, operaciones asíncronas, normalización síncrona, secuencias inmutables y preservación exacta de referencias, orden y duplicados.
- Los fallos comprobados deben usar `SourceConnectorError`, clasificar retryabilidad, incluir traza UUIDv4 y no reflejar referencias ni datos de entrada en el mensaje.
- La resolución mediante registry debe ser lazy y cachear una instancia por ID; `UPDATE_CHECK` sigue bloqueado antes de factory hasta que K187 defina su contrato.
- Los tests de K067 conservan el chequeo AST del paquete para imports de persistencia y escrituras directas. Estos guardrails y el conjunto contractual deben pasar junto con el gate frontend/backend/build/smoke.
- Cada conector first-party futuro debe añadir pruebas equivalentes a esta suite y pasarlas antes de incluirse en el allowlist de producción. No se acepta omitir fallos de contrato mediante skips o excepciones locales.
- El gate valida integraciones first-party confiables in-process; no convierte conectores Python en plugins hostiles aislados por el sistema operativo. Ese límite sigue gobernado por ADR-0054.

## Alternatives Considered

- **Aprobar el SDK solo por tipado estático:** Pyright no verifica pérdida de páginas, errores en runtime, registro lazy ni trazabilidad.
- **Probar solamente MockConnector:** deja sin verificar la composición con registry y la política de capacidad del host.
- **Ejecutar contract tests solo manualmente:** permite que regresiones entren en el mismo pipeline que afirma calidad; Pytest ya es obligatorio en el quality gate.
- **Crear una segunda suite gate independiente:** duplicaría configuración de runtime y podría producir dos definiciones contradictorias de PASS. La suite dedicada forma parte de Pytest y del quality gate único.

## Consequences

- La suite es offline y determinista; no requiere servidor, puerto, red, credenciales ni conectores de producto.
- El resultado del gate depende de los tests contractuales, Pyright, Ruff y el quality gate completo; el reporte de fase conserva los conteos y límites observados.
- Nuevos comportamientos o capacidades requieren actualizar contrato, tests y ADR antes de integrarse.

## Failure Modes

- Incompatibilidad de firma o tipos: Pyright falla el gate.
- Pérdida de orden, duplicados o referencias: un test contractual falla.
- Error no trazado, reintentabilidad equivocada o mensaje con datos recibidos: un test contractual falla.
- Factory ejecutada antes de resolver o por capacidad no permitida: un test de registry falla.
- Acceso connector a DB o escritura directa: el guard AST falla.

## Reversal Strategy

Cambiar el contrato base, exclusiones del gate o garantías de preservación requiere una nueva decisión arquitectónica y pruebas de migración para todo connector ya registrado. Eliminar un guard requiere demostrar una frontera equivalente sin debilitar el aislamiento aprobado en ADR-0054.
