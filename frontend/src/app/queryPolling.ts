import type { QueryClient } from "@tanstack/react-query";
import type { DownloadJobStatus, DownloadJobsPage } from "../api/client";

const active: DownloadJobStatus[] = ["QUEUED", "FETCHING_METADATA", "DOWNLOADING", "VALIDATING",
  "PROCESSING", "PACKAGING", "VERIFYING", "INDEXING"];

export function queryPollingInterval(client: QueryClient): number | false {
  if (document.visibilityState === "hidden") return false;
  const busy = client.getQueriesData<DownloadJobsPage>({ queryKey: ["download-jobs"] })
    .some(([, page]) => page !== undefined && (page.meta.states !== undefined
      ? active.some((status) => (page.meta.states?.[status] ?? 0) > 0)
      : page.data.some((job) => active.includes(job.status))));
  return busy ? 2_000 : 15_000;
}
