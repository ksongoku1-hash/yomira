# ADR-0051 — Contrato de referencias de página de Source Connector

## Status

Accepted for K064 implementation — 2026-09-26.

## Context

K064 define qué devuelve `get_pages` para que el Downloader reciba referencias sin perder su orden. La especificación nombra `get_pages()` y el manifest registra `ordinal` y `remote_reference`, pero no define URL obligatoria, esquema, extracción de ordinal ni deduplicación. ADR-0035 establece que el ordinal persistido es base uno y el `remote_reference` es texto opaco no vacío.

## Decision

- Cada elemento devuelto por `get_pages` es un `ConnectorPageReference` inmutable con un único campo `remote_reference: str`.
- La referencia debe ser una string no vacía y se conserva exactamente. Puede ser URL absoluta/relativa o un ID opaco; K064 no analiza, valida esquema, recorta ni transforma su contenido.
- `SourceConnector.get_pages` devuelve `Sequence[ConnectorPageReference]`. El orden de la secuencia es el orden de páginas; el core asigna ordinales base uno por posición al crear filas `DownloadPage`, conforme a ADR-0035.
- Se conservan referencias repetidas y una secuencia vacía; la especificación no autoriza deduplicar páginas ni inventar un mínimo. La capa posterior decide cómo reportar un capítulo sin páginas.
- El DTO no contiene bytes, paths locales, headers ni credenciales. Los tokens incluidos en la referencia remota no deben enviarse a logs o Support Bundle; su transporte y sanitización corresponden a las capas de red y diagnóstico.
- Fixtures contractuales son locales. K064 no ejecuta HTTP ni crea filas persistentes.

## Alternatives Considered

- **Aceptar solo URL absoluta HTTP(S):** descarta fuentes que usan IDs o rutas relativas que el adapter resuelve.
- **Guardar la referencia como string suelta en la interfaz:** es compatible, pero no distingue estáticamente una referencia de otro texto y no ofrece un punto de validación inmutable.
- **Derivar ordinal desde URL/nombre de archivo:** cambia la identidad y el orden de fuentes cuyo ID remoto no contiene una secuencia estable.
- **Deduplicar referencias iguales:** puede eliminar páginas repetidas que la fuente publica deliberadamente.
- **Agregar headers o cookies al DTO:** mezcla credenciales con datos de página y amplía superficies de fuga; los secretos pertenecen al Vault y al cliente HTTP.

## Consequences

- El downloader recibe una referencia lossless en secuencia de lectura y persiste el ordinal local por posición, no por contenido remoto.
- IDs de página y URLs permanecen opacos hasta que el cliente de red aplique reglas de origen, autenticación y seguridad.
- Los duplicados se conservan para que el conteo del manifest refleje exactamente la respuesta del connector.

## Failure Modes

- Referencia vacía: se rechaza antes de crear un `DownloadPage`.
- Orden remoto alterado o elementos deduplicados: cambia el contenido y el orden publicado; el adapter debe respetar la secuencia original.
- Se abre una referencia como path local o se usa URL sin validar en HTTP: viola el límite de connector/downloader; K064 no autoriza I/O.
- Referencia con token se escribe en log o bundle: debe sanitizarse en el propietario de diagnóstico; el DTO no es una autorización para registrarla.

## Reversal Strategy

Cambiar la representación, tratamiento de orden o duplicados requiere ADR nuevo y fixtures que comparen secuencias completas. No renumerar ni eliminar páginas persistidas durante una conversión.
