# ADR-0120 — Raíz de biblioteca en Documents del perfil

## Status

Accepted — solicitado directamente por el usuario el 2026-10-04. Este ADR sustituye únicamente la ubicación predeterminada de ADR-0114; ADR-0115 continúa gobernando la compatibilidad con AlphaData y los CBZ heredados.

## Context

ADR-0114 usaba la carpeta Documentos resuelta por el Shell de Windows. En esta instalación, Windows redirige esa carpeta a `C:/Users/USER\OneDrive\Documents`, mientras que también existe `C:/Users/USER\Documents`. El usuario no encontraba los CBZ y pidió usar la carpeta `Mangas` dentro de Documentos. Para esta solicitud, la ruta deseada es la carpeta literal `Documents` del perfil del usuario, sin redirección a OneDrive.

## Decision

- El valor predeterminado de `library_root` será `%USERPROFILE%\Documents\Mangas`. Si `USERPROFILE` no está definido, se usa `Path.home()\Documents\Mangas`.
- Yomira no consultará la ruta conocida de Documentos del Shell para este valor predeterminado. `YOMIRA_LIBRARY_ROOT` sigue permitiendo una ruta alternativa absoluta y mantiene las validaciones actuales de volumen y contención.
- `data_root`, SQLite, progreso, trabajos, temporales y backups no cambian.
- El CBZ registrado que estaba bajo la carpeta Documentos redirigida se copió a la misma ruta relativa bajo `%USERPROFILE%\Documents\Mangas`, después de comparar tamaño, SHA-256 y CRC ZIP con el registro existente. La copia final no reemplazó ningún destino y el archivo de origen se conserva.
- Los CBZ heredados registrados como `library/chapters/...` siguen resolviéndose bajo `data_root`, de acuerdo con ADR-0115.
- Una instancia ya abierta conserva la raíz cargada al inicio. El valor predeterminado nuevo se aplica al siguiente inicio normal; esta decisión no autoriza detener la Alpha activa ni activar un candidato.

## Consequences

- Los CBZ nuevos y su staging se publican bajo `%USERPROFILE%\Documents\Mangas`.
- Si Windows redirige la carpeta conocida de Documentos a OneDrive, esa redirección ya no altera el destino de Yomira.
- La ubicación podrá verificarse directamente en el Explorador bajo la carpeta `Documents` del perfil.

## Verification

- La prueba de configuración falló antes del cambio mostrando `OneDrive\Documents\Mangas` y pasó después con `%USERPROFILE%\Documents\Mangas`; `backend/tests/test_settings.py`: 24/24.
- La prueba de arranque aislado `test_normal_startup_serves_empty_library_from_runtime_database` pasó. Ruff y Pyright pasaron para `app/settings.py` y `tests/test_settings.py`.
- Suite completa del backend con CPython 3.14.7, dependencia `comtypes==1.4.17` disponible y `basetemp` corto: 1.615 pasaron, 11 omitidas, 0 fallaron (1.626 recogidas). Una corrida anterior con `basetemp` largo produjo fallos por el límite de rutas de Windows; la misma prueba de descarga pasó al repetirla en `C:\pyfcheck`.
- La base activa pasó `PRAGMA quick_check` antes y después de copiar.
- La copia mide 4.921.886 bytes, coincide con el SHA-256 registrado `2d913c1f9cef9651234f632b393f3bde487941b577e4f58487e525c63af5a457` y pasó el CRC ZIP con 12 miembros. El CBZ original sigue presente con el mismo hash.
- `YOMIRA_LIBRARY_ROOT` quedó guardada en el entorno de usuario como `C:/Users/USER\Documents\Mangas`. La Alpha que ya estaba abierta continúa ejecutándose; la nueva raíz se aplica al siguiente inicio.

## Reversal Strategy

Revertir exige un ADR nuevo. No borrar la copia ni el CBZ de origen al revertir; conservar ambos hasta una migración verificada que revise todos los vínculos SQLite y las posibles colisiones.
