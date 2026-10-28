import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import styles from "./SettingsPage.module.css";

interface MobileStatus {
  enabled: boolean;
  local: boolean;
  addresses?: string[];
  url?: string | null;
  pin?: string | null;
  auto_start?: boolean;
  windows_firewall_supported?: boolean;
  startup_error?: string | null;
}

export const MOBILE_QUERY_KEY = ["system", "mobile"] as const;
export const isMobileConnection = !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);

async function mobileRequest(action = "", address?: string, enabled?: boolean): Promise<MobileStatus> {
  const response = await fetch(`/api/v1/system/mobile${action}`, {
    ...(action ? { method: "POST", headers: { "Content-Type": "application/json", "X-Yomira-Mobile": "1" },
      body: JSON.stringify({ address, enabled }) } : {}),
    cache: "no-store",
  });
  if (response.status === 401 && isMobileConnection) location.replace("/mobile/login");
  if (!response.ok) throw new Error("No se pudo cambiar la conexión. Revisa la red y vuelve a intentarlo.");
  const result: unknown = await response.json();
  if (!result || typeof result !== "object" || !("enabled" in result) || !("local" in result)
    || typeof result.enabled !== "boolean" || typeof result.local !== "boolean") {
    throw new Error("La conexión móvil no devolvió un estado válido.");
  }
  return result as MobileStatus;
}

export function MobileSessionGuard() {
  useQuery({ queryKey: MOBILE_QUERY_KEY, queryFn: () => mobileRequest(), enabled: isMobileConnection,
    refetchInterval: 30_000, refetchOnWindowFocus: true, retry: false });
  return null;
}

export function MobileAccessPanel() {
  const client = useQueryClient();
  const status = useQuery({ queryKey: MOBILE_QUERY_KEY, queryFn: () => mobileRequest(), retry: false });
  const [address, setAddress] = useState("");
  const [showPin, setShowPin] = useState(false);
  const [notice, setNotice] = useState("");
  const data = status.data;
  const mutation = useMutation({
    mutationFn: ({ action, enabled }: { action: string; enabled?: boolean }) =>
      mobileRequest(action, address || data?.addresses?.[0], enabled),
    onSuccess: (result) => { client.setQueryData(MOBILE_QUERY_KEY, result); setShowPin(false); setNotice(""); },
  });

  async function copyAddress() {
    if (!data?.url) return;
    try { await navigator.clipboard.writeText(data.url); setNotice("Dirección copiada."); }
    catch { setNotice("Selecciona la dirección y cópiala para enviarla a tu móvil."); }
  }

  async function allowWindows() {
    setNotice("");
    try {
      const response = await fetch("/api/v1/system/mobile/firewall", { method: "POST",
        headers: { "X-Yomira-Mobile": "1" } });
      if (!response.ok) throw new Error();
      setNotice("Acepta el aviso de Windows y abre la dirección desde el móvil.");
    } catch { setNotice("Windows no pudo abrir el permiso. Vuelve a intentarlo desde el PC."); }
  }

  return <section aria-labelledby="mobile-access-title" className={styles.appearanceCard}>
    <h2 id="mobile-access-title">Acceso desde móvil</h2>
    <p className={styles.fieldDescription}>Lee desde el navegador del móvil en la misma red Wi-Fi.
      El PC debe permanecer encendido. Usa tu red de confianza.</p>
    {status.isPending ? <p>Comprobando conexión…</p> : null}
    {status.isError ? <p role="status">No se pudo consultar la conexión. <button type="button"
      className={styles.secondaryButton} onClick={() => void status.refetch()}>Reintentar conexión</button></p> : null}
    {data?.local ? <>
      <label className={styles.toggleLabel}>
        <input type="checkbox" checked={data.auto_start ?? false} disabled={mutation.isPending}
          onChange={(event) => mutation.mutate({ action: "/startup", enabled: event.target.checked })} />
        Activar el acceso móvil al iniciar Yomira
      </label>
      <p className={styles.fieldDescription}>{data.auto_start
        ? "Se abrirá automáticamente cada vez que inicies Yomira, con un PIN nuevo."
        : "Activa esta opción para recordar el acceso móvil en los próximos inicios."}</p>
      {data.startup_error ? <p role="status">{data.startup_error}</p> : null}
      {!data.enabled ? <>
        {(data.addresses?.length ?? 0) > 1 ? <label className={styles.fieldLabel}>
          Red del PC <select className={styles.selectInput} value={address || data.addresses?.[0]}
            onChange={(event) => setAddress(event.target.value)}>
            {data.addresses?.map((item) => <option key={item} value={item}>{item}</option>)}
          </select></label> : null}
        <button type="button" className={styles.primaryButton} disabled={mutation.isPending || !data.addresses?.length}
          onClick={() => mutation.mutate({ action: "/enable" })}>Activar acceso desde móvil</button>
        {!data.addresses?.length ? <p>Conecta el PC a tu red de casa para activar el acceso.</p> : null}
      </> : <>
        <p>Conexión activa · sesiones de 8 horas</p>
        <label className={styles.fieldLabel}>Dirección para el móvil
          <input className={styles.selectInput} readOnly value={data.url ?? ""} onFocus={(event) => event.target.select()} />
        </label>
        <button type="button" className={styles.secondaryButton} onClick={() => void copyAddress()}>Copiar dirección</button>
        <p>PIN: <strong>{showPin ? data.pin : "••••••"}</strong> <button type="button"
          className={styles.secondaryButton} aria-pressed={showPin} onClick={() => setShowPin(!showPin)}>
          {showPin ? "Ocultar PIN" : "Mostrar PIN"}</button></p>
        {data.windows_firewall_supported !== false ? <>
        <button type="button" className={styles.secondaryButton} onClick={() => void allowWindows()}>Permitir conexión en Windows</button>
        <p className={styles.fieldDescription}>La primera vez, permite la conexión en Windows.
          Solo se habilita para tu red privada. Si cambia la dirección del PC, repite este paso.</p>
        </> : <p className={styles.fieldDescription}>Si el móvil no conecta, revisa el cortafuegos de tu distribución de Linux.</p>}
        <button type="button" className={styles.secondaryButton} disabled={mutation.isPending}
          onClick={() => mutation.mutate({ action: "/disable" })}>Desactivar y cerrar sesiones</button>
      </>}
      <p className={styles.fieldDescription}>El progreso y las descargas se guardan en tu PC;
        el móvil comparte la misma biblioteca. El acceso se cierra al salir{data.auto_start
          ? " y volverá a activarse automáticamente al iniciar Yomira." : "."}</p>
    </> : data ? <>
      <p>Conectado a tu Yomira. Para cambiar la conexión, usa Ajustes en el PC.</p>
      <form method="post" action="/mobile/logout"><button className={styles.secondaryButton}>Cerrar sesión del móvil</button></form>
    </> : null}
    {mutation.isError ? <p role="alert">{mutation.error.message}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
  </section>;
}
