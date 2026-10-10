# ADR-0049 — Contrato de metadata de serie de Source Connector

## Status

Accepted for K062 implementation — 2026-09-26.

## Context

K062 debe definir la salida común de metadata de serie sin mezclarla con metadata local ni con la referencia remota. ADR-0043 define siete campos de fuente: título, URL de portada, autor, descripción, géneros, estado e idioma. `SeriesSource` ya persiste esos campos como valores opcionales y conserva texto y géneros sin aplicar autoridad local. La especificación no fija un formato de idioma/estado ni una normalización común.

## Decision

- `ConnectorSeriesMetadata` es un valor inmutable con los siete campos compartidos: `title`, `cover_url`, `author`, `description`, `genres`, `status` y `language`.
- Cada campo es opcional; `None` significa que la fuente no proporcionó ese campo. Una cadena vacía o una tupla vacía es un valor presente y se conserva distinta de la ausencia.
- Los seis campos escalares son `str | None`. `genres` es `tuple[str, ...] | None`; se conserva exactamente el orden y los duplicados de género.
- No se recortan ni normalizan strings, no se parsean estados ni idiomas, no se resuelve autoridad y no se promociona una URL de portada a Asset local. La cover URL sigue siendo un dato de fuente no confiable.
- `SourceConnector.get_series` devuelve el payload crudo tipado como `RawMetadataT`; `normalize_metadata` lo convierte de forma local y síncrona a `ConnectorSeriesMetadata`. Así, I/O remoto y normalización siguen siendo etapas separadas.
- El adaptador que persista los siete campos en `SeriesSource` pertenece al servicio/capa de aplicación; K062 no añade escrituras, DB ni cambios de autoridad.
- Pruebas de contrato usan fixtures locales sin red y validan tipos, valores ausentes/vacíos y conservación exacta.

## Alternatives Considered

- **Usar directamente `Series` o `SeriesSource`:** mezcla metadata remota con UUIDs, campos locales, identidad de asociación y timestamps que son responsabilidad del core.
- **Requerir un título o todos los campos:** algunas fuentes omiten atributos; rechazarían metadata parcial que el dominio ya admite.
- **Normalizar idiomas, estados, géneros o URLs:** la especificación no declara gramáticas compartidas y una transformación podría perder la forma original.
- **Convertir una cover URL en portada local:** contradice ADR-0043; solo un Asset `COVER` validado puede asignarse como portada local.
- **Usar listas mutables para géneros:** permitiría modificar metadata después de validarla y cambiar su significado sin una nueva observación.

## Consequences

- Connectors heterogéneos entregan metadata en un esquema común que el core puede validar y presentar.
- El core mantiene separadas procedencia y autoridad; las decisiones locales del usuario no cambian por una respuesta remota.
- Las cadenas vacías y los géneros repetidos requieren conservarse deliberadamente al mapear al dominio.
- Los formatos opcionales que no aparecen en ADR-0043 necesitan una decisión antes de incorporarse al modelo común.

## Failure Modes

- Tipo escalar incompatible o géneros que no sean una tupla de strings: se rechazan antes de cruzar al core.
- Campo remoto ausente: se conserva como `None`; no se fabrica un valor de presentación ni un default de producto.
- Cover URL inválida o maliciosa: permanece dato remoto sin acceso filesystem; un flujo posterior debe validarla antes de usarla.
- Se sobrescriben valores locales o overrides al aplicar metadata: incumple ADR-0043; la capa de persistencia debe actualizar solo la autoridad de fuente.

## Reversal Strategy

Agregar, renombrar o normalizar campos requiere un ADR nuevo, fixtures compatibles y una migración protegida si afecta persistencia. Los valores existentes de `SeriesSource` no se reescriben ni se promueven a metadata local durante la transición.
