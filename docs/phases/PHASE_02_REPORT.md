# Informe — Fase 2: Tooling y esqueleto ejecutable

**Estado:** PASS
**Fecha:** 2026-09-25
**Tareas:** K014–K022
**Alcance:** base ejecutable React + FastAPI, sin lógica de manga.

## Resumen de tareas

| Tarea | Estado | Resultado |
|---|---|---|
| K014 — Estructura física | PASS | Carpetas del proyecto, reglas de formato y exclusiones locales de Git. |
| K015 — Runtimes | PASS | Python 3.14.7, Node 24.21.0 LTS y npm 11.19.0 fijados; package locks y hashes oficiales registrados. |
| K016 — Tooling backend | PASS | Ruff, Pyright estricto, Pytest y dependencias fijadas en requirements.lock. |
| K017 — FastAPI mínimo | PASS | Liveness y versión con schemas declarados y pruebas HTTP ASGI. |
| K018 — Tooling frontend | PASS | React, TypeScript estricto, Vite, Router, Query, Zustand, CSS Modules, ESLint, Vitest y package-lock. |
| K019 — App Shell | PASS | Tema oscuro, seis rutas, navegación teclado/touch y menú compacto accesible; páginas aún provisionales. |
| K020 — API Client | PASS | Único cliente fetch, URL relativa /api/v1, validación JSON, errores tipados, trace_id y comprobación de API_VERSION. |
| K021 — Logging y errores base | PASS | Enum y modelo del sobre API con cuatro códigos iniciales; ADR-0021 agrega la familia API_. |
| K022 — Unified Quality Gate | PASS | Gate reproducible para frontend, backend, integración HTTP y cierre controlado de procesos. |

## Verificación final

El gate completo se ejecutó con las versiones fijadas en runtime-versions.json y terminó con PHASE 2 QUALITY GATE: PASS.

| Verificación | Resultado |
|---|---|
| Versiones de Python, Node y npm | Coinciden exactamente con las fijadas. |
| Dependencias frontend | npm ls --depth=0 sin dependencias faltantes. |
| TypeScript | PASS. |
| ESLint | PASS. |
| Vitest | 8 pruebas aprobadas. |
| Build Vite | PASS; assets de producción generados. |
| pip check | No hay dependencias Python rotas. |
| Pytest | 7 pruebas aprobadas. |
| Ruff | PASS. |
| Pyright | 0 errores, 0 warnings, 0 informations. |
| Smoke de integración | FastAPI y Vite iniciaron en 127.0.0.1; la página raíz, /library y las rutas de API respondieron. Los dos procesos iniciados por el smoke se cerraron al finalizar. |
| npm audit de K018 | 0 vulnerabilidades reportadas en la verificación inicial de dependencias. |

## Fallos reproducidos y correcciones

1. El primer typecheck de K019 detectó que un nombre CSS Module podía tiparse como undefined con exactOptionalPropertyTypes. Se garantizó que className siempre reciba una cadena y el typecheck pasó.
2. Pyright detectó que el diccionario anidado del test no satisface el tipo estático del modelo Pydantic. El test ahora construye ApiErrorBody explícitamente y Pyright pasó.
3. La primera ejecución del smoke alcanzó la API, pero el script intentó asignar a la variable local home. PowerShell compara nombres de variables sin distinguir mayúsculas y HOME es de solo lectura. Se cambió el nombre local a homeResponse; la ejecución integral posterior pasó. Los logs de aquella ejecución fallida se conservaron en el directorio temporal indicado por el warning.

## Límites que siguen vigentes

- Las seis páginas muestran placeholders; no hay funciones de biblioteca, búsqueda, descargas ni lectura.
- No se creó base de datos, conector, secreto ni contenido persistente.
- Los modelos de error aún no se conectan a middleware; K027 corresponde a Fase 3.
- Liveness no pretende representar readiness; K026 definirá esa distinción.
- El smoke prueba integración local mediante loopback, no acceso LAN.

## Decisión de fase

K014–K022 cumplen sus criterios. Fase 2 queda PASS y habilita K023–K033, sujeto al mismo flujo de calidad y checkpoint antes de avanzar.
