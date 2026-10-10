# ADR-0127: Excluir del puente la extensión MangaDex con búsqueda irrelevante

## Estado

Accepted — verificación local de la integración el 2026-10-05.

## Contexto

ADR-0126 habilitó MangaDex, NovelCool y ManhwaWeb mediante Suwayomi. La búsqueda real de `Mato Seihei no Slave` en MangaDex ES devolvió dos títulos ajenos a la consulta, repetidos también en otros idiomas. Las búsquedas de NovelCool ES y ManhwaWeb ES sí devolvieron el título buscado.

Yomira ya registra `yomira.metadata.mangadex` para búsqueda/metadatos, separado del puente de extensiones.

## Decisión

1. Desinstalar `eu.kanade.tachiyomi.extension.all.mangadex` de la instancia Suwayomi de Yomira para impedir que resultados irrelevantes ocupen la búsqueda agregada.
2. Mantener instaladas las extensiones de NovelCool y ManhwaWeb, cuyos resultados coincidieron con la consulta de prueba.
3. Conservar `yomira.metadata.mangadex`; esta decisión solo excluye la extensión MangaDex de Suwayomi.
4. Reconsiderar la extensión si una prueba futura confirma búsqueda pertinente en MangaDex para títulos en español.

## Consecuencias

- El conector agregado consulta menos fuentes y evita resultados MangaDex no relacionados.
- La búsqueda/metadatos directos de MangaDex permanece disponible por su conector propio.

## Verificación

- Confirmar que MangaDex deja de figurar entre las fuentes instaladas de Suwayomi.
- Confirmar que NovelCool y ManhwaWeb permanecen y devuelven resultados relevantes para `Mato Seihei no Slave`.
- Confirmar que la búsqueda de Yomira responde sin error de conexión al servidor local.
