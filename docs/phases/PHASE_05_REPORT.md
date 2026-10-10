# Informe - Fase 5: Biblioteca local

**Estado:** PASS

**Fecha:** 2026-09-26

**Tareas evaluadas:** K049-K058

**Alcance:** servicio y API de biblioteca, tarjetas, detalle, filtros locales, autoridad de metadata, resolución de rutas relativas y gate de escala.

## Resultado de tareas

| Tarea | Estado | Evidencia |
|---|---|---|
| K049 | PASS | `LibraryService` insert-only, consulta por ID y listado paginado; pruebas en `test_library_service.py`. |
| K050 | PASS | Contratos de listado/detalle/overrides y composición del runtime en `test_library_api.py` y `test_lifecycle.py`; ADR-0045 define `data_root`, migración verificada e inyección de servicios al startup. |
| K051 | PASS | FK y triggers limitan portadas a Assets `COVER`; migración y rechazo de referencias inválidas cubiertos por tests. |
| K052 | PASS | Miniaturas WebP deterministas con cache LRU acotado; pruebas en `test_thumbnails.py`. |
| K053 | PASS | Tarjetas, filtros, estados de error, recuperación y paginación en `LibraryPage`; tests de frontend. |
| K054 | PASS | Detalle de serie, navegación y metadata validada en `App.test.tsx`. |
| K055 | PASS | Filtros de lectura derivados de capítulos y progreso local; conteo antes de paginar, ADR-0042. |
| K056 | PASS | Precedencia por campo, conflictos conservados, overrides y migración `0013_metadata_authority`, ADR-0043. |
| K057 | PASS | `LibraryPathMapper` valida raíces y traversal, canonicaliza symlinks/junctions y bloquea escapes; ADR-0044. |
| K058 | PASS | Suite de escala con 100, 1.000 y 10.000 series, 10 capítulos por serie; mediciones abajo. |

## Evidencia de K058

La suite genera datos deterministas en una base SQLite temporal y confirma cada bloque de 500 series. El conjunto de 10.000 series contiene 100.000 capítulos. Se verifican la primera, media y última página; totales de filtros `completed`, `reading` e `incomplete`; búsqueda local dentro de la página devuelta; y consulta de detalle por ID. La UI recibe un total sintético de 10.000 pero solo renderiza una página de 100, permite buscar en esa página, avanza de página y abre el detalle.

Latencias del servicio backend (ms; ejecución local, no promesa de hardware):

| Series / capítulos | Siembra | Página inicial | Página media | Página final | Filtro más lento | Búsqueda local | Detalle |
|---|---:|---:|---:|---:|---:|---:|---:|
| 100 / 1.000 | 21,61 | 11,64 | — (una página) | — (una página) | 6,80 | 0,02 | 1,48 |
| 1.000 / 10.000 | 208,34 | 3,23 | 2,34 | 2,60 | 12,83 | 0,02 | 1,17 |
| 10.000 / 100.000 | 2.560,02 | 3,60 | 5,23 | 7,89 | 109,48 | 0,05 | 1,68 |

La búsqueda de UI sobre los 100 resultados visibles tomó 24,54 ms en Vitest/JSDOM, por debajo del objetivo de 200 ms de la especificación. El mismo test midió 723,63 ms para renderizar la primera página, 1.353,34 ms para cargar otra página y 93,85 ms para abrir un detalle; estos tiempos incluyen React Query, el mock de red, JSDOM y consultas de Testing Library. No representan un benchmark de pintura en un navegador real. La interacción usa paginación, por lo que el DOM queda acotado a 100 series y no monta 10.000 tarjetas simultáneamente.

Durante la primera ejecución de 10.000 series, la siembra de prueba en una transacción prolongada terminó con `SQLite: unable to open database file`. Confirmado el amplio espacio libre y que la base volvía a abrir con `integrity_check=ok`, se cambió el generador de prueba para confirmar lotes de 500 series. El mismo perfil de 10.000 series y 100.000 capítulos pasó; no se modificaron consultas de producción para ocultar el fallo del fixture.

## Verificación

| Verificación | Resultado |
|---|---|
| Backend Pytest | 177 aprobadas. |
| Ruff | PASS. |
| Pyright | 0 errores, warnings ni informations. |
| Frontend Vitest | 28 aprobadas. |
| TypeScript | PASS. |
| ESLint | PASS. |
| Build Vite | PASS; 79 módulos transformados. |
| Smoke loopback | PASS; SPA, API real de biblioteca en DB nueva, liveness, readiness, versión y cierre. |
| Quality gate PowerShell completo | PASS; TypeScript, ESLint, 28 Vitest, build, `pip check`, 177 Pytest, Ruff, Pyright y smoke. Se usó una copia aislada del Python 3.14.7 fijado con `pip==26.2.1` añadido desde un wheel verificado; la instalación original quedó intacta. |
| `git diff --check` | PASS. |

## Límites conocidos

- El smoke inicia la app normal con una raíz aislada y confirma que `GET /api/v1/library` responde 200 tras abrir SQLite y aplicar migraciones. Los tests de render de UI siguen usando respuestas simuladas; no sustituyen una prueba de navegador real con datos persistidos.
- La búsqueda del listado local está limitada a la página cargada. La búsqueda de fuentes remotas y su UI pertenecen a K158-K169.
- K057 verifica contención al resolver la ruta. El servicio que finalmente abra o escriba un archivo debe proteger también la operación frente a cambios concurrentes de symlinks/junctions y aplicar el manejo de bloqueos del SO.
- La instalación original del Python embebido no trae los archivos ejecutables de `pip` aunque el lock fija `pip==26.2.1`; el gate se completó con una copia aislada del mismo runtime y el wheel verificado de esa versión.

## Decisión

K049–K058 pasan y el quality gate completo terminó en `PASS`. El arranque normal abre y migra SQLite antes de readiness; el smoke confirma la API de biblioteca contra esa base. Fase 5 queda aprobada.
