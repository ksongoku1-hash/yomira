from app.factory import create_app
from app.settings import Settings
from app.version import APP_VERSION


def test_application_factory_builds_independent_fastapi_instances() -> None:
    first_app = create_app()
    second_app = create_app()

    assert first_app is not second_app
    assert first_app.title == "Yomira API"
    assert first_app.version == APP_VERSION
    assert set(first_app.openapi()["paths"]) == {
        "/api/v1/library",
        "/api/v1/library/local-imports",
        "/api/v1/library/local-imports/retry",
        "/api/v1/library/folders/{kind}/open",
        "/api/v1/library/recent",
        "/api/v1/reader/series/{series_id}/chapters",
        "/api/v1/reader/progress/recent",
        "/api/v1/reader/library",
        "/api/v1/reader/library/preferences/{series_id}",
        "/api/v1/reader/library/bookmarks",
        "/api/v1/reader/library/bookmarks/{chapter_id}/{page_index}",
        "/api/v1/reader/library/import",
        "/api/v1/reader/{chapter_id}",
        "/api/v1/reader/{chapter_id}/open",
        "/api/v1/reader/{chapter_id}/progress",
        "/api/v1/reader/{chapter_id}/session",
        "/api/v1/reader/{chapter_id}/pages/{page}",
        "/api/v1/reader/{chapter_id}/download-source",
        "/api/v1/recommendations",
        "/api/v1/settings/reader",
        "/api/v1/settings/reader-cache",
        "/api/v1/connectors",
        "/api/v1/connectors/public-sources",
        "/api/v1/connectors/{connector_id}/search",
        "/api/v1/connectors/{connector_id}/series/{remote_series_id}/cover",
        "/api/v1/connectors/{connector_id}/series/{remote_series_id}/chapters",
        "/api/v1/connectors/{connector_id}/series/{remote_series_id}/availability",
        "/api/v1/connectors/{connector_id}/series/{remote_series_id}/download-jobs",
        "/api/v1/connectors/{connector_id}/series/{remote_series_id}/library-series",
        "/api/v1/connectors/{connector_id}/series/{remote_series_id}/reader-chapter",
        "/api/v1/download-jobs",
        "/api/v1/download-jobs/groups",
        "/api/v1/download-jobs/groups/{series_id}/pause",
        "/api/v1/download-jobs/{job_id}/archive",
        "/api/v1/download-jobs/{job_id}/restore",
        "/api/v1/downloads/{job_id}/cancel",
        "/api/v1/downloads/{job_id}/retry",
        "/api/v1/usb-exports",
        "/api/v1/usb-exports/targets",
        "/api/v1/usb-exports/pages",
        "/api/v1/usb-exports/chapters/{chapter_id}",
        "/api/v1/cbr-exports",
        "/api/v1/series/{series_id}",
        "/api/v1/series/{series_id}/cover",
        "/api/v1/series/{series_id}/metadata/refresh",
        "/api/v1/series/{series_id}/metadata/external-candidates",
        "/api/v1/series/{series_id}/metadata/external",
        "/api/v1/series/{series_id}/metadata-overrides",
        "/api/v1/series/{series_id}/metadata-overrides/{field}",
        "/api/v1/system/mobile",
        "/api/v1/system/mobile/enable",
        "/api/v1/system/mobile/startup",
        "/api/v1/system/mobile/disable",
        "/api/v1/system/mobile/firewall",
        "/api/v1/system/liveness",
        "/api/v1/system/readiness",
        "/api/v1/system/version",
        "/api/v1/system/diagnostics",
        "/api/v1/system/updates",
        "/api/v1/system/updates/check",
        "/api/v1/system/updates/install",
    }
    assert "503" in first_app.openapi()["paths"]["/api/v1/system/readiness"]["get"]["responses"]


def test_application_factory_accepts_typed_settings() -> None:
    settings = Settings(app_env="test", api_port=9000)
    app = create_app(settings)

    assert app.state.settings is settings
