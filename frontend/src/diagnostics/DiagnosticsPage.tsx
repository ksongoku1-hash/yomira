import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  ApiError,
  getSystemDiagnostics,
  getSystemLiveness,
  getSystemReadiness,
  type SystemDiagnosticCheck,
  type SystemDiagnosticId,
  type SystemDiagnosticStatus,
} from "../api/client";
import styles from "./DiagnosticsPage.module.css";

const DIAGNOSTIC_NAMES: Record<SystemDiagnosticId, string> = {
  backend: "Backend",
  database: "Base de datos",
  library: "Biblioteca",
  disk: "Espacio en disco",
  image_processor: "Procesador de imágenes",
  cbz_builder: "Constructor de CBZ",
  connectors: "Conectores",
  lan: "LAN",
};

const STATUS_NAMES: Record<SystemDiagnosticStatus, string> = {
  ok: "Correcto",
  warning: "Advertencia",
  error: "Error",
  unknown: "Estado desconocido",
};

function errorDetails(error: unknown) {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      code: error.code ?? "Sin código de servidor",
      status: error.status,
      traceId: error.traceId,
    };
  }

  return {
    message: "Falló una operación de la interfaz.",
    code: "Sin código de servidor",
    status: null,
    traceId: null,
  };
}

function ErrorDetails({ error }: { error: unknown }) {
  const details = errorDetails(error);
  return (
    <>
      <p>{details.message}</p>
      <p className={styles.errorMeta}>
        Código: <code>{details.code}</code>
        {details.status !== null ? <span> · HTTP {details.status}</span> : null}
      </p>
      {details.traceId ? (
        <p className={styles.errorMeta}>
          Traza: <code>{details.traceId}</code>
        </p>
      ) : null}
    </>
  );
}

function DiagnosticCheck({ check }: { check: SystemDiagnosticCheck }) {
  const statusClass = {
    ok: styles.statusOk,
    warning: styles.statusWarning,
    error: styles.statusError,
    unknown: styles.statusUnknown,
  }[check.status];

  return (
    <li className={styles.checkItem} data-check-id={check.id}>
      <article className={styles.checkCard}>
        <div className={styles.checkHeading}>
          <h3>{DIAGNOSTIC_NAMES[check.id]}</h3>
          <span className={[styles.statusBadge, statusClass].join(" ")}>
            {STATUS_NAMES[check.status]}
          </span>
        </div>
        <p className={styles.checkMessage}>{check.message}</p>
        {check.code !== null ? (
          <p className={styles.checkMeta}>
            Código: <code>{check.code}</code>
          </p>
        ) : null}
        {check.traceId !== null ? (
          <p className={styles.checkMeta}>
            Traza: <code>{check.traceId}</code>
          </p>
        ) : null}
      </article>
    </li>
  );
}

export function DiagnosticsPage() {
  const liveness = useQuery({
    queryKey: ["system", "liveness"],
    queryFn: getSystemLiveness,
    staleTime: 0,
  });
  const readiness = useQuery({
    queryKey: ["system", "readiness"],
    queryFn: getSystemReadiness,
    staleTime: 0,
  });
  const diagnostics = useQuery({
    queryKey: ["system", "diagnostics"],
    queryFn: getSystemDiagnostics,
    staleTime: 0,
  });
  const refreshing = liveness.isFetching || readiness.isFetching || diagnostics.isFetching;
  const backendNotReady =
    readiness.data?.status === "not_ready" || readiness.isError;

  function refreshAll() {
    void Promise.all([
      liveness.refetch(),
      readiness.refetch(),
      diagnostics.refetch(),
    ]);
  }

  return (
    <section aria-labelledby="diagnostics-title" className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Estado local de Yomira</p>
          <h1 id="diagnostics-title">Diagnóstico</h1>
          <p className={styles.intro}>
            Consulta la disponibilidad del servicio y el estado de sus componentes locales.
          </p>
        </div>
        <button
          className={styles.refreshButton}
          disabled={refreshing}
          onClick={refreshAll}
          type="button"
        >
          {refreshing ? "Actualizando…" : "Actualizar diagnóstico"}
        </button>
      </header>

      {backendNotReady ? (
        <section aria-labelledby="recovery-title" className={styles.recoveryPanel}>
          <h2 id="recovery-title">Modo de recuperación</h2>
          <p>
            El backend no está listo o no se pudo verificar. Esta pantalla conserva el acceso a
            diagnósticos de solo lectura; no se ejecutarán operaciones que cambien datos hasta
            que el servicio esté listo.
          </p>
          <Link className={styles.homeLink} to="/">
            Volver a Inicio
          </Link>
        </section>
      ) : null}

      <section aria-labelledby="availability-title" className={styles.panel}>
        <h2 id="availability-title">Disponibilidad</h2>
        <div className={styles.availabilityGrid}>
          <article className={styles.availabilityCard}>
            <h3>Proceso (liveness)</h3>
            {liveness.isPending ? (
              <p aria-live="polite" className={styles.statusText} role="status">
                Comprobando proceso…
              </p>
            ) : liveness.isError ? (
              <div aria-labelledby="liveness-error-title" className={styles.inlineError} role="alert">
                <h4 id="liveness-error-title">No se pudo comprobar el proceso</h4>
                <ErrorDetails error={liveness.error} />
                <button
                  className={styles.retryButton}
                  disabled={liveness.isFetching}
                  onClick={() => void liveness.refetch()}
                  type="button"
                >
                  Reintentar liveness
                </button>
              </div>
            ) : (
              <p className={styles.stateOk}>Proceso activo.</p>
            )}
          </article>

          <article className={styles.availabilityCard}>
            <h3>Preparación (readiness)</h3>
            {readiness.isPending ? (
              <p aria-live="polite" className={styles.statusText} role="status">
                Comprobando readiness…
              </p>
            ) : readiness.isError ? (
              <div aria-labelledby="readiness-error-title" className={styles.inlineError} role="alert">
                <h4 id="readiness-error-title">No se pudo comprobar readiness</h4>
                <ErrorDetails error={readiness.error} />
                <button
                  className={styles.retryButton}
                  disabled={readiness.isFetching}
                  onClick={() => void readiness.refetch()}
                  type="button"
                >
                  Reintentar readiness
                </button>
              </div>
            ) : readiness.data.status === "ready" ? (
              <p className={styles.stateOk}>Servicio listo.</p>
            ) : (
              <p className={styles.stateWarning}>Servicio no listo.</p>
            )}
          </article>
        </div>
      </section>

      <section aria-labelledby="checks-title" className={styles.panel}>
        <header className={styles.checksHeader}>
          <div>
            <h2 id="checks-title">Componentes</h2>
            {diagnostics.data ? (
              <>
                <p className={styles.version}>
                  Yomira {diagnostics.data.appVersion} · API {diagnostics.data.apiVersion}
                </p>
                <p className={styles.schemaVersion}>
                  Esquema de base de datos: {diagnostics.data.schemaVersion ?? "No disponible"}
                </p>
              </>
            ) : null}
          </div>
          {diagnostics.isFetching ? (
            <p aria-live="polite" className={styles.refreshing} role="status">
              Actualizando comprobaciones…
            </p>
          ) : null}
        </header>

        {diagnostics.isPending ? (
          <p aria-live="polite" className={styles.loading} role="status">
            Cargando comprobaciones del sistema…
          </p>
        ) : diagnostics.isError ? (
          <div aria-labelledby="diagnostics-error-title" className={styles.errorState} role="alert">
            <div>
              <h3 id="diagnostics-error-title">Error de diagnóstico</h3>
              <ErrorDetails error={diagnostics.error} />
            </div>
            <button
              className={styles.retryButton}
              disabled={diagnostics.isFetching}
              onClick={() => void diagnostics.refetch()}
              type="button"
            >
              Reintentar diagnóstico
            </button>
          </div>
        ) : diagnostics.data.checks.length === 0 ? (
          <p className={styles.emptyState} role="status">
            No hay comprobaciones de sistema disponibles.
          </p>
        ) : (
          <ul aria-label="Comprobaciones del sistema" className={styles.checkList}>
            {diagnostics.data.checks.map((check) => (
              <DiagnosticCheck check={check} key={check.id} />
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
