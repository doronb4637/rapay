"""
What the connection modal needs to offer DDS choices instead of free text:
core's deployment defaults, and the units a DDS Interface defines.

`def`, not `async def`: loading an Interface imports user code (and Connext).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from gsim.core_gateway import dds_defaults, dds_units

router = APIRouter(prefix="/api/dds", tags=["dds"])


@router.get("/defaults")
def defaults() -> dict[str, Any]:
    """The domain id and QoS file core uses when a config leaves them out."""
    return dds_defaults()


@router.get("/units")
def units(interface: str) -> dict[str, Any]:
    """Every unit `interface` defines, with its unit code and topics."""
    try:
        return dds_units(interface.strip())
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - an Interface is user code; say why it failed
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"could not load the DDS Interface: {type(exc).__name__}: {exc}") from exc
