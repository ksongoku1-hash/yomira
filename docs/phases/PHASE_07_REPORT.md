# Informe — Fase 7: Mock Manga Server

**Estado:** PASS
**Fecha:** 2026-09-26
**Tareas:** K071–K080
**Alcance:** servidor HTTP de test offline y limitado, fixture normal, inyección determinista de fallos de transporte/contenido y Mock Gate.

## Resultado de tareas

| Tarea | Estado | Resultado |
|---|---|---|
| K071 | PASS | Servidor HTTP de test solo en `127.0.0.1`, puerto efímero, cuatro workers máximos, start/stop explícitos e idempotencia de cierre; ADR-0058. |
| K072 | PASS | Search, metadata, capítulos, referencias de páginas y PNG 2×2 normal, con longitud y headers verificados; ADR-0059. |
| K073 | PASS | Delay fijo de 200 ms, respuesta timeout a 800 ms, recuperación del worker y medición de concurrencia limitada a cuatro; ADR-0060. |
| K074 | PASS | HTTP 404, 429 con `Retry-After: 1` y 500; cuerpos genéricos sin query reflejada y health disponible; ADR-0061. |
| K075 | PASS | Respuesta HTTP 200 que anuncia el PNG completo y transmite la mitad; cliente recibe `IncompleteRead`, worker se libera y health recupera; ADR-0062. |
| K076 | PASS | HTML con MIME de JPEG y JPEG truncado se rechazan al cargar con Pillow; ADR-0063. |
| K077 | PASS | PNG válido 2048×2048 generado en streaming y PNG bomb 20000×20000 rechazado al inspeccionar la cabecera antes de decodificar; ADR-0064. |
| K078 | PASS | JSON 200 con tipos de metadata inválidos y `page_count` inconsistente frente a dos referencias; ADR-0065. |
| K079 | PASS | Secuencia `page-1`, `page-2`, `page-1` preserva orden y repetición; `page_count=3`; ADR-0066. |
| K080 | PASS | Matriz verifica el conjunto exacto de 14 escenarios y compara snapshots entre dos instancias nuevas; ADR-0067. |

## Evidencia de seguridad y recursos

- El fixture no usa Internet, no recibe un host o puerto externo configurable, no se inicia desde la aplicación y no escribe datos de usuario.
- El pool y backlog de requests están limitados; los delays son constantes y finitos.
- La respuesta de image bomb transmite menos de 1 KiB y no crea el raster de 400 millones de píxeles. La imagen grande normal transmite menos de 128 KiB y genera filas bajo demanda.
- Status, cabeceras de integridad, body, MIME, referencias repetidas y resultados de timeout/truncamiento quedan cubiertos por tests de transporte.

## Verificación final

El quality gate completo, con runtimes fijados, terminó con `QUALITY GATE: PASS`.

| Verificación | Resultado |
|---|---|
| Python / SQLite / Node / npm | Python 3.14.7 / SQLite 3.50.4 / Node 24.21.0 / npm 11.19.0. |
| Dependencias | `npm ls --depth=0` y `pip check`: PASS. |
| TypeScript / ESLint | PASS. |
| Vitest | 28 aprobadas. |
| Build frontend | Vite PASS; 79 módulos transformados. |
| Pytest | 309 aprobadas. |
| Ruff | PASS. |
| Pyright | 0 errores, 0 warnings y 0 informations. |
| Smoke integración | PASS; root, `/library`, API y endpoints de sistema en loopback y cierre controlado. |
| `git diff --check` | PASS antes del commit de K080. |

Pytest informó un `PytestCacheWarning` no bloqueante al actualizar `.pytest_cache` por una carpeta preexistente con el mismo nombre. La suite terminó sin errores.

## Fallos reproducidos y resolución

Las pruebas rojas iniciales confirmaron la ausencia de cada modo antes de su implementación. K080 volvió a ejecutarlas desde instancias independientes y verificó resultados repetibles. Una invocación manual de Pyright usó una ruta inexistente del intérprete fijado y no se contó como evidencia; se corrigió la ruta, Pyright obtuvo cero errores y el quality gate completo repitió la verificación con PASS.

## Límites vigentes

- El Mock Manga Server genera estímulos de test, no descarga fuentes reales ni implementa retry/circuit breaker de producto.
- Las tareas de producto pendientes incluyen Job Manager, Download Engine, validación de imágenes, procesamiento SAFE, empaquetado CBZ/ComicInfo, conectores reales, búsqueda remota, lector y conversiones.
- Los estímulos de K077 no fijan `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, `MAX_IMAGE_WIDTH` ni `MAX_IMAGE_HEIGHT`; esos límites requieren decisión y validación en Fase 10.

## Decisión de fase

K071–K080 cumplen sus criterios y el quality gate completo pasa. Fase 7 queda `PASS`. Puede iniciar Fase 8 con el Job Manager y conservar escenarios locales reproducibles para las siguientes pruebas de resiliencia.
