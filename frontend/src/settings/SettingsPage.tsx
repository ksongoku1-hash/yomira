import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, getReaderSettings, putReaderSettings } from "../api/client";
import type { ReaderSettings } from "../api/client";
import { LocalImportPanel } from "../library/LibraryPage";
import { useAppearanceStore } from "../app/appearanceStore";
import { AppUpdates } from "../app/AppUpdates";
import styles from "./SettingsPage.module.css";
import { isMobileConnection, MobileAccessPanel } from "./MobileAccessPanel";
import { ReaderCachePanel } from "./ReaderCachePanel";

const READER_SETTINGS_QUERY_KEY = ["reader-settings"] as const;

interface ResourceSettingsDraft {
  cacheLimitMb: string;
  preloadAhead: string;
}

function SettingsError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const apiError = error instanceof ApiError ? error : null;

  return (
    <div className={styles.errorState} role="alert">
      <div>
        <h2>No se pudieron cargar los ajustes</h2>
        <p>{apiError?.message ?? "Falló una operación de ajustes."}</p>
        <p className={styles.errorDetails}>
          Código: <code>{apiError?.code ?? "Sin código de servidor"}</code>
          {apiError?.status !== null && apiError?.status !== undefined ? (
            <span> · HTTP {apiError.status}</span>
          ) : null}
        </p>
        {apiError?.traceId ? (
          <p className={styles.errorDetails}>
            Traza: <code>{apiError.traceId}</code>
          </p>
        ) : null}
      </div>
      <button className={styles.secondaryButton} onClick={onRetry} type="button">
        Reintentar carga
      </button>
    </div>
  );
}

function isValidDraft(draft: ResourceSettingsDraft): boolean {
  const cacheLimitMb = Number(draft.cacheLimitMb);
  const preloadAhead = Number(draft.preloadAhead);
  return (
    draft.cacheLimitMb.trim() !== "" &&
    Number.isSafeInteger(cacheLimitMb) &&
    cacheLimitMb >= 16 &&
    cacheLimitMb <= 500 &&
    draft.preloadAhead.trim() !== "" &&
    Number.isSafeInteger(preloadAhead) &&
    preloadAhead >= 0 &&
    preloadAhead <= 2
  );
}

function isDraftDirty(draft: ResourceSettingsDraft, saved: ReaderSettings): boolean {
  return (
    Number(draft.cacheLimitMb) !== saved.cache_limit_mb ||
    Number(draft.preloadAhead) !== saved.preload_ahead
  );
}

export function SettingsPage() {
  const appearance = useAppearanceStore();
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: READER_SETTINGS_QUERY_KEY,
    queryFn: getReaderSettings,
  });
  const [draft, setDraft] = useState<ResourceSettingsDraft | null>(null);
  const saveMutation = useMutation({
    mutationFn: putReaderSettings,
    onSuccess: (settings) => {
      queryClient.setQueryData(READER_SETTINGS_QUERY_KEY, settings);
    },
  });

  const savedSettings = settingsQuery.data;
  const visibleDraft = draft ?? (savedSettings === undefined ? null : {
    cacheLimitMb: String(savedSettings.cache_limit_mb),
    preloadAhead: String(savedSettings.preload_ahead),
  });
  const valid = visibleDraft !== null && isValidDraft(visibleDraft);
  const dirty =
    visibleDraft !== null && savedSettings !== undefined && isDraftDirty(visibleDraft, savedSettings);

  function updateDraft(update: Partial<ResourceSettingsDraft>) {
    if (visibleDraft === null) return;
    saveMutation.reset();
    setDraft({ ...visibleDraft, ...update });
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || !dirty || visibleDraft === null || savedSettings === undefined) return;
    saveMutation.mutate({
      ...savedSettings,
      cache_limit_mb: Number(visibleDraft.cacheLimitMb),
      preload_ahead: Number(visibleDraft.preloadAhead),
    });
  }

  return (
    <section aria-labelledby="settings-title" className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Preferencias locales</p>
          <h1 id="settings-title">Ajustes</h1>
          <p className={styles.intro}>
            {isMobileConnection ? "Personaliza la apariencia y ajusta el lector." : "Revisa las carpetas de tus mangas y ajusta el lector."}
          </p>
        </div>
      </header>

      <MobileAccessPanel />
      {!isMobileConnection ? <LocalImportPanel /> : null}
      {!isMobileConnection ? <AppUpdates /> : null}
      <ReaderCachePanel />

      <section aria-labelledby="appearance-title" className={styles.appearanceCard}>
        <h2 id="appearance-title">Apariencia</h2>
        <label className={styles.fieldLabel} htmlFor="interface-theme">Tema de la interfaz</label>
        <select className={styles.selectInput} id="interface-theme" value={appearance.theme}
          aria-describedby="interface-theme-description"
          onChange={(event) => appearance.update({ theme: event.currentTarget.value === "papel" ? "papel" : "tinta" })}>
          <option value="tinta">Tinta · oscuro</option>
          <option value="papel">Papel · claro</option>
        </select>
        <p className={styles.fieldDescription} id="interface-theme-description">
          Se aplica al instante. El lector conserva el fondo y el brillo que elegiste para leer.
        </p>
        <label className={styles.toggleLabel}>
          <input type="checkbox" checked={appearance.enabled}
            onChange={(event) => appearance.update({ enabled: event.currentTarget.checked })} />
          Fondos ilustrados
        </label>
        <p className={styles.fieldDescription} id="backdrop-description">
          Usa la ilustración decorativa y las portadas locales en Inicio y en la ficha de cada manga. El lector mantiene su fondo habitual.
        </p>
        <label className={styles.fieldLabel} htmlFor="backdrop-intensity">
          Intensidad del fondo: {appearance.intensity}%
        </label>
        <input aria-describedby="backdrop-description" className={styles.rangeInput}
          id="backdrop-intensity" type="range" min={0} max={100} step={1}
          disabled={!appearance.enabled} value={appearance.intensity}
          onChange={(event) => appearance.update({ intensity: Number(event.currentTarget.value) })} />
        <label className={styles.toggleLabel}>
          <input type="checkbox" checked={appearance.simpleGraphics}
            onChange={(event) => appearance.update({ simpleGraphics: event.currentTarget.checked })} />
          Gráficos sencillos
        </label>
        <p className={styles.fieldDescription}>
          Desactiva los fondos ambientales, desenfoques, sombras y transiciones. Recomendado para equipos modestos y pantallas de tinta electrónica.
        </p>
        <p className={styles.fieldDescription}>
          Los cambios se aplican al instante y se guardan en este navegador.
        </p>
        {appearance.storageError ? <p className={styles.fieldDescription} role="alert">
          El navegador no permite guardar esta preferencia. Se aplicará durante esta sesión.
        </p> : null}
      </section>

      {settingsQuery.isPending ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Cargando ajustes del lector…
        </p>
      ) : settingsQuery.isError ? (
        <SettingsError error={settingsQuery.error} onRetry={() => void settingsQuery.refetch()} />
      ) : visibleDraft === null ? null : (
        <form className={styles.settingsForm} onSubmit={handleSubmit}>
          <div className={styles.settingCard}>
            <label className={styles.fieldLabel} htmlFor="reader-cache-limit">
              Presupuesto de caché del lector (MB)
            </label>
            <p className={styles.fieldDescription} id="reader-cache-description">
              Límite de memoria reservado para las páginas residentes; admite de 16 a 500 MB.
            </p>
            <input
              aria-describedby="reader-cache-description"
              className={styles.numberInput}
              id="reader-cache-limit"
              max={500}
              min={16}
              onChange={(event) => updateDraft({ cacheLimitMb: event.currentTarget.value })}
              step={1}
              type="number"
              value={visibleDraft.cacheLimitMb}
            />
          </div>

          <div className={styles.settingCard}>
            <label className={styles.fieldLabel} htmlFor="reader-preload-ahead">
              Pre-carga de páginas siguientes
            </label>
            <p className={styles.fieldDescription} id="reader-preload-description">
              Cantidad de páginas posteriores que el lector puede preparar, entre 0 y 2.
            </p>
            <select
              aria-describedby="reader-preload-description"
              className={styles.selectInput}
              id="reader-preload-ahead"
              onChange={(event) => updateDraft({ preloadAhead: event.currentTarget.value })}
              value={visibleDraft.preloadAhead}
            >
              <option value="0">Ninguna página</option>
              <option value="1">1 página</option>
              <option value="2">2 páginas</option>
            </select>
          </div>

          <div className={styles.formFooter}>
            {saveMutation.isError ? (
              <div className={styles.saveError} role="alert">
                <p>{saveMutation.error instanceof ApiError
                  ? saveMutation.error.message
                  : "No se pudieron guardar los ajustes."}</p>
                {saveMutation.error instanceof ApiError ? (
                  <p className={styles.errorDetails}>
                    Código: <code>{saveMutation.error.code ?? "Sin código de servidor"}</code>
                    {saveMutation.error.traceId ? (
                      <span> · Traza: <code>{saveMutation.error.traceId}</code></span>
                    ) : null}
                  </p>
                ) : null}
              </div>
            ) : saveMutation.isPending ? (
              <p aria-live="polite" className={styles.statusMessage} role="status">
                Guardando ajustes…
              </p>
            ) : saveMutation.data !== undefined && !dirty ? (
              <p aria-live="polite" className={styles.successMessage} role="status">
                Ajustes guardados.
              </p>
            ) : dirty ? (
              <p className={styles.statusMessage}>Hay cambios sin guardar.</p>
            ) : (
              <p className={styles.statusMessage}>Los ajustes están guardados.</p>
            )}
            <button
              className={styles.primaryButton}
              disabled={!valid || !dirty || saveMutation.isPending}
              type="submit"
            >
              {saveMutation.isPending ? "Guardando…" : "Guardar ajustes"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
