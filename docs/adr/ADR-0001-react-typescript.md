# ADR-0001 — React + TypeScript

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

La especificación fija React y TypeScript con `strict = true` para la interfaz de Windows, el lector y las vistas adaptables a equipos lentos.

## Decision

Usar React + TypeScript estricto, Vite, React Router, TanStack Query, Zustand, CSS Modules y variables CSS, junto con Vitest, React Testing Library y Playwright.

## Alternatives Considered

- Vue + TypeScript: viable, pero no es el stack fijado por la SPEC.
- JavaScript sin tipado estático: reduce configuración inicial, pero debilita los contratos entre UI y API.
- Interfaz nativa Windows: no cumple por sí sola los objetivos de lector local y acceso desde navegador LAN.

## Positive Consequences

- Contratos de UI más verificables en compilación.
- Stack coherente con pruebas unitarias, componentes y flujos E2E ya requeridos.
- La UI puede tratar el backend como API y no necesita acceder al filesystem.

## Negative Consequences

- Requiere proceso de build y gestión de dependencias frontend.
- El tipado de TypeScript no valida por sí mismo datos recibidos en ejecución.

## Failure Modes

- Desfase entre tipos frontend y schemas Pydantic.
- Error de una sección que se propaga al resto de la aplicación.
- Listas grandes o lector virtualizado que consumen recursos sin límites.

## Reversal Strategy

Una sustitución del framework requiere ADR nuevo, compatibilidad de la API y migración gradual de vistas con pruebas de regresión. No cambia el contrato de archivos ni de biblioteca.
