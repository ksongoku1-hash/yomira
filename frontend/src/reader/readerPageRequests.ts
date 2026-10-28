import { getReaderPageBlob } from "../api/client";

type Priority = "visible" | "preload";
interface Request {
  priority: Priority;
  signal: AbortSignal | undefined;
  start: () => Promise<Blob>;
  resolve: (blob: Blob) => void;
  reject: (error: unknown) => void;
  abort: () => void;
}
const waiting: Request[] = [];
let running = 0;
let runningPreloads = 0;

function drain() {
  while (running < 2 && waiting.length > 0) {
    const urgent = waiting.findIndex((request) => request.priority === "visible");
    // Leave a slot for visible pages instead of filling both with slow preloads.
    if (urgent < 0 && runningPreloads > 0) break;
    const request = waiting.splice(urgent < 0 ? 0 : urgent, 1)[0]!;
    request.signal?.removeEventListener("abort", request.abort);
    running++;
    if (request.priority === "preload") runningPreloads++;
    void request.start().then(request.resolve, request.reject).finally(() => {
      running--;
      if (request.priority === "preload") runningPreloads--;
      drain();
    });
  }
}

export function prioritizeReaderPage(signal: AbortSignal): void {
  const request = waiting.find((pending) => pending.signal === signal);
  if (request === undefined) return;
  request.priority = "visible";
  drain();
}

export function requestReaderPage(
  chapterId: string, page: number, signal?: AbortSignal, priority: Priority = "visible",
): Promise<Blob> {
  if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const request: Request = {
      priority, signal, resolve, reject,
      start: () => getReaderPageBlob(chapterId, page, signal),
      abort: () => {
        const index = waiting.indexOf(request);
        if (index >= 0) waiting.splice(index, 1);
        reject(new DOMException("Aborted", "AbortError"));
      },
    };
    waiting.push(request);
    signal?.addEventListener("abort", request.abort, { once: true });
    drain();
  });
}
