# ADR-0102 — Revalidación con punto de montaje capturado

## Estado

Aceptado — 2026-09-28.

## Contexto

El gate K116 no termina ni siquiera el perfil loopback acotado de 2 jobs × 2 páginas. El perfil de prueba del 2026-09-28 contó 55 270 llamadas a `DataRootIdentity.assert_current`; cada una volvía a ejecutar `GetVolumePathNameW`, `GetDriveTypeW` y `GetFileInformationByHandleEx(FileIdInfo)`. `GetVolumePathNameW` acumuló 114,25 s de tiempo inclusivo en los workers; esas duraciones se solapan y no equivalen a tiempo de pared.

ADR-0078 exige comprobar la raíz antes de cada operación de filesystem y rechazar un cambio de identidad o un volumen no soportado. No requiere redescubrir el punto de montaje en cada comprobación. La identidad física debe seguir consultándose en cada comprobación.

## Decisión

`DataRootIdentity.capture` obtiene y conserva el punto de montaje devuelto por `GetVolumePathNameW` junto con el `VolumeSerialNumber` y `FileId` originales. `assert_current` conserva la resolución de `canonical_path`, consulta `GetDriveTypeW` usando el punto de montaje capturado y vuelve a obtener el `VolumeSerialNumber` y `FileId` de la ruta resuelta. Solo permite continuar si el tipo sigue siendo `DRIVE_FIXED` o `DRIVE_REMOVABLE` y el par físico coincide exactamente con el capturado. Cualquier error, tipo no admitido o identidad distinta invalida el objeto compartido y falla cerrado.

El punto de montaje es solo una cadena inmutable de consulta; no se cachean ni el tipo actual ni la identidad física. `GetVolumePathNameW` sigue siendo obligatorio al capturar la identidad y al validar por adelantado un volumen para crear una raíz ausente. No cambia el límite de una comprobación de identidad por operación filesystem exigido por ADR-0078.

## Alternativas consideradas

- **Repetir `GetVolumePathNameW` en cada comprobación:** conserva la conducta actual, pero la medición confirma que añade una llamada lenta redundante por operación.
- **Cachear el tipo de unidad o el `VolumeSerialNumber`/`FileId`:** rechazado; permitiría que el runtime siguiera usando una raíz después de cambiar el volumen o la clasificación admitida.
- **Omitir `GetDriveTypeW` durante la sesión:** rechazado; el tipo actual debe seguir siendo fijo o removible.

## Consecuencias y riesgos

- Se elimina una llamada de descubrimiento de montaje por comprobación, pero se mantienen el chequeo actual del tipo y la lectura actual de identidad física.
- Una ruta de montaje que desaparezca o pase a un tipo no admitido falla cerrado. Un volumen nuevo en la misma ruta debe coincidir además con el serial y el identificador de archivo originales; de lo contrario, se invalida el runtime.
- La prueba estructural verifica el número de llamadas; K116 todavía debe demostrar finalización, integridad de páginas/CBZ y límites sostenidos de recursos después del cambio.

## Verificación requerida

- La prueba de regresión exige una llamada a `GetVolumePathNameW` durante `capture`, cero llamadas posteriores durante `assert_current`, y llamadas a `GetDriveTypeW` y `GetFileInformationByHandleEx` en cada comprobación estable.
- Un tipo no admitido, un serial distinto o un `FileId` distinto deben fallar cerrado y dejar la instancia invalidada.
- Mantener verdes los tests existentes de cambio/reparse de raíz de K093; repetir K116 y completar el post-run de integridad en modo de solo lectura.

## Referencias

- [Microsoft Learn: GetVolumePathNameW](https://learn.microsoft.com/en-us/windows/desktop/api/fileapi/nf-fileapi-getvolumepathnamew)
- [Microsoft Learn: GetDriveTypeW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdrivetypew)
- [Microsoft Learn: FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)
- ADR-0078 — Atomic job manifest persistence.
