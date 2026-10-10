# Reglas de dependencias

**Estado:** APPROVED — K003 aprobado formalmente por el usuario el 2026-09-25.

## 1. Dirección permitida

```text
React UI → API contracts
API → Application / Job Manager → Domain interfaces
Application services → Domain + declared ports
Adapters (connectors, SQLite, filesystem, HTTP) → declared ports/domain types
```

Las dependencias concretas se inyectan en los límites de aplicación. El dominio no importa React, FastAPI, SQLAlchemy, SQLite, Pillow, `httpx` ni módulos de conectores.

## 2. Matriz permitida

| Origen | Dependencia permitida | Dependencia prohibida |
|---|---|---|
| React | API HTTP y contratos publicados | Rutas locales, DB, SQL, biblioteca, secretos o código de connector |
| API | Schemas, autenticación LAN, application services | HTML parsers, SQL directo disperso, procesamiento de imagen en handlers |
| Job Manager | Dominio, puertos de connector/downloader/processor/CBZ/repository | Parser de fuente concreto, escritura directa de archivos o SQL libre |
| Connector | Contrato, `ConnectorHostContext` y cliente HTTP acotado | Job Manager interno, DB/repositories, rutas, escritura de archivos, `library/`, CBZ builder o export |
| Downloader | HTTP streaming, limits, manifest/repository port | React, ZIP/CBZ final, selección de metadatos |
| Validator/Processor | Bytes locales, límites y Pillow | HTTP, connector, SQLite directa o decisiones de UI |
| CBZ Builder/Verifier | Páginas validadas, ComicInfo schema, filesystem publisher | Connector, API, lógica de UI o estado de job fuera del puerto definido |
| Repository | Dominio y SQLAlchemy/SQLite | Network, React, filesystem arbitrary paths, parsing de fuente |
| Filesystem/Export | Paths ya autorizados, DiskBudgetManager, repositorio | URL remota sin validar, React directo o metadata remota como ruta |
| Logging/Diagnostics | Eventos sanitizables y versiones declaradas | Secret stores en texto plano, contenido de manga o lectura de biblioteca completa |

## 3. Prohibiciones globales

- No hay import circular entre capas ni llamadas ocultas de un módulo a otro.
- Un connector no puede escribir directamente ni emitir SQL arbitrario. El SDK solo le entrega capabilities tipadas; un chequeo AST bloquea imports de persistencia/filesystem y llamadas directas de escritura. Esta frontera aplica a código first-party confiable y no se presenta como sandbox contra plugins hostiles.
- El Processor no hace HTTP; el Downloader no empaqueta CBZ; el Builder no consulta fuentes.
- React nunca maneja filesystem y no puede marcar operaciones críticas como completadas de forma optimista.
- No se usan mensajes humanos como clave de control; se usan tipos, estados y `error_code`.
- No se cambia el estado persistente al margen del Job Manager y sus transiciones válidas.
- Ningún handler llama código bloqueante largo sin una estrategia de recursos explícita.
- Los paths externos y nombres remotos pasan sanitización y resolución bajo una raíz permitida.
- Secrets no pasan a logs ni a Support Bundle.

## 4. Recursos y concurrencia

- Todo pool tiene límite global, por fuente y por host donde corresponda.
- Colas, cachés, precarga del reader, decodificación, logs y buffers tienen presupuesto finito.
- Las escrituras SQLite se centralizan y permanecen breves.
- Cada job declara la etapa y el espacio temporal que necesita; DiskBudgetManager comprueba presupuesto antes del lote.
- Cancelación y cleanup respetan el punto seguro de cada etapa.

## 5. Pruebas y contratos

- Conectores se prueban con fixtures offline; las pruebas de integración de pipeline usan Mock Manga Server.
- Los límites entre capas se prueban con errores y estados, no comparando texto traducido.
- Un cambio de dirección de dependencia requiere ADR y revisión de impacto sobre integridad, recovery, memoria y trazabilidad.
- Cualquier excepción temporal debe declarar owner, límite, condición de retiro y pruebas de regresión.

