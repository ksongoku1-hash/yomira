from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Generator
from contextlib import contextmanager
from ctypes import c_int
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from app.domain.asset import Asset, AssetType
from app.errors import ErrorCode
from app.services.usb_export import (
    ExportFailure,
    ExportTarget,
    UsbExportService,
    WindowsRemovableDriveProvider,
)

# These tests verify the private COM boundary directly, without widening the production API.
from app.services.wpd_export import (
    _FOLDER_CONTENT_TYPE,  # pyright: ignore[reportPrivateUsage]
    _GENERIC_FILE_CONTENT_TYPE,  # pyright: ignore[reportPrivateUsage]
    _OBJECT_PROPERTIES_GUID,  # pyright: ignore[reportPrivateUsage]
    PortableDeviceFile,
    PortableDeviceSession,
    WpdExportService,
    _classify_wpd_error,  # pyright: ignore[reportPrivateUsage]
    _create_values,  # pyright: ignore[reportPrivateUsage]
    _get_wpd_string_value,  # pyright: ignore[reportPrivateUsage]
    _load_wpd_modules,  # pyright: ignore[reportPrivateUsage]
    _property_key,  # pyright: ignore[reportPrivateUsage]
    _WindowsWpdSession,  # pyright: ignore[reportPrivateUsage]
)


@pytest.mark.skipif(os.name != "nt", reason="Windows COM calling convention")
@pytest.mark.parametrize("result_code", [0, -1])
def test_native_delete_marshals_one_id_and_checks_per_file_result(
    monkeypatch: pytest.MonkeyPatch,
    result_code: int,
) -> None:
    import ctypes

    import comtypes
    from comtypes.client import GetModule

    from app.services import wpd_export

    api = GetModule(("{1F001332-1A57-4934-BE31-AFFC99F4EE0A}", 1, 0))
    variant_type = cast(type[ctypes.Structure], api.tag_inner_PROPVARIANT)
    observed: list[str] = []

    class Collection(comtypes.COMObject):
        _com_interfaces_ = [api.IPortableDevicePropVariantCollection]

        def Add(self, value: Any) -> int:
            variant = ctypes.cast(value, ctypes.POINTER(variant_type)).contents
            assert variant.vt == 31
            observed.append(getattr(variant, variant._fields_[-1][0]).pwszVal)
            return 0

        def GetAt(self, index: int, output: Any) -> int:
            assert index == 0
            variant = variant_type()
            variant.vt = 10
            getattr(variant, variant._fields_[-1][0]).scode = result_code
            ctypes.memmove(output, ctypes.byref(variant), ctypes.sizeof(variant))
            return 0

    collection = Collection()
    pointer = collection.QueryInterface(api.IPortableDevicePropVariantCollection)

    class Content:
        def Properties(self) -> object:
            return object()

        def _IPortableDeviceContent__com_Delete(self, options: int, ids: Any, output: Any) -> int:
            assert options == 0 and ids == pointer
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
                pointer,
                ctypes.c_void_p,
            ).value
            # Match the reference that the real COM out parameter transfers to its caller.
            pointer.AddRef()
            return 0

    def create_collection(*args: object, **kwargs: object) -> object:
        return pointer

    monkeypatch.setattr(
        wpd_export, "_load_runtime_functions", lambda: (comtypes, create_collection)
    )
    types = SimpleNamespace(PortableDevicePropVariantCollection=object())
    session = _WindowsWpdSession(comtypes, api, types, Content())
    if result_code < 0:
        with pytest.raises(OSError, match="HRESULT"):
            session.delete_file("owned-partial")
    else:
        session.delete_file("owned-partial")
    assert observed == ["owned-partial"]


@dataclass
class _MemoryFile:
    object_id: str
    name: str
    content: bytes


class _MemoryWriteStream:
    def __init__(self, backend: _MemoryWpdBackend, name: str) -> None:
        self._backend = backend
        self._name = name
        self._content = bytearray()

    def write(self, content: bytes) -> int:
        self._content.extend(content)
        return len(content)

    def commit(self) -> str:
        object_id = f"object-{len(self._backend.files) + 1}"
        self._backend.files[object_id] = _MemoryFile(object_id, self._name, bytes(self._content))
        return object_id

    def close(self) -> None:
        return None


class _MemoryWpdSession:
    def __init__(self, backend: _MemoryWpdBackend) -> None:
        self._backend = backend

    def __enter__(self) -> _MemoryWpdSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def ensure_folder(self, parent_object_id: str, name: str) -> str:
        assert parent_object_id == self._backend.target.storage_object_id
        assert name == "Books"
        return "books-folder"

    def list_files(self, storage_object_id: str) -> tuple[PortableDeviceFile, ...]:
        assert storage_object_id == "books-folder"
        return tuple(
            PortableDeviceFile(item.object_id, item.name, len(item.content))
            for item in self._backend.files.values()
        )

    def get_file(self, object_id: str) -> PortableDeviceFile:
        item = self._backend.files[object_id]
        return PortableDeviceFile(item.object_id, item.name, len(item.content))

    def create_file(
        self,
        storage_object_id: str,
        name: str,
        size_bytes: int,
    ) -> _MemoryWriteStream:
        assert storage_object_id == "books-folder"
        assert size_bytes >= 0
        return _MemoryWriteStream(self._backend, name)

    def open_read(self, object_id: str) -> io.BytesIO:
        return io.BytesIO(self._backend.files[object_id].content)

    def rename_file(self, object_id: str, name: str) -> None:
        self._backend.files[object_id].name = name

    def delete_file(self, object_id: str) -> None:
        del self._backend.files[object_id]


class _MemoryWpdBackend:
    def __init__(self, target: ExportTarget) -> None:
        self.target = target
        self.files: dict[str, _MemoryFile] = {}

    def list_targets(self) -> tuple[ExportTarget, ...]:
        return (self.target,)

    @contextmanager
    def open_session(self, target: ExportTarget) -> Generator[PortableDeviceSession]:
        assert target == self.target
        yield _MemoryWpdSession(self)


def _target() -> ExportTarget:
    return ExportTarget(
        target_id="wpd:opaque-device-target",
        mount_path=None,
        display_name="B6 Color - Almacenamiento interno compartido",
        file_system="MTP/WPD",
        total_bytes=1024 * 1024,
        free_bytes=1024 * 1024,
        transport="wpd",
        portable_device_id="USB\\PRIVATE-DEVICE-ID",
        storage_object_id="private-storage-object-id",
        storage_persistent_id="private-persistent-id",
    )


def _asset(path: Path, content: bytes) -> Asset:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return Asset.new_local(
        AssetType.CBZ,
        "chapters/chapter-1.cbz",
        hashlib.sha256(content).hexdigest(),
        len(content),
        verified_at=datetime.now(UTC),
    )


def _export_service(
    library_root: Path,
    backend: _MemoryWpdBackend,
) -> UsbExportService:
    exporter = WpdExportService(backend)
    return UsbExportService(
        target_provider=backend,
        portable_exporter=exporter,
        library_root=library_root,
        copy_chunk_bytes=7,
    )


def test_wpd_export_target_hides_windows_device_identifiers() -> None:
    target = _target()

    assert target.transport == "wpd"
    assert target.mount_path is None
    assert "PRIVATE-DEVICE-ID" not in repr(target)
    assert "private-storage-object-id" not in repr(target)
    assert "private-persistent-id" not in repr(target)


@pytest.mark.parametrize(
    "existing,content_type",
    [
        (0, "folder"),
        (1, "folder"),
        (2, "folder"),
        (1, "file"),
    ],
)
def test_books_folder_is_created_or_reused_and_conflicts_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    existing: int,
    content_type: str,
) -> None:
    from app.services import wpd_export

    created: list[object] = []
    native = wpd_export.ctypes
    folder_type = _FOLDER_CONTENT_TYPE

    class Values:
        def SetStringValue(self, key: Any, value: str) -> None:
            return None

        def SetGuidValue(self, key: Any, value: str) -> None:
            assert value == folder_type

        def GetGuidValue(self, key: Any) -> str:
            return folder_type if content_type == "folder" else "file"

    class _Properties:
        def GetValues(self, *_args: object) -> Values:
            return Values()

    class Content:
        def Properties(self) -> _Properties:
            return _Properties()

        def CreateObjectWithPropertiesOnly(self, values: object) -> str:
            created.append(values)
            return "books-id"

    def fake_key(*_args: object) -> c_int:
        return native.c_int()

    def fake_values(*_args: object) -> Values:
        return Values()

    def storage_id(*_args: object) -> str:
        return "storage-id"

    def guid(value: str) -> str:
        return value

    def files(parent: str) -> tuple[PortableDeviceFile, ...]:
        return tuple(PortableDeviceFile("books-id", "Books", 0) for _ in range(existing))

    monkeypatch.setattr(wpd_export, "_property_key", fake_key)
    monkeypatch.setattr(wpd_export, "_create_values", fake_values)
    monkeypatch.setattr(wpd_export, "_get_wpd_string_value", storage_id)
    session = _WindowsWpdSession(
        SimpleNamespace(GUID=guid),
        object(),
        SimpleNamespace(PortableDeviceValues=object()),
        Content(),
    )
    monkeypatch.setattr(session, "list_files", files)
    if existing > 1 or content_type != "folder":
        with pytest.raises(ValueError):
            session.ensure_folder("storage-id", "Books")
        assert created == []
    else:
        assert session.ensure_folder("storage-id", "Books") == "books-id"
        assert len(created) == (1 if existing == 0 else 0)


def test_removable_target_provider_includes_portable_device_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import usb_export

    monkeypatch.setattr(usb_export, "_enumerate_logical_drives", lambda: ())

    class _PortableProvider:
        def list_targets(self) -> tuple[ExportTarget, ...]:
            return (_target(),)

    targets = WindowsRemovableDriveProvider(_PortableProvider()).list_targets()

    assert targets == (_target(),)


def test_windows_wpd_releases_device_before_uninitializing_com(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    events: list[str] = []

    class _Comtypes:
        @staticmethod
        def CoInitialize() -> None:
            events.append("initialize")

        @staticmethod
        def CoUninitialize() -> None:
            events.append("uninitialize")

    class _Manager:
        def RefreshDeviceList(self) -> None:
            return None

    class _Device:
        def Close(self) -> None:
            events.append("close")

        def __del__(self) -> None:
            events.append("release")

    def create_device_manager(_api: Any) -> _Manager:
        return _Manager()

    def get_device_ids(_manager: Any) -> tuple[str, ...]:
        return ("device",)

    def open_device(_api: Any, _types: Any, _device_id: str) -> _Device:
        return _Device()

    def read_device_targets(*_args: Any) -> list[ExportTarget]:
        return []

    monkeypatch.setattr(wpd_export, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(wpd_export.ctypes, "WinDLL", object(), raising=False)
    monkeypatch.setattr(
        wpd_export,
        "_load_wpd_modules",
        lambda: (_Comtypes, object(), object()),
    )
    monkeypatch.setattr(wpd_export, "_create_device_manager", create_device_manager)
    monkeypatch.setattr(wpd_export, "_get_pnp_device_ids", get_device_ids)
    monkeypatch.setattr(wpd_export, "_open_device", open_device)
    monkeypatch.setattr(wpd_export, "_read_device_targets", read_device_targets)

    assert wpd_export.WindowsWpdBackend().list_targets() == ()

    assert events.index("release") < events.index("uninitialize")


def test_windows_wpd_closes_session_interfaces_before_uninitializing_com(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    events: list[str] = []

    class _Comtypes:
        @staticmethod
        def CoInitialize() -> None:
            events.append("initialize")

        @staticmethod
        def CoUninitialize() -> None:
            events.append("uninitialize")

    class _Properties:
        def __del__(self) -> None:
            events.append("properties_release")

    class _Content:
        def Properties(self) -> _Properties:
            return _Properties()

        def __del__(self) -> None:
            events.append("content_release")

    class _Device:
        def Content(self) -> _Content:
            return _Content()

        def Close(self) -> None:
            events.append("close")

        def __del__(self) -> None:
            events.append("device_release")

    def open_device(*_args: Any) -> _Device:
        return _Device()

    def resolve_storage(
        _api: Any,
        _content: Any,
        _persistent_id: str,
        _original_id: str | None,
    ) -> str:
        return "storage"

    monkeypatch.setattr(wpd_export, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(wpd_export.ctypes, "WinDLL", object(), raising=False)
    monkeypatch.setattr(
        wpd_export,
        "_load_wpd_modules",
        lambda: (_Comtypes, object(), object()),
    )
    monkeypatch.setattr(wpd_export, "_open_device", open_device)
    monkeypatch.setattr(wpd_export, "_resolve_storage_object_id", resolve_storage)

    with wpd_export.WindowsWpdBackend().open_session(_target()) as session:
        assert session.storage_object_id == "storage"

    assert events.index("properties_release") < events.index("close")
    assert events.index("content_release") < events.index("close")
    assert events.index("close") < events.index("device_release")
    assert events.index("content_release") < events.index("uninitialize")
    assert events.index("properties_release") < events.index("uninitialize")
    assert events.index("device_release") < events.index("uninitialize")


def _stub_wpd_get_devices(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reported_count: int,
    device_names: tuple[str, ...],
    second_call_result: int = 0,
) -> tuple[list[int], list[int], Any]:
    from app.services import wpd_export

    native = wpd_export.ctypes
    real_cast = native.cast
    manager = SimpleNamespace()
    capacities: list[int] = []
    freed: list[int] = []
    allocated_names: list[Any] = []

    class _TaskMemFree:
        argtypes: object = ()
        restype: object = None

        def __call__(self, pointer: Any) -> None:
            freed.append(int(pointer.value))

    free = _TaskMemFree()

    def get_devices(device_ids: Any, count_pointer: Any) -> int:
        count = native.cast(count_pointer, native.POINTER(native.c_ulong)).contents
        if device_ids is None:
            count.value = reported_count
            return 0

        capacity = int(count.value)
        capacities.append(capacity)
        output = native.cast(device_ids, native.POINTER(native.c_wchar_p))
        actual_count = min(capacity, len(device_names))
        for index, name in enumerate(device_names[:actual_count]):
            buffer = native.create_unicode_buffer(name)
            allocated_names.append(buffer)
            output[index] = real_cast(buffer, native.c_wchar_p)
        count.value = actual_count
        return second_call_result

    def load_library(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(CoTaskMemFree=free)

    setattr(manager, "_IPortableDeviceManager__com_GetDevices", get_devices)  # noqa: B010
    monkeypatch.setattr(native, "WinDLL", load_library, raising=False)
    return capacities, freed, manager


def test_wpd_device_enumeration_bounds_driver_reported_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    device_names = tuple(f"device-{index}" for index in range(wpd_export.MAX_WPD_DEVICES))
    capacities, freed, manager = _stub_wpd_get_devices(
        monkeypatch,
        reported_count=100_000,
        device_names=device_names,
        second_call_result=1,
    )

    get_pnp_device_ids: Any = getattr(wpd_export, "_get_pnp_device_ids")  # noqa: B009
    result = get_pnp_device_ids(manager)

    assert capacities == [wpd_export.MAX_WPD_DEVICES]
    assert result == device_names
    assert len(freed) == wpd_export.MAX_WPD_DEVICES


def test_wpd_device_enumeration_frees_partial_outputs_on_hresult_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    capacities, freed, manager = _stub_wpd_get_devices(
        monkeypatch,
        reported_count=2,
        device_names=("device-1",),
        second_call_result=-1,
    )

    with pytest.raises(OSError, match="HRESULT"):
        get_pnp_device_ids: Any = getattr(wpd_export, "_get_pnp_device_ids")  # noqa: B009
        get_pnp_device_ids(manager)

    assert capacities == [2]
    assert len(freed) == 1


def _install_cotaskmem_free_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from app.services import wpd_export

    freed: list[int] = []

    class _TaskMemFree:
        argtypes: object = ()
        restype: object = None

        def __call__(self, pointer: Any) -> None:
            freed.append(int(pointer.value))

    free = _TaskMemFree()

    def load_library(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(CoTaskMemFree=free)

    monkeypatch.setattr(
        wpd_export.ctypes,
        "WinDLL",
        load_library,
        raising=False,
    )
    return freed


@pytest.mark.parametrize(("hresult", "expected"), ((0, "Storage"), (-1, None)))
def test_wpd_property_string_is_freed_even_after_partial_hresult_failure(
    monkeypatch: pytest.MonkeyPatch,
    hresult: int,
    expected: str | None,
) -> None:
    from app.services import wpd_export

    native = wpd_export.ctypes
    real_cast = native.cast
    freed = _install_cotaskmem_free_spy(monkeypatch)
    buffer = native.create_unicode_buffer("Storage")
    allocated_string = real_cast(buffer, native.c_wchar_p)
    address = real_cast(allocated_string, native.c_void_p).value
    assert address is not None
    pointer_value = int(address)

    class _Values:
        def GetStringValue(self, _key: object) -> str:
            return "Storage"

    values = _Values()

    def get_string_value(_key: Any, output: Any) -> int:
        native.cast(output, native.POINTER(native.c_void_p))[0] = pointer_value
        return hresult

    setattr(values, "_IPortableDeviceValues__com_GetStringValue", get_string_value)  # noqa: B010

    optional_string: Any = getattr(wpd_export, "_optional_string")  # noqa: B009
    result = optional_string(values, native.c_int(1))

    assert result == expected
    assert freed == [pointer_value]


def _stub_wpd_object_enumerator(
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...],
    *,
    failing_index: int | None = None,
) -> tuple[Any, list[int]]:
    from app.services import wpd_export

    native = wpd_export.ctypes
    real_cast = native.cast
    freed = _install_cotaskmem_free_spy(monkeypatch)

    class _Enumerator:
        def __init__(self) -> None:
            self.index = 0
            self.allocations: list[Any] = []

        def next_native(self, requested: int, output_ids: Any, fetched_pointer: Any) -> int:
            fetched = real_cast(fetched_pointer, native.POINTER(native.c_ulong)).contents
            if self.index >= len(names):
                fetched.value = 0
                return 1
            output = real_cast(output_ids, native.POINTER(native.c_wchar_p))
            value = names[self.index]
            buffer = native.create_unicode_buffer(value)
            self.allocations.append(buffer)
            output[0] = real_cast(buffer, native.c_wchar_p)
            current_index = self.index
            self.index += 1
            fetched.value = 1
            return -1 if current_index == failing_index else 0

        def Next(self, requested: int) -> tuple[str | None, int]:
            output_ids = (native.c_void_p * requested)()
            fetched = native.c_ulong(requested)
            result = self.next_native(
                requested,
                real_cast(output_ids, native.POINTER(native.c_wchar_p)),
                native.byref(fetched),
            )
            if result < 0:
                raise OSError("HRESULT failure from IEnumPortableDeviceObjectIDs::Next")
            return (
                native.wstring_at(output_ids[0]) if fetched.value else None,
                int(fetched.value),
            )

    enumerator = _Enumerator()
    setattr(  # noqa: B010
        enumerator,
        "_IEnumPortableDeviceObjectIDs__com_Next",
        enumerator.next_native,
    )

    def enum_objects(*_args: Any) -> Any:
        return enumerator

    content = SimpleNamespace(EnumObjects=enum_objects)
    return content, freed


def test_wpd_object_enumerator_frees_each_returned_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    content, freed = _stub_wpd_object_enumerator(
        monkeypatch,
        ("storage-1", "storage-2"),
    )

    enumerate_children: Any = getattr(wpd_export, "_enumerate_children")  # noqa: B009
    result = enumerate_children(content, "DEVICE", limit=2)

    assert result == ("storage-1", "storage-2")
    assert len(freed) == 2


def test_wpd_object_enumerator_frees_partial_id_when_hresult_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    content, freed = _stub_wpd_object_enumerator(
        monkeypatch,
        ("partial-id",),
        failing_index=0,
    )

    with pytest.raises(OSError, match="HRESULT"):
        enumerate_children: Any = getattr(wpd_export, "_enumerate_children")  # noqa: B009
        enumerate_children(content, "DEVICE", limit=1)

    assert len(freed) == 1


def test_wpd_enumeration_logs_classified_numeric_error_without_device_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from app.services import wpd_export

    class _PortableDeviceError(OSError):
        winerror = 64

    error = _PortableDeviceError("private-device-id must not be logged")
    with caplog.at_level(logging.WARNING, logger="yomira.usb_export"):
        log_failure: Any = getattr(wpd_export, "_log_wpd_enumeration_failure")  # noqa: B009
        log_failure(error)

    record = next(
        item for item in caplog.records if item.message == "usb_export.portable_target_unavailable"
    )
    assert record.__dict__["error_code"] == "EXP_DEVICE_DISCONNECTED"
    assert record.__dict__["retryable"] is True
    assert record.__dict__["stage"] == "target_enumeration"
    assert record.__dict__["winerror"] == 64
    assert record.__dict__["trace_id"]
    assert "private-device-id" not in caplog.text


@pytest.mark.parametrize("hresult", (0, -1))
def test_wpd_committed_object_id_is_freed_even_after_partial_hresult_failure(
    monkeypatch: pytest.MonkeyPatch,
    hresult: int,
) -> None:
    from comtypes import GUID
    from comtypes.client import GetModule

    from app.services import wpd_export

    native = wpd_export.ctypes
    real_cast = native.cast
    freed = _install_cotaskmem_free_spy(monkeypatch)
    runtime = SimpleNamespace(GUID=GUID)
    buffer = native.create_unicode_buffer("committed-object")
    allocated_id = real_cast(buffer, native.c_wchar_p)
    address = real_cast(allocated_id, native.c_void_p).value
    assert address is not None
    pointer_value = int(address)

    class _Stream:
        def QueryInterface(self, _interface: Any) -> Any:
            return SimpleNamespace(_IPortableDeviceDataStream__com_GetObjectID=get_object_id)

    def get_object_id(output: Any) -> int:
        native.cast(output, native.POINTER(native.c_void_p))[0] = pointer_value
        return hresult

    object_id_function_name = "_get_object_id_from_committed_stream"
    get_object_id_from_stream: Any = getattr(wpd_export, object_id_function_name)

    api = GetModule(("{1F001332-1A57-4934-BE31-AFFC99F4EE0A}", 1, 0))

    if hresult < 0:
        with pytest.raises(OSError, match="HRESULT"):
            get_object_id_from_stream(
                runtime,
                _Stream(),
                api.IStream,
            )
    else:
        assert (
            get_object_id_from_stream(
                runtime,
                _Stream(),
                api.IStream,
            )
            == "committed-object"
        )

    assert freed == [pointer_value]


@pytest.mark.parametrize("raises", (False, True))
def test_wpd_create_file_frees_partial_cookie(
    monkeypatch: pytest.MonkeyPatch,
    raises: bool,
) -> None:
    from app.services import wpd_export

    native = wpd_export.ctypes
    real_cast = native.cast
    freed = _install_cotaskmem_free_spy(monkeypatch)
    buffer = native.create_unicode_buffer("unused-cookie")
    allocated_cookie = real_cast(buffer, native.c_wchar_p)
    address = real_cast(allocated_cookie, native.c_void_p).value
    assert address is not None
    pointer_value = int(address)

    class _Stream(native.Structure):
        _fields_ = [("marker", native.c_int)]

    stream_record = _Stream()
    stream_pointer = native.pointer(stream_record)

    class _Properties:
        def __getattr__(self, _name: str) -> Any:
            def setter(*_args: Any) -> None:
                return None

            return setter

    class _Content:
        def Properties(self) -> object:
            return object()

        def _IPortableDeviceContent__com_CreateObjectWithPropertiesAndData(
            self,
            _properties: Any,
            _stream_output: Any,
            buffer_size_output: Any,
            cookie_output: Any,
        ) -> int:
            native.cast(
                _stream_output,
                native.POINTER(native.c_void_p),
            )[0] = real_cast(stream_pointer, native.c_void_p).value
            native.cast(
                buffer_size_output,
                native.POINTER(native.c_ulong),
            ).contents.value = 4096
            native.cast(cookie_output, native.POINTER(native.c_void_p))[0] = pointer_value
            return -1 if raises else 0

    def make_guid(_value: str) -> object:
        return object()

    def make_property_key(*_args: Any) -> object:
        return native.c_int(1)

    def create_values(*_args: Any) -> _Properties:
        return _Properties()

    session_type: Any = getattr(wpd_export, "_WindowsWpdSession")  # noqa: B009
    session = session_type(
        SimpleNamespace(GUID=make_guid),
        SimpleNamespace(
            _tagpropertykey=make_property_key,
            IStream=_Stream,
        ),
        SimpleNamespace(PortableDeviceValues=object()),
        _Content(),
    )
    monkeypatch.setattr(wpd_export, "_create_values", create_values)
    monkeypatch.setattr(
        wpd_export,
        "_property_key",
        make_property_key,
    )

    if raises:
        with pytest.raises(OSError, match="HRESULT"):
            session.create_file("storage", "chapter.cbz", 42)
    else:
        assert session.create_file("storage", "chapter.cbz", 42) is not None

    assert freed == [pointer_value]


@pytest.mark.parametrize("reported_bytes", (9, 3))
def test_wpd_write_stream_uses_comtypes_remote_write_contract(
    reported_bytes: int,
) -> None:
    from app.services import wpd_export

    written_payloads: list[bytes] = []

    class _Stream:
        def RemoteWrite(self, buffer: Any, size: int) -> int:
            written_payloads.append(bytes(buffer[:size]))
            return reported_bytes

    writer_type: Any = getattr(wpd_export, "_WindowsWpdWriteStream")  # noqa: B009
    writer = writer_type(object(), _Stream(), object())

    assert writer.write(b"CBZ chunk") == reported_bytes
    assert written_payloads == [b"CBZ chunk"]


def test_wpd_write_stream_preserves_comtypes_write_errors() -> None:
    from app.services import wpd_export

    class _Stream:
        def RemoteWrite(self, _buffer: Any, _size: int) -> int:
            raise OSError("Windows Portable Devices returned HRESULT 0x80070005.")

    writer_type: Any = getattr(wpd_export, "_WindowsWpdWriteStream")  # noqa: B009
    writer = writer_type(object(), _Stream(), object())

    with pytest.raises(OSError, match="HRESULT 0x80070005"):
        writer.write(b"CBZ chunk")


def test_wpd_short_write_fails_closed_without_committing_or_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "library"
    content = b"verified CBZ content that must not be truncated"
    source_path = library_root / "chapters" / "chapter-1.cbz"
    asset = _asset(source_path, content)
    backend = _MemoryWpdBackend(_target())

    class _ShortWriteStream(_MemoryWriteStream):
        def write(self, content: bytes) -> int:
            short_content = content[:-1]
            self._content.extend(short_content)
            return len(short_content)

    def create_short_write_stream(
        session: _MemoryWpdSession,
        storage_object_id: str,
        name: str,
        size_bytes: int,
    ) -> _ShortWriteStream:
        assert storage_object_id == "books-folder"
        assert size_bytes == len(content)
        return _ShortWriteStream(backend, name)

    monkeypatch.setattr(_MemoryWpdSession, "create_file", create_short_write_stream)
    exporter = WpdExportService(backend)

    with pytest.raises(ExportFailure) as raised:
        exporter.export_asset(
            asset,
            source_path=source_path,
            target=backend.target,
            export_id=uuid4(),
            trace_id="short-write-regression",
        )

    assert raised.value.code is ErrorCode.EXP_COPY_FAILED
    assert raised.value.stage == "copy"
    assert backend.files == {}
    assert source_path.read_bytes() == content


@pytest.mark.parametrize(
    ("failure_phase", "expected_stage"),
    (("create", "create_staging"), ("commit", "commit_staging")),
)
def test_wpd_export_attributes_com_argument_errors_to_native_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    expected_stage: str,
) -> None:
    from ctypes import ArgumentError

    library_root = tmp_path / "library"
    content = b"verified CBZ content"
    source_path = library_root / "chapters" / "chapter-1.cbz"
    asset = _asset(source_path, content)
    backend = _MemoryWpdBackend(_target())

    class _CommitFailureStream(_MemoryWriteStream):
        def commit(self) -> str:
            raise ArgumentError("bad COM argument")

    def create_file(
        session: _MemoryWpdSession,
        storage_object_id: str,
        name: str,
        size_bytes: int,
    ) -> _MemoryWriteStream:
        assert storage_object_id == "books-folder"
        assert size_bytes == len(content)
        if failure_phase == "create":
            raise ArgumentError("bad COM argument")
        return _CommitFailureStream(backend, name)

    monkeypatch.setattr(_MemoryWpdSession, "create_file", create_file)
    exporter = WpdExportService(backend)

    with pytest.raises(ExportFailure) as raised:
        exporter.export_asset(
            asset,
            source_path=source_path,
            target=backend.target,
            export_id=uuid4(),
            trace_id="wpd-stage-regression",
        )

    assert raised.value.code is ErrorCode.EXP_COPY_FAILED
    assert raised.value.stage == expected_stage
    assert source_path.read_bytes() == content


def test_wpd_export_verifies_temporary_object_before_final_name(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    content = b"verified CBZ content" * 100
    asset = _asset(library_root / "chapters" / "chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())
    service = _export_service(library_root, backend)

    result = service.export_asset(asset, target_id=backend.target.target_id)

    assert result.status == "EXPORTED"
    assert result.sha256 == asset.sha256
    assert result.size_bytes == asset.size_bytes
    assert [(item.name, item.content) for item in backend.files.values()] == [
        ("chapter-1.cbz", content)
    ]
    assert (library_root / "chapters" / "chapter-1.cbz").read_bytes() == content


def test_wpd_export_refuses_existing_different_file_without_changing_it(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    asset = _asset(library_root / "chapters" / "chapter-1.cbz", b"new chapter")
    backend = _MemoryWpdBackend(_target())
    existing = b"reader's existing file"
    backend.files["existing-id"] = _MemoryFile("existing-id", "chapter-1.cbz", existing)
    service = _export_service(library_root, backend)

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id=backend.target.target_id)

    assert captured.value.code == "EXP_DESTINATION_CONFLICT"
    assert captured.value.trace_id
    assert backend.files["existing-id"].content == existing
    assert len(backend.files) == 1


def test_wpd_export_skips_identical_destination_only_after_readback(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    content = b"already present"
    asset = _asset(library_root / "chapters" / "chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())
    backend.files["existing-id"] = _MemoryFile("existing-id", "chapter-1.cbz", content)
    service = _export_service(library_root, backend)

    result = service.export_asset(asset, target_id=backend.target.target_id)

    assert result.status == "SKIPPED"
    assert len(backend.files) == 1


def test_wpd_export_preserves_staging_object_when_readback_hash_fails(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    content = b"verified source content" * 30
    asset = _asset(library_root / "chapters" / "chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())

    class _CorruptReadbackSession(_MemoryWpdSession):
        def open_read(self, object_id: str) -> io.BytesIO:
            return io.BytesIO(b"corrupted readback")

    @contextmanager
    def open_corrupt_session(
        _target: ExportTarget,
    ) -> Generator[_CorruptReadbackSession]:
        yield _CorruptReadbackSession(backend)

    backend.open_session = open_corrupt_session  # type: ignore[method-assign]
    service = _export_service(library_root, backend)

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id=backend.target.target_id)

    assert captured.value.code == "EXP_HASH_MISMATCH"
    assert captured.value.stage == "verify_staging"
    assert captured.value.staging_name
    assert len(backend.files) == 1
    assert backend.files[next(iter(backend.files))].name.endswith(".part")


@pytest.mark.skipif(os.name != "nt", reason="Windows COM interfaces required")
def test_wpd_values_accept_the_api_property_keys() -> None:
    from app.services import wpd_export

    runtime, api, types = _load_wpd_modules()
    runtime.CoInitialize()
    values = None
    try:
        values = _create_values(runtime, api, types.PortableDeviceValues)
        name_key = _property_key(runtime, api, _OBJECT_PROPERTIES_GUID, 4)
        size_key = _property_key(runtime, api, _OBJECT_PROPERTIES_GUID, 11)
        type_key = _property_key(runtime, api, _OBJECT_PROPERTIES_GUID, 7)
        values.SetStringValue(wpd_export.ctypes.byref(name_key), "Capítulo de prueba.cbz")
        values.SetUnsignedLargeIntegerValue(wpd_export.ctypes.byref(size_key), 42)
        values.SetGuidValue(
            wpd_export.ctypes.byref(type_key),
            runtime.GUID(_GENERIC_FILE_CONTENT_TYPE),
        )
        assert _get_wpd_string_value(values, name_key) == "Capítulo de prueba.cbz"
        assert values.GetUnsignedLargeIntegerValue(wpd_export.ctypes.byref(size_key)) == 42
    finally:
        values = None
        runtime.CoUninitialize()


def test_wpd_read_stream_passes_an_integer_buffer_size_to_comtypes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    class ReadStream:
        def RemoteRead(self, size: int) -> tuple[bytes, int]:
            return b"page", 4

    class _Resources:
        def GetStream(self, object_id: str, key: Any, mode: int, buffer_size: int) -> Any:
            assert object_id == "chapter-object"
            assert type(buffer_size) is int
            assert buffer_size == wpd_export.COPY_CHUNK_BYTES
            return buffer_size, ReadStream()

    class _Content:
        def Properties(self) -> None:
            return None

        def Transfer(self) -> _Resources:
            return _Resources()

    def fake_key(*_args: object) -> wpd_export.ctypes.c_int:
        return wpd_export.ctypes.c_int()

    monkeypatch.setattr(wpd_export, "_property_key", fake_key)
    session = _WindowsWpdSession(object(), object(), object(), _Content())
    with session.open_read("chapter-object") as stream:
        assert stream.read(4) == b"page"


@pytest.mark.parametrize("name_error,file_error", [(0, 0), (-1, 0), (0, -1), (-1, -1)])
def test_wpd_rename_checks_each_property_result(
    monkeypatch: pytest.MonkeyPatch,
    name_error: int,
    file_error: int,
) -> None:
    from app.services import wpd_export

    native = wpd_export.ctypes
    requested: dict[int, str] = {}

    def key_id(key: Any) -> int:
        return int(native.cast(key, native.POINTER(native.c_int)).contents.value)

    class _Values:
        def SetStringValue(self, key: Any, name: str) -> None:
            requested[key_id(key)] = name

    class _Results:
        def GetErrorValue(self, key: Any) -> int:
            return name_error if key_id(key) == 4 else file_error

    class _Properties:
        def SetValues(self, object_id: str, values: Any) -> _Results:
            assert object_id == "chapter-object"
            return _Results()

    class _Content:
        def Properties(self) -> _Properties:
            return _Properties()

    def fake_key(_runtime: object, _api: object, _guid: str, pid: int) -> c_int:
        return native.c_int(pid)

    def fake_values(*_args: object) -> _Values:
        return _Values()

    monkeypatch.setattr(wpd_export, "_property_key", fake_key)
    monkeypatch.setattr(wpd_export, "_create_values", fake_values)
    session = _WindowsWpdSession(
        object(),
        object(),
        SimpleNamespace(PortableDeviceValues=object()),
        _Content(),
    )
    if name_error < 0 and file_error < 0:
        with pytest.raises(OSError, match="HRESULT"):
            session.rename_file("chapter-object", "Capítulo 1.cbz")
    else:
        session.rename_file("chapter-object", "Capítulo 1.cbz")
    assert requested == {4: "Capítulo 1.cbz", 12: "Capítulo 1.cbz"}


def test_wpd_uses_published_name_when_original_import_name_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import wpd_export

    def free_task_memory(_pointer: Any) -> None:
        return None

    def fake_cotaskmem_library(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(CoTaskMemFree=free_task_memory)

    monkeypatch.setattr(
        wpd_export.ctypes,
        "WinDLL",
        fake_cotaskmem_library,
        raising=False,
    )

    class _PropertyKey(wpd_export.ctypes.Structure):
        _fields_ = [("value", wpd_export.ctypes.c_int)]

    class _Values:
        def __init__(self) -> None:
            self._name = wpd_export.ctypes.create_unicode_buffer("chapter-1.cbz")

        def _IPortableDeviceValues__com_GetStringValue(
            self,
            _key: object,
            output: Any,
        ) -> int:
            pointer = wpd_export.ctypes.cast(
                self._name,
                wpd_export.ctypes.c_void_p,
            ).value
            wpd_export.ctypes.cast(
                output,
                wpd_export.ctypes.POINTER(wpd_export.ctypes.c_void_p),
            )[0] = pointer
            return 0

        def GetUnsignedLargeIntegerValue(self, _key: object) -> int:
            return 42

    class _Properties:
        def GetValues(self, _object_id: str, _keys: None) -> _Values:
            return _Values()

    class _Content:
        def Properties(self) -> _Properties:
            return _Properties()

    def fake_property_key(*_args: Any) -> _PropertyKey:
        return _PropertyKey()

    monkeypatch.setattr(wpd_export, "_property_key", fake_property_key)
    session = _WindowsWpdSession(object(), object(), object(), _Content())

    remote_file = session.get_file("object-1")

    assert remote_file.name == "chapter-1.cbz"
    assert remote_file.size_bytes == 42


@pytest.mark.parametrize("hresult", [0x802A0006, 0x8007001F])
def test_wpd_unresponsive_device_is_recoverable(hresult: int) -> None:
    class DeviceError(Exception):
        pass

    error = DeviceError("Reader stopped responding")
    error.hresult = hresult - 0x100000000  # type: ignore[attr-defined]
    assert _classify_wpd_error(error) == (
        ErrorCode.EXP_DEVICE_DISCONNECTED,
        True,
    )


def test_wpd_retry_reuses_verified_staging_even_with_no_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"verified chapter"
    asset = _asset(tmp_path / "chapters/chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())
    creates = 0
    original_create = _MemoryWpdSession.create_file
    original_rename = _MemoryWpdSession.rename_file

    def create(
        session: _MemoryWpdSession,
        storage: str,
        name: str,
        size: int,
    ) -> _MemoryWriteStream:
        nonlocal creates
        creates += 1
        return original_create(session, storage, name, size)

    def failed_rename(_session: _MemoryWpdSession, _id: str, _name: str) -> None:
        raise OSError("Device stopped responding before publishing")

    monkeypatch.setattr(_MemoryWpdSession, "create_file", create)
    monkeypatch.setattr(_MemoryWpdSession, "rename_file", failed_rename)
    service = _export_service(tmp_path, backend)
    with pytest.raises(ExportFailure):
        service.export_asset(asset, target_id=backend.target.target_id)
    assert [item.name for item in backend.files.values()] == [
        f".yomira-{asset.asset_id.hex}.part",
    ]
    backend.target = replace(backend.target, free_bytes=0)
    monkeypatch.setattr(_MemoryWpdSession, "rename_file", original_rename)
    assert service.export_asset(asset, target_id=backend.target.target_id).status == "EXPORTED"
    assert creates == 1
    assert [(item.name, item.content) for item in backend.files.values()] == [
        ("chapter-1.cbz", content),
    ]


@pytest.mark.parametrize("partial", [b"", b"veri", b"verified"])
def test_wpd_retry_replaces_only_its_verified_partial(tmp_path: Path, partial: bytes) -> None:
    content = b"verified chapter"
    asset = _asset(tmp_path / "chapters/chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())
    backend.files["partial"] = _MemoryFile(
        "partial",
        f".yomira-{asset.asset_id.hex}.part",
        partial,
    )
    backend.files["unrelated"] = _MemoryFile("unrelated", ".yomira-old.part", b"user data")
    service = _export_service(tmp_path, backend)
    assert service.export_asset(asset, target_id=backend.target.target_id).status == "EXPORTED"
    assert backend.files["unrelated"].content == b"user data"
    assert sorted((item.name, item.content) for item in backend.files.values()) == [
        (".yomira-old.part", b"user data"),
        ("chapter-1.cbz", content),
    ]


@pytest.mark.parametrize("staged_content", [b"wrong prefix", b"wrong content!!!"])
def test_wpd_retry_preserves_a_mismatching_temporal(tmp_path: Path, staged_content: bytes) -> None:
    content = b"verified chapter"
    asset = _asset(tmp_path / "chapters/chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())
    backend.files["partial"] = _MemoryFile(
        "partial",
        f".yomira-{asset.asset_id.hex}.part",
        staged_content,
    )
    service = _export_service(tmp_path, backend)
    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id=backend.target.target_id)
    assert captured.value.stage == "resume_staging"
    assert captured.value.code == ErrorCode.EXP_HASH_MISMATCH
    assert len(backend.files) == 1
    assert backend.files["partial"].content == staged_content


def test_wpd_retry_rejects_duplicate_temporals(tmp_path: Path) -> None:
    content = b"verified chapter"
    asset = _asset(tmp_path / "chapters/chapter-1.cbz", content)
    backend = _MemoryWpdBackend(_target())
    for object_id in ("first", "second"):
        backend.files[object_id] = _MemoryFile(
            object_id,
            f".yomira-{asset.asset_id.hex}.part",
            content,
        )
    with pytest.raises(ExportFailure) as captured:
        _export_service(tmp_path, backend).export_asset(asset, target_id=backend.target.target_id)
    assert captured.value.code == ErrorCode.EXP_DESTINATION_CONFLICT
    assert len(backend.files) == 2


def test_wpd_retry_preserves_partial_moved_outside_books(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset(tmp_path / "chapters/chapter-1.cbz", b"verified chapter")
    backend = _MemoryWpdBackend(_target())
    backend.files["partial"] = _MemoryFile("partial", f".yomira-{asset.asset_id.hex}.part", b"veri")
    original_list = _MemoryWpdSession.list_files
    calls = 0

    def list_files(session: _MemoryWpdSession, storage: str) -> tuple[PortableDeviceFile, ...]:
        nonlocal calls
        calls += 1
        # The object still exists but was moved out of the destination after verification.
        return original_list(session, storage) if calls < 3 else ()

    monkeypatch.setattr(_MemoryWpdSession, "list_files", list_files)
    with pytest.raises(ExportFailure) as raised:
        _export_service(tmp_path, backend).export_asset(asset, target_id=backend.target.target_id)
    assert raised.value.code == ErrorCode.EXP_DESTINATION_CONFLICT
    assert backend.files["partial"].content == b"veri"
