from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "get_devices",
    "get_ssd_devices",
    "get_hdd_devices",
    "read_raw_smart",
    "ssd_check",
    "hdd_check",
    "storage_check",
]

try:
    from pySMART import Device, DeviceList  # type: ignore
    from pySMART.smartctl import Smartctl  # type: ignore
    try:
        from pySMART.utils import smartctl_type  # type: ignore
    except Exception:  # pragma: no cover - very old pySMART
        def smartctl_type(interface):  # type: ignore
            return interface
    _PYSMART_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover - import-time fallback
    Device = Any  # type: ignore
    DeviceList = None  # type: ignore
    Smartctl = None  # type: ignore
    smartctl_type = None  # type: ignore
    _PYSMART_ERROR = exc


def _make_smartctl(use_sudo: bool):
    """Build a pySMART Smartctl wrapper, optionally via ``sudo -E``.

    With ``use_sudo=True`` smartctl runs as ``sudo -E smartctl ...``,
    so the user is prompted for their (root) password when elevation is
    needed. Ignored on non-POSIX systems (pySMART logs a warning there).
    """
    if Smartctl is None:
        raise RuntimeError("pySMART is not available")
    if not use_sudo:
        return Smartctl()
    return Smartctl(sudo=True)


def _want_sudo(use_sudo: Optional[bool]) -> bool:
    """Resolve ``None`` to "sudo unless already root"."""
    if use_sudo is not None:
        return bool(use_sudo)
    try:
        import os
        if hasattr(os, "geteuid"):
            return os.geteuid() != 0
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Device discovery / classification
# ---------------------------------------------------------------------------

def get_devices(use_sudo: Optional[bool] = None) -> list:
    """Return all storage devices detected via pySMART.

    Args:
        use_sudo: Run smartctl via ``sudo -E`` (prompts for the root
            password when needed). ``None`` (default) means "sudo unless
            already running as root". Pass ``False`` to never elevate.

    Raises:
        RuntimeError: If pySMART/smartctl is unavailable or the scan fails.
    """
    if _PYSMART_ERROR is not None or DeviceList is None:
        raise RuntimeError(
            "pySMART is not available "
            f"({_PYSMART_ERROR}); install it with 'pip install pySMART' "
            "and make sure 'smartctl' (smartmontools) is on PATH."
        )
    try:
        smartctl = _make_smartctl(_want_sudo(use_sudo))
        return list(DeviceList(smartctl=smartctl).devices)
    except Exception as exc:
        raise RuntimeError(
            f"Unable to enumerate storage devices: {exc}. "
            "Hint: SMART queries usually need root/admin privileges "
            "(a sudo password prompt is expected) "
            "and smartmontools installed."
        ) from exc


def _iface(dev) -> str:
    for attr in ("dev_interface", "interface", "_interface"):
        try:
            val = getattr(dev, attr, None)
            if callable(val):
                continue
            if val:
                return str(val).lower()
        except Exception:
            continue
    return ""


def is_ssd(dev) -> bool:
    """Best-effort SSD vs HDD classification for a pySMART Device."""
    try:
        if bool(getattr(dev, "is_ssd", False)):
            return True
    except Exception:
        pass
    iface = _iface(dev)
    if "nvme" in iface:
        return True
    try:
        rr = getattr(dev, "rotation_rate", None)
        if isinstance(rr, (int, float)) and rr and rr > 0:
            return False  # spinning disk -> HDD
    except Exception:
        pass
    model = f"{getattr(dev, 'model', '') or ''} {getattr(dev, 'family', '') or ''}".lower()
    if any(k in model for k in ("ssd", "nvme", "solid state")):
        return True
    return False


def device_kind(dev) -> str:
    """Return 'nvme-ssd', 'sata-ssd', 'hdd' or 'unknown'."""
    iface = _iface(dev)
    if "nvme" in iface:
        return "nvme-ssd"
    if is_ssd(dev):
        return "sata-ssd"
    # rotation rate present or classic ATA spinning disk
    if iface in ("ata", "sata", "sat", "scsi", "sas", "csmi"):
        return "hdd"
    return "unknown"


def get_ssd_devices(devices: Optional[list] = None,
                    use_sudo: Optional[bool] = None) -> list:
    """Filter (or freshly enumerate) SSD/NVMe devices."""
    devs = devices if devices is not None else get_devices(use_sudo=use_sudo)
    return [d for d in devs if is_ssd(d)]


def get_hdd_devices(devices: Optional[list] = None,
                    use_sudo: Optional[bool] = None) -> list:
    """Filter (or freshly enumerate) spinning-disk (HDD) devices."""
    devs = devices if devices is not None else get_devices(use_sudo=use_sudo)
    return [d for d in devs if not is_ssd(d)]


# ---------------------------------------------------------------------------
# Raw SMART dumps
# ---------------------------------------------------------------------------

_KIND_LABEL = {
    "nvme-ssd": "NVMe SSD",
    "sata-ssd": "SATA SSD",
    "hdd": "HDD",
    "unknown": "Drive",
}

_RULE_HEAVY = "=" * 78
_RULE_LIGHT = "-" * 78


def _dev_path(dev) -> str:
    for attr in ("dev_reference", "path", "name"):
        try:
            val = getattr(dev, attr, None)
            val = val() if callable(val) else val
            if val:
                return str(val)
        except Exception:
            continue
    return "unknown device"


def _dev_title(dev) -> str:
    """Short identity line used in the boundary header (no health verdict)."""
    try:
        model = (getattr(dev, "model", None) or "Unknown model").strip()
    except Exception:
        model = "Unknown model"
    kind = device_kind(dev)
    return f"{_dev_path(dev)} — {model} ({_KIND_LABEL.get(kind, kind)})"


def read_raw_smart(dev) -> str:
    """Return the raw ``smartctl --all`` output for a single device.

    Uses the device's own smartctl wrapper (which carries the sudo setting
    from enumeration), falling back to a non-elevated wrapper.

    Raises:
        RuntimeError: If the raw data cannot be read.
    """
    try:
        ref = _dev_path(dev)
        iface = _iface(dev) or None
        if iface is not None and smartctl_type is not None:
            try:
                iface = smartctl_type(iface)
            except Exception:
                pass
        wrapper = getattr(dev, "smartctl", None)
        if wrapper is None:
            wrapper = _make_smartctl(False)
        lines = wrapper.all(ref, iface)
        text = "\n".join(lines).strip()
        if not text:
            raise RuntimeError("smartctl returned no output")
        return text
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Unable to read SMART data: {exc}") from exc


def _raw_section(title: str, devs: list) -> str:
    """Frame each drive's raw dump with a boundary header and footer."""
    lines = [f"{title} (raw SMART data, "
             f"{len(devs)} drive{'s' if len(devs) != 1 else ''}):",
             ""]
    if not devs:
        lines.append("  No drives of this type detected.")
        return "\n".join(lines)
    blocks = []
    total = len(devs)
    for index, dev in enumerate(devs, start=1):
        block = [
            _RULE_HEAVY,
            f"[{index}/{total}] {_dev_title(dev)}",
            _RULE_HEAVY,
        ]
        try:
            block.append(read_raw_smart(dev))
        except RuntimeError as exc:
            block.append(f"(could not read SMART data: {exc})")
        except Exception as exc:  # pragma: no cover - defensive
            block.append(f"(could not read SMART data: {exc})")
        block.append(_RULE_LIGHT)
        blocks.append("\n".join(block))
    lines.append("\n\n".join(blocks))
    return "\n".join(lines).rstrip()


def _checked_devices(kind_filter: str,
                     use_sudo: Optional[bool] = None) -> tuple[list | None, str | None]:
    """Return (devices, error_string). error_string is None on success."""
    try:
        devs = get_devices(use_sudo=use_sudo)
    except RuntimeError as exc:
        return None, f"Storage check failed: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Storage check failed: {exc}"
    if not devs:
        return [], (
            "No storage devices detected. "
            "Hint: SMART queries usually need root/admin privileges "
            "(a sudo password prompt is expected when not root) "
            "and smartmontools ('smartctl') installed."
        )
    if kind_filter == "ssd":
        return [d for d in devs if is_ssd(d)], None
    if kind_filter == "hdd":
        return [d for d in devs if not is_ssd(d)], None
    return devs, None


def ssd_check(use_sudo: Optional[bool] = None) -> str:
    """Raw SMART dumps for SSD/NVMe drives. Never raises.

    Args:
        use_sudo: Elevate smartctl via ``sudo -E`` (password prompt when
            needed). Defaults to "sudo unless already root".
    """
    devs, err = _checked_devices("ssd", use_sudo=use_sudo)
    if err is not None and devs is None:
        return err
    assert devs is not None
    if err is not None:  # no devices at all
        return "SSD Health:\n  " + err
    if not devs:
        return "SSD Health:\n  No SSD/NVMe drives detected on this system."
    try:
        return "SSD Health:\n" + _raw_section("SSDs", devs)
    except Exception as exc:  # pragma: no cover - defensive
        return f"SSD check failed: {exc}"


def hdd_check(use_sudo: Optional[bool] = None) -> str:
    """Raw SMART dumps for spinning (HDD) drives. Never raises.

    Args:
        use_sudo: Elevate smartctl via ``sudo -E`` (password prompt when
            needed). Defaults to "sudo unless already root".
    """
    devs, err = _checked_devices("hdd", use_sudo=use_sudo)
    if err is not None and devs is None:
        return err
    assert devs is not None
    if err is not None:  # no devices at all
        return "HDD Health:\n  " + err
    if not devs:
        return "HDD Health:\n  No HDD (spinning) drives detected on this system."
    try:
        return "HDD Health:\n" + _raw_section("HDDs", devs)
    except Exception as exc:  # pragma: no cover - defensive
        return f"HDD check failed: {exc}"


def storage_check(use_sudo: Optional[bool] = None) -> str:
    """Raw SMART dumps for ALL drives (SSDs + HDDs). Never raises.

    Args:
        use_sudo: Elevate smartctl via ``sudo -E`` (password prompt when
            needed). Defaults to "sudo unless already root".
    """
    devs, err = _checked_devices("all", use_sudo=use_sudo)
    if err is not None and devs is None:
        return err
    assert devs is not None
    if err is not None:
        return "Storage Health:\n  " + err
    try:
        return "Storage Health:\n" + _raw_section("Drives", devs)
    except Exception as exc:  # pragma: no cover - defensive
        return f"Storage check failed: {exc}"
