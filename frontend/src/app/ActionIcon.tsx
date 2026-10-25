const paths = {
  sidebar: "M3 3h18v18H3V3Zm6 0v18m6-13-3 4 3 4",
  pause: "M8 5v14m8-14v14",
  check: "m5 12 4 4L19 6",
  flame: "M12 3c1 5 7 6 7 12a7 7 0 0 1-14 0c0-3 2-5 4-7 0 3 1 4 2 4 2-2 2-5 1-9Z",
  home: "m3 10 9-7 9 7v11h-6v-7H9v7H3V10Z",
  search: "M11 18a7 7 0 1 1 0-14a7 7 0 0 1 0 14Zm5-2 5 5",
  sparkle: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z",
  external: "M7 17 19 5M8 5h11v11M5 7v14h14",
  grid: "M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z",
  back: "M19 12H5m7-7-7 7 7 7",
  previous: "m15 6-6 6 6 6",
  next: "m9 6 6 6-6 6",
  play: "m8 5 11 7-11 7V5Z",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  settings: "M4 6h8m4 0h4M4 12h2m4 0h10M4 18h10m4 0h2M12 3v6M6 9v6m8 0v6",
  fullscreen: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5",
  exitFullscreen: "M3 8h5V3m13 5h-5V3M8 21v-5H3m13 5v-5h5",
  focus: "M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3M9 12h6",
  more: "M5 11v2m7-2v2m7-2v2",
  refresh: "M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2",
  close: "m6 6 12 12M6 18 18 6",
  chapters: "M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1",
  page: "M14 3H6v18h13V8l-5-5Zm0 0v5h5M9 12h7M9 16h7",
  vertical: "M4 3h9v6H4V3Zm0 12h9v6H4v-6ZM19 4v15m-3-3 3 3 3-3",
  spread: "M12 5C9 3 6 3 3 4v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15",
  continuous: "M5 3h14v6H5V3Zm0 12h14v6H5v-6ZM8 9v6m8-6v6",
  rtl: "M5 5h14M9 9h10M5 15h14m-10-4-4 4 4 4",
  zoomIn: "M11 18a7 7 0 1 1 0-14a7 7 0 0 1 0 14Zm5-2 5 5M8 11h6m-3-3v6",
  zoomOut: "M11 18a7 7 0 1 1 0-14a7 7 0 0 1 0 14Zm5-2 5 5M8 11h6",
  fitWidth: "M3 5v14m18-14v14M6 12h12m-9-3-3 3 3 3m6-6 3 3-3 3",
  fitHeight: "M5 3h14M5 21h14M12 6v12m-3-9 3-3 3 3m-6 6 3 3 3-3",
  device: "M6 3h12a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1ZM9 7h6M9 11h6M11 17h2",
  moon: "M20.5 13a8.5 8.5 0 1 1-9.5-9.5A6.5 6.5 0 0 0 20.5 13Z",
  sepia: "M5 19c9 1 15-4 14-14C9 4 4 10 5 19Zm0 0L15 9m-4 4h5",
  sun: "M16 12a4 4 0 1 1-8 0a4 4 0 0 1 8 0ZM12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5",
  eye: "M2 12C6 3 18 3 22 12 18 21 6 21 2 12Zm13 0a3 3 0 1 1-6 0a3 3 0 0 1 6 0Z",
  keyboard: "M3 5h18v14H3V5ZM6 9h1m4 0h1m4 0h1M6 13h1m4 0h1m4 0h1M7 16h10",
  progress: "M4 8h16v8H4V8Zm4 0v8m4-8v8",
  compact: "M5 4h14v4H5V4Zm0 6h14v4H5v-4Zm0 6h14v4H5v-4Z",
} as const;

export function ActionIcon({ name }: { name: keyof typeof paths }) {
  return <svg aria-hidden="true" focusable="false" width="18" height="18" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
    <path d={paths[name]} />
  </svg>;
}
