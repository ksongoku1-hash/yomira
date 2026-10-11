import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router-dom";

import styles from "./App.module.css";
import { useShellStore } from "./app/shellStore";
import { useAppearanceStore } from "./app/appearanceStore";
import { DownloadsPage } from "./downloads/DownloadsPage";
import { DiagnosticsPage } from "./diagnostics/DiagnosticsPage";
import { HomePage } from "./home/HomePage";
import { LibraryPage } from "./library/LibraryPage";
import "./global.css";
import { ReaderPage } from "./reader/ReaderPage";
import { SearchPage } from "./search/SearchPage";
import { SettingsPage } from "./settings/SettingsPage";
import { SeriesDetailPage } from "./series/SeriesDetailPage";
import { UsbExportsPage } from "./usb/UsbExportsPage";

function createQueryClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        retry: false,
        staleTime: 30_000,
      },
      mutations: { retry: false },
    },
  });
  for (const key of ["download-jobs", "reader-chapters", "library", "series"]) {
    client.setQueryDefaults([key], {
      refetchInterval: 2_000,
      refetchOnMount: "always",
      refetchOnWindowFocus: "always",
    });
  }
  return client;
}

const navigationSections = [
  {
    label: "Explorar",
    items: [
      { label: "Inicio", path: "/", icon: "⌂" },
      { label: "Biblioteca", path: "/library", icon: "▤" },
      { label: "Buscar", path: "/search", icon: "⌕" },
    ],
  },
  {
    label: "Descargas",
    items: [
      { label: "Cola de descargas", path: "/downloads", icon: "⇩" },
      { label: "Exportar USB", path: "/usb-exports", icon: "⇧" },
    ],
  },
  {
    label: "Aplicación",
    items: [
      { label: "Ajustes", path: "/settings", icon: "⚙" },
      { label: "Diagnóstico", path: "/diagnostics", icon: "ⓘ" },
    ],
  },
] as const;

function AppShell() {
  const reading = useLocation().pathname.startsWith("/reader/");
  const simpleGraphics = useAppearanceStore((state) => state.simpleGraphics);
  const isNavigationOpen = useShellStore((state) => state.isNavigationOpen);
  const toggleNavigation = useShellStore((state) => state.toggleNavigation);
  const closeNavigation = useShellStore((state) => state.closeNavigation);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!isNavigationOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeNavigation();
        menuButtonRef.current?.focus();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [isNavigationOpen, closeNavigation]);

  return (
    <div className={`${styles.appShell} ${reading ? styles.readerShell : ""} ${simpleGraphics ? styles.simpleGraphics : ""}`}>
      <a className={styles.skipLink} href="#main-content">Saltar al contenido</a>
      <header className={styles.topBar} hidden={reading}>
        <NavLink className={[styles.brand].filter(Boolean).join(" ")} to="/" onClick={closeNavigation}>
          <img alt="" className={styles.brandMark} src="/assets/brand/yomira-manga-portrait-v2.png" width="56" height="56" />
          <span>
            <strong>Yomira</strong>
            <small>Tu mundo entre páginas</small>
          </span>
        </NavLink>
        <nav
          aria-label="Navegación principal"
          className={styles.navigation}
          id="primary-navigation"
        >
          {navigationSections.slice(0, 2).map((section) => (
            <div className={styles.navigationSection} key={section.label}>
              <h2>{section.label}</h2>
              {section.items.filter(({ path }) => path !== "/usb-exports").map(({ icon, label, path }) => (
                <NavLink
                  className={({ isActive }) =>
                    [styles.navigationLink, isActive ? styles.navigationLinkActive : ""]
                      .filter(Boolean)
                      .join(" ")
                  }
                  end={path === "/"}
                  aria-label={label}
                  key={path}
                  onClick={closeNavigation}
                  to={path}
                >
                  <span aria-hidden="true" className={styles.navigationIcon}>{icon}</span>
                  <span>{path === "/downloads" ? "Descargas" : label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className={styles.localStatus}>
          <span aria-hidden="true" className={styles.localDot} />
          <span>Biblioteca local</span>
        </div>
          <button
            aria-controls="more-navigation"
            aria-expanded={isNavigationOpen}
            aria-label={isNavigationOpen ? "Cerrar navegación" : "Abrir navegación"}
            className={styles.menuButton}
            ref={menuButtonRef}
            onClick={toggleNavigation}
            type="button"
          >
            <span aria-hidden="true">{isNavigationOpen ? "×" : "☰"}</span>
            <span className={styles.menuLabel}>Más</span>
          </button>
          <nav aria-label="Más opciones" className={styles.moreNavigation} hidden={!isNavigationOpen}
            id="more-navigation">
            {[
              navigationSections[1].items[1],
              ...navigationSections[2].items,
            ].map(({ label, path, icon }) => (
              <NavLink className={({ isActive }) => [styles.navigationLink, isActive ? styles.navigationLinkActive : ""].filter(Boolean).join(" ")}
                key={path} onClick={closeNavigation} to={path}>
                <span aria-hidden="true">{icon}</span>{label}
              </NavLink>
            ))}
          </nav>
      </header>
      <div className={styles.appMain}>
        <main className={styles.mainContent} id="main-content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function App() {
  const [queryClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route element={<HomePage />} path="/" />
            <Route element={<LibraryPage />} path="/library" />
            <Route element={<SeriesDetailPage />} path="/series/:seriesId" />
            <Route element={<SearchPage />} path="/search" />
            <Route element={<DownloadsPage />} path="/downloads" />
            <Route element={<UsbExportsPage />} path="/usb-exports" />
            <Route element={<ReaderPage />} path="/reader/:chapterId" />
            <Route element={<SettingsPage />} path="/settings" />
            <Route element={<DiagnosticsPage />} path="/diagnostics" />
            <Route element={<Navigate replace to="/" />} path="*" />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

