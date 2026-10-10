# ADR-0010 — Local-first y LAN opt-in

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

La biblioteca y DB pertenecen al PC local. El acceso desde móvil o e-reader es útil, pero una instancia abierta en red amplía la superficie de ataque.

## Decision

Iniciar enlazado a `127.0.0.1`. LAN permanece desactivada hasta habilitación explícita; al activarla exige puerto, autenticación local con sesión temporal y rate limit, y el firewall solo se solicita entonces.

## Alternatives Considered

- Escuchar en LAN por defecto: expone una instancia sin decisión explícita.
- Servidor público o nube: fuera del alcance V1.
- No soportar LAN: impide lectura opcional en dispositivos de red local.

## Positive Consequences

- Operación principal no depende de Internet.
- Exposición de red es opt-in y requiere controles explícitos.

## Negative Consequences

- El usuario debe habilitar y configurar LAN para dispositivos externos.
- Sesiones, puerto ocupado y firewall requieren diagnósticos claros.

## Failure Modes

- Bind accidental a interfaz externa, PIN sometido a fuerza bruta, sesión reutilizada o puerto ocupado sin aviso.

## Reversal Strategy

Desactivar LAN revierte el servicio a loopback, invalida sesiones y retira la regla correspondiente si existe. Mantener sin cambios el almacenamiento local.
