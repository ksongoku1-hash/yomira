from app.domain.job_backpressure import ResourceSample
from app.platform.windows_resources import (
    ResourceSamplingUnavailable,
    WindowsResourceSampler,
)


class FakeNativeResourceApi:
    def __init__(self) -> None:
        self.closed = False

    def read_memory(self) -> tuple[int, int]:
        return 1024, 4096

    def read_disk(self) -> tuple[float, float]:
        return 42.5, 12.0

    def close(self) -> None:
        self.closed = True


def test_windows_resource_sampler_maps_memory_and_disk_metrics() -> None:
    native = FakeNativeResourceApi()
    sampler = WindowsResourceSampler(native_api=native)

    sample = sampler.sample()

    assert isinstance(sample, ResourceSample)
    assert sample.available_memory_bytes == 1024
    assert sample.total_memory_bytes == 4096
    assert sample.disk_busy_percent == 42.5
    assert sample.disk_latency_ms == 12.0
    assert sample.sampled_at_monotonic > 0

    sampler.close()
    assert native.closed is True


def test_resource_sampler_preserves_stable_native_failure_reason() -> None:
    class FailedDiskApi(FakeNativeResourceApi):
        def read_disk(self) -> tuple[float, float]:
            raise ResourceSamplingUnavailable("PDH_COUNTERS_WARMING_UP", 1234)

    sampler = WindowsResourceSampler(native_api=FailedDiskApi())

    try:
        sampler.sample()
    except ResourceSamplingUnavailable as error:
        assert error.reason == "PDH_COUNTERS_WARMING_UP"
        assert error.native_error_code == 1234
    else:
        raise AssertionError("Unavailable disk counters were accepted as a valid sample")


def test_native_sampler_initializes_its_os_adapter_only_on_first_sample() -> None:
    created: list[FakeNativeResourceApi] = []

    def create_api() -> FakeNativeResourceApi:
        api = FakeNativeResourceApi()
        created.append(api)
        return api

    sampler = WindowsResourceSampler(native_api_factory=create_api)
    assert created == []
    sampler.sample()
    assert len(created) == 1
    sampler.close()
