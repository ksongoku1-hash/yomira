# ADR-0114 — Biblioteca de CBZ en Documentos/Mangas

> Compatibilidad del perfil AlphaData y de las rutas CBZ anteriores: ver ADR-0115.

## Status

Superseded by ADR-0120 on 2026-10-04. The original decision was accepted by direct user instruction on 2026-10-04 and is retained here as history.

## Context

Yomira publica actualmente CBZ dentro de `YomiraData/library/chapters`, junto con el árbol operativo de datos. El usuario solicita que los CBZ queden en la carpeta de mangas dentro de Documentos. La raíz operativa debe seguir en `%LOCALAPPDATA%\Yomira\YomiraData` para que SQLite, temporales, logs, backups y diagnósticos no se mezclen con la biblioteca visible.

## Decision

- La raíz predeterminada de CBZ será `Mangas/` dentro de la carpeta Documentos de Windows obtenida del shell. `YOMIRA_LIBRARY_ROOT` puede sustituirla por otra ruta absoluta local.
- La raíz de datos continúa siendo independiente. Páginas temporales, manifests, SQLite, logs y backups permanecen en `YomiraData`.
- El CBZ y su archivo temporal de construcción se crean bajo la raíz de biblioteca, en `.yomira-staging/` y `chapters/`, respectivamente. La publicación final permanece atómica y sin reemplazo dentro del mismo árbol de biblioteca.
- SQLite conserva rutas relativas a la raíz de biblioteca para assets CBZ; el lector las resuelve contra esa raíz. Los assets de portada mantienen su raíz operativa actual.
- Para mantener válida la reserva de disco compartida y la publicación local, ambas raíces deben estar en el mismo volumen fijo o extraíble compatible. El arranque falla cerrado antes de aceptar jobs si la biblioteca no puede crearse, verificarse o no cumple esa restricción.
- Los CBZ ya existentes no se migran en bloque. Una reubicación solicitada de un archivo existente debe verificar el origen y evitar sobrescribir cualquier destino.

## Consequences

- Los CBZ nuevos quedan en `Documentos\Mangas\chapters\<series UUID>\`.
- Si el volumen o la carpeta Documentos no cumple los requisitos, Yomira no inicia jobs y conserva los archivos existentes.
- Cambiar la raíz de biblioteca es una configuración explícita; los paths persistidos siguen siendo relativos y no guardan rutas absolutas del perfil del usuario.

## Reversal Strategy

Revertir requiere ADR nuevo. No se mueven archivos automáticamente al cambiar la configuración; una futura migración debe verificar hash, identidad de archivo, colisiones y todos los vínculos SQLite antes de confirmar cambios.
