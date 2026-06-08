"""
GPS / GNSS dongle detection for ISM miner pre-installation check.

Uses pyserial's serial.tools.list_ports to enumerate COM ports, then
optionally probes candidates with a quick NMEA sentence check.

This module **never raises**. All exceptions are caught and returned
as {"found": False, "error": "..."}.
"""

import logging
from typing import Dict, Any

_logger = logging.getLogger(__name__)

# Keywords in port description or hardware ID that suggest a GPS/GNSS receiver
_GPS_KEYWORDS = (
    "gps", "gnss", "nmea", "u-blox", "ublox",
    "ch340", "pl2303", "cp210", "cp2102", "cp2104", "ftdi",
    "prolific", "silicon labs", "globalsat", "navilock",
)


def _is_gps_candidate(port) -> bool:
    """Check if a serial port's metadata suggests a GPS receiver."""
    desc = (port.description or "").lower()
    hwid = (port.hwid or "").lower()
    combined = desc + " " + hwid
    return any(kw in combined for kw in _GPS_KEYWORDS)


def _nmea_probe(port_device: str, timeout: float) -> bool:
    """Open *port_device*, read for *timeout* seconds, check for NMEA prefixes."""
    try:
        import serial
    except ImportError:
        return False

    nmea_prefixes = (b"$GP", b"$GN", b"$GL", b"$GA", b"$BD")

    for baud in (115200, 57600, 38400, 9600, 4800):
        try:
            with serial.Serial(port_device, baud, timeout=timeout) as ser:
                data = ser.read(2048)
                if any(prefix in data for prefix in nmea_prefixes):
                    return True
        except Exception as _exc:
            _logger.debug("GPS probe failed on %s @ %d: %s", port_device, baud, _exc)
            continue
    return False


def detect_gps_dongle(timeout_per_port: float = 2.0) -> Dict[str, Any]:
    """Scan serial ports for GPS/GNSS receivers.

    Returns dict with keys:
      - found (bool): True if a GPS dongle was detected
      - port (str|None): device name of the first match (e.g. "COM3")
      - description (str|None): port description string
      - error (str|None): error message if scanning failed

    This function **never raises**.
    """
    try:
        from serial.tools.list_ports import comports
    except ImportError:
        return {"found": False, "port": None, "description": None,
                "error": "pyserial not available"}

    try:
        ports = list(comports())
    except Exception as exc:
        return {"found": False, "port": None, "description": None,
                "error": f"Port enumeration failed: {exc}"}

    # First pass: check port metadata for GPS keywords
    candidates = [p for p in ports if _is_gps_candidate(p)]

    # If no obvious candidates, try all ports (some GPS dongles have generic names)
    if not candidates:
        candidates = ports

    for port in candidates:
        try:
            if _nmea_probe(port.device, timeout_per_port):
                _logger.info("GPS dongle detected on %s (%s)", port.device, port.description)
                return {
                    "found": True,
                    "port": port.device,
                    "description": port.description,
                    "error": None,
                }
        except Exception:
            continue

    return {"found": False, "port": None, "description": None, "error": None}
