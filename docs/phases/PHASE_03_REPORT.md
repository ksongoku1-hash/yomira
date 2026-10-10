# Informe — Fase 3: Backend Core

**Estado:** PASS  
**Fecha:** 2026-09-25  
**Tareas:** K023–K033  
**Alcance:** núcleo de API, configuración y ciclo de vida; no incluye base de datos ni funciones de producto.

## Resumen de tareas

| Tarea | Estado | Resultado |
|---|---|---|
| K023 — Application Factory | PASS | Factoría FastAPI separada del punto de entrada; rutas registradas por versión. |
| K024 — Configuración tipada | PASS | Settings validados para entorno, host loopback, puerto y nivel de log. |
| K025 — Entornos | PASS | Dev, test y prod explícitos; documentación API deshabilitada fuera de dev; sin lectura de `.env`. |
| K026 — Liveness y readiness | PASS | Liveness informa que el proceso responde; readiness solo pasa tras startup y vuelve a no listo al cerrar. |
| K027 — Errores HTTP | PASS | Sobre estable, detalles de validación sanitizados y errores internos genéricos; contrato en ADR-0022. |
| K028 — Correlación | PASS | UUID canónico de `X-Trace-ID` propagado a respuesta y errores; UUIDv4 generado cuando falta o no es válido. |
| K029 — Logging estructurado | PASS | Eventos JSON a stderr con timestamp UTC y contexto acotado; sin texto de excepción ni rutas completas. |
| K030 — Startup | PASS | Inicialización controlada; readiness no se publica antes de adquirir recursos de runtime. |
| K031 — Shutdown | PASS | Readiness se desactiva antes de liberar recursos; fallos quedan registrados. |
| K032 — Instancia única | PASS | Mutex Windows por sesión; un segundo proceso no asume propiedad del backend. |
| K033 — Backend Core Gate | PASS | Ciclos de cierre, pruebas de errores y smoke de UI/API integrados al gate completo. |

## Verificación final

El gate completo se ejecutó con los runtimes fijados en `runtime-versions.json` y terminó con `QUALITY GATE: PASS`.

| Verificación | Resultado |
|---|---|
| Python, Node y npm | Versiones exactas aprobadas: Python 3.14.7, Node 24.21.0 y npm 11.19.0. |
| Dependencias frontend | `npm ls --depth=0` sin dependencias faltantes. |
| TypeScript y ESLint | PASS. |
| Vitest | 8 pruebas aprobadas. |
| Build Vite | PASS. |
| `pip check` | Sin dependencias Python rotas. |
| Pytest | 38 pruebas aprobadas. |
| Ruff | PASS. |
| Pyright | 0 errores, 0 warnings y 0 informations. |
| Ciclo de vida | 10 ciclos de startup/shutdown consecutivos liberan readiness y el mutex. |
| Mutex Windows | Segundo proceso rechazado; al terminar forzadamente el propietario, un proceso nuevo puede adquirir el mutex. |
| Smoke de integración | Frontend y API en loopback; raíz, fallback de biblioteca, liveness, readiness, versión y `X-Trace-ID` verificados; procesos iniciados cerrados al terminar. |

## Fallos reproducidos y corregidos

1. La primera prueba del handler de validación devolvía 500 porque se pidió a Pydantic un argumento `include_url` que `RequestValidationError.errors()` no admite. Se reprodujo con una petición inválida; se corrigió la extracción del error y la prueba confirma una respuesta 422 con detalles sanitizados.
2. El test de propiedad del mutex lanzaba un Python embebible en otro proceso. Su `sys.path` no incluía automáticamente el backend del proyecto. Se reprodujo el fallo de importación, se añadió la raíz del backend al proceso hijo y la prueba de terminación/readquisición pasó.
3. El smoke necesitaba un entorno determinista para no heredar valores YOMIRA del proceso padre. Se fijaron valores locales de test y se restauran al finalizar; ahora verifica también readiness y la cabecera `X-Trace-ID`.

## Límites que siguen vigentes

- El servicio solo escucha en loopback; LAN permanece desactivada.
- No se creó SQLite, repositorio, migración, dominio persistente, conector, almacén de secretos ni funcionalidad de biblioteca/descarga/lector.
- El backend aún no procesa tareas de usuario; el ciclo de vida verificado cubre únicamente el esqueleto de runtime.

## Decisión de fase

K023–K033 cumplen sus criterios y el gate completo pasa. Fase 3 queda `PASS`; Fase 4 puede iniciar con sus contratos de persistencia revisados y cualquier nueva decisión de comportamiento registrada en ADR antes de implementarse.
