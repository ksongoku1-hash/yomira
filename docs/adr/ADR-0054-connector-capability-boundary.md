# ADR-0054 — Frontera de capacidades del SDK de conectores

## Status

Accepted for K067 implementation — 2026-09-26.

## Context

ADR-0006, `DEPENDENCY_RULES.md` y la especificación prohíben que un conector escriba en `library/`, ejecute SQL arbitrario, modifique estados de jobs o acceda a repositorios internos. K059–K066 ya definen el protocolo tipado, los datos de fuente, errores y el único acceso autorizado a credenciales. El SDK aún no tiene un contexto explícito que el host entregue al construir un conector.

El código de connector previsto es código de primera parte, incluido en Yomira; el producto no descarga ni carga plugins Python de terceros. Un objeto Python ejecutado dentro del proceso no puede tratarse como una frontera de seguridad contra código hostil: anotaciones, `Protocol` y objetos de contexto no impiden importaciones globales o llamadas directas a la biblioteca estándar.

## Decision

- K067 define `ConnectorHostContext` como dataclass inmutable y `slots=True`. Su única capacidad inicial es `secret_vault: SecretVault`; no contiene `SQLiteDatabase`, `Session`, repository, `data_root`, `library_root`, `Path`, FileSystem, Job Manager, API interna ni mutadores de biblioteca.
- K068 construirá e inyectará este contexto al crear connectors mediante el registry. Un conector que necesite credenciales recibe referencias `SecretReference` y usa solo `SecretVault`; no conoce el path ni el formato DPAPI.
- El paquete `app.connectors` mantiene la frontera estática: no importa `app.persistence`, `app.services`, `sqlite3`, `sqlalchemy`, `pathlib`, `os`, `shutil` ni `tempfile`; no usa `open` ni métodos de mutación de archivos. Un contract test AST la comprobará en cada gate del SDK.
- El host sigue siendo owner de DB, filesystem, jobs y publicación. Los resultados del conector vuelven como DTOs; solo servicios/repositorios del core pueden persistirlos o crear archivos.
- Este ADR define aislamiento por capacidades y dependencia para código Yomira confiable, no un sandbox de proceso ni una barrera contra código malicioso. No se cargan connectors externos dinámicamente. Añadir plugins de terceros requiere antes otro ADR y aislamiento de SO, proceso, filesystem y red con pruebas negativas; K067 no implementa ni promete ese límite.

## Alternatives Considered

- **Dar `SQLiteDatabase`/repositories al connector y confiar en convención:** permite SQL libre y persistencia parcial, contradice ADR-0006 y hace difícil comprobar el límite.
- **Prohibir imports del proceso con un custom importer:** Python mantiene múltiples vías de acceso a builtins y módulos cargados; una sustitución incompleta produciría una falsa garantía de seguridad.
- **Ejecutar connectors actuales en procesos separados:** requiere contratos IPC, protocolo de ciclo de vida, límites de proceso y manejo de red que no están definidos en K067 ni son necesarios para código first-party confiable; elevaría sin evidencia memoria, latencia y superficies de fallo.
- **Aceptar plugins Python de terceros en proceso:** no ofrece confinamiento de sistema operativo y queda expresamente fuera del alcance.

## Consequences

- Pyright y las pruebas de contrato verifican qué capability expone el host; AST guard evita que el SDK incorpore acceso directo a persistencia o archivos.
- El connector puede usar la interfaz de secreto ya aprobada, pero no obtener tokens directamente desde settings o DPAPI.
- El chequeo estático no prueba aislamiento frente a imports dinámicos, reflection, C extensions ni código malicioso. Solo Yomira first-party puede implementarse bajo esta frontera.
- Nuevas capacidades requieren un puerto tipado, una justificación en ADR y pruebas que demuestren que no añaden DB raw ni escritura directa.

## Failure Modes

- `ConnectorHostContext` incorpora DB, Session, rutas o APIs mutables: contract test debe fallar antes del merge.
- El paquete connector importa un módulo persistente o filesystem o llama una operación de escritura directa: AST contract test falla.
- Se interpreta la frontera como sandbox contra plugin hostil: bloquear la carga; no habilitarla hasta contar con aislamiento de proceso aprobado.
- Una fuente solicita persistencia o publicación local: devolver datos al core y dejar que el owner de dominio ejecute la operación; no crear un fallback en el connector.

## Reversal Strategy

Ampliar las capabilities de `ConnectorHostContext` requiere ADR, tests estáticos y contract tests del registry. Retirar la interfaz del Vault exige migrar conectores y conservar intactos los registros cifrados. Introducir código no confiable requiere una arquitectura de aislamiento de proceso independiente, no un flag o import filter añadido a este SDK.
