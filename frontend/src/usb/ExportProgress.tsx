import type { UsbExportJob } from "../api/client";

const labels: Record<string, string> = {
  verify_source: "Comprobando el archivo del equipo",
  copy: "Copiando al lector",
  verify_staging: "Comprobando la copia",
  resume_staging: "Revisando el envío anterior",
  destination_verify: "Comprobando el archivo existente",
  publish: "Guardando el nombre final",
  verify_final: "Verificando el archivo final",
};

export function ExportProgress({ job }: { job: UsbExportJob }) {
  const progress = job.progress;
  if (!progress) return null;
  const percent = Math.floor(progress.processedBytes / progress.totalBytes * 100);
  const label = labels[progress.stage] ?? "Preparando el envío";
  return <div role="status" aria-live="polite">
    <p>{label}: {percent}% de esta etapa · {(progress.processedBytes / 1_000_000).toFixed(1)} de {(progress.totalBytes / 1_000_000).toFixed(1)} MB</p>
    <progress aria-label={label} max={progress.totalBytes} value={progress.processedBytes} />
  </div>;
}
