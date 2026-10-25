import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router-dom";

import styles from "./App.module.css";
import { useShellStore } from "./app/shellStore";
import { useAppearanceStore } from "./app/appearanceStore";
import { queryPollingInterval } from "./app/queryPolling";
import { AppUpdates } from "./app/AppUpdates";
import { ActionIcon } from "./app/ActionIcon";
import { AppSearch } from "./app/AppSearch";

import { HomePage } from "./home/HomePage";

import "./global.css";

const DownloadsPage = lazy(() => import("./downloads/DownloadsPage").then((module) => ({default: module.DownloadsPage})));

const DiagnosticsPage = lazy(() => import("./diagnostics/DiagnosticsPage").then((module) => ({default: module.DiagnosticsPage})));

const LibraryPage = lazy(() => import("./library/LibraryPage").then((module) => ({default: module.LibraryPage})));

const ReaderPage = lazy(() => import("./reader/ReaderPage").then((module) => ({default: module.ReaderPage})));

const SearchPage = lazy(() => import("./search/SearchPage").then((module) => ({default: module.SearchPage})));

const SettingsPage = lazy(() => import("./settings/SettingsPage").then((module) => ({default: module.SettingsPage})));

const SeriesDetailPage = lazy(() => import("./series/SeriesDetailPage").then((module) => ({default: module.SeriesDetailPage})));

const UsbExportsPage = lazy(() => import("./usb/UsbExportsPage").then((module) => ({default: module.UsbExportsPage})));

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
      refetchInterval: () => queryPollingInterval(client),
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
      { label: "Inicio", path: "/", icon: "home" },
      { label: "Explorar", path: "/search", icon: "search" },
      { label: "Biblioteca", path: "/library", icon: "grid" },
    ],
  },
  {
    label: "Descargas",
    items: [
      { label: "Cola de descargas", path: "/downloads", icon: "download" },
      { label: "Exportar USB", path: "/usb-exports", icon: "device" },
    ],
  },
  {
    label: "Aplicación",
    items: [
      { label: "Configuración", path: "/settings", icon: "settings" },
      { label: "Diagnóstico", path: "/diagnostics", icon: "eye" },
    ],
  },
] as const;

function AppShell() {
  const pathname = useLocation().pathname;
  const reading = pathname.startsWith("/reader/");
  const [collapsed, setCollapsed] = useState(() => window.innerWidth <= 1100);
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
    <div className={`${styles.appShell} ${reading ? styles.readerShell : ""} ${collapsed ? styles.collapsed : ""} ${simpleGraphics ? styles.simpleGraphics : ""}`}>
      <a className={styles.skipLink} href="#main-content">Saltar al contenido</a>
      <header className={styles.topBar} hidden={reading}>
        <NavLink className={[styles.brand].filter(Boolean).join(" ")} to="/" onClick={closeNavigation}>
          <img alt="" className={styles.brandMark} src="/assets/brand/yomira-ice-mark.svg" width="44" height="44" />
          <span className={styles.brandCopy}>
            <strong>Yomira</strong>
            <small>Tu mundo entre páginas</small>
          </span>
        </NavLink>
        <nav
          aria-label="Navegación principal"
          className={styles.navigation}
          id="primary-navigation"
        >
          {[...navigationSections.slice(0, 2), {label: "Preferencias", items: [navigationSections[2].items[0]]}].map((section) => (
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
                  title={label}
                  key={path}
                  onClick={closeNavigation}
                  to={path}
                >
                  <span className={styles.navigationIcon}><ActionIcon name={icon} /></span>
                  <span className={styles.navigationLabel}>{path === "/downloads" ? "Descargas" : label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className={styles.localStatus} title="Biblioteca local">
          <span aria-hidden="true" className={styles.localDot} />
          <span className={styles.navigationLabel}>Biblioteca local</span>
        </div>
        <button className={styles.collapseButton} type="button" aria-controls="primary-navigation" aria-expanded={!collapsed}
          aria-label={collapsed ? "Expandir menú" : "Contraer menú"} title={collapsed ? "Expandir menú" : "Contraer menú"}
          onClick={() => setCollapsed(!collapsed)}><ActionIcon name="sidebar" /><span className={styles.navigationLabel}>{collapsed ? "Expandir menú" : "Contraer menú"}</span></button>
          <button
            aria-controls="more-navigation"
            aria-expanded={isNavigationOpen}
            aria-label={isNavigationOpen ? "Cerrar navegación" : "Abrir navegación"}
            title={isNavigationOpen ? "Cerrar navegación" : "Más opciones"}
            className={styles.menuButton}
            ref={menuButtonRef}
            onClick={toggleNavigation}
            type="button"
          >
            <ActionIcon name={isNavigationOpen ? "close" : "more"} />
            <span className={styles.menuLabel}>Más</span>
          </button>
          <nav aria-label="Más opciones" className={styles.moreNavigation} hidden={!isNavigationOpen}
            id="more-navigation">
            {[
              navigationSections[1].items[1],
              navigationSections[2].items[1],
            ].map(({ label, path, icon }) => (
              <NavLink className={({ isActive }) => [styles.navigationLink, isActive ? styles.navigationLinkActive : ""].filter(Boolean).join(" ")}
                key={path} onClick={closeNavigation} to={path}>
                <ActionIcon name={icon} />{label}
              </NavLink>
            ))}
          </nav>
      </header>
      <div className={styles.appMain}>
        <main className={styles.mainContent} id="main-content" tabIndex={-1}>
          {!reading ? <AppSearch /> : null}
          <AppUpdates compact hidden={reading || pathname === "/settings"} />
          <Suspense fallback={<p role="status">Abriendo pantalla…</p>}><Outlet /></Suspense>
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

