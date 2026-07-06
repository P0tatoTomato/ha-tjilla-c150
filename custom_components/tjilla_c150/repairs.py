"""Repair issues — geeft de gebruiker actiebare instructies bij problemen."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# Issue keys
ISSUE_DEVICE_OFFLINE = "device_offline"
ISSUE_AUTH_FAILED = "auth_failed"
ISSUE_CLOUD_AUTH_EXPIRED = "cloud_auth_expired"
ISSUE_PROTOCOL_MISMATCH = "protocol_mismatch"


def create_device_offline_issue(
    hass: HomeAssistant, entry_id: str, device_name: str, host: str,
) -> None:
    """Maak een issue aan wanneer de stofzuiger lange tijd offline is."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_DEVICE_OFFLINE}_{entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_DEVICE_OFFLINE,
        translation_placeholders={
            "device_name": device_name,
            "host": host,
        },
    )


def create_auth_failed_issue(
    hass: HomeAssistant, entry_id: str, device_name: str,
) -> None:
    """Maak een issue aan bij authenticatie-fout (verkeerde local_key)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_AUTH_FAILED}_{entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_AUTH_FAILED,
        translation_placeholders={
            "device_name": device_name,
        },
    )


# create_cloud_auth_issue verwijderd (cloud weg).


def clear_issue(hass: HomeAssistant, entry_id: str, issue_key: str) -> None:
    """Verwijder een issue als het probleem is opgelost."""
    ir.async_delete_issue(hass, DOMAIN, f"{issue_key}_{entry_id}")


class TjillaRepairFlow(RepairsFlow):
    """Wizard die de gebruiker door een fix begeleidt."""

    def __init__(self, hass: HomeAssistant, issue_id: str, data: dict | None) -> None:
        self.hass = hass
        self.issue_id = issue_id
        self.data = data or {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if self.issue_id.startswith(ISSUE_DEVICE_OFFLINE):
            return await self.async_step_device_offline()
        if self.issue_id.startswith(ISSUE_AUTH_FAILED):
            return await self.async_step_auth_failed()
        return self.async_create_entry(title="", data={})

    async def async_step_device_offline(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Toon checklist voor offline device."""
        if user_input is not None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="device_offline",
            data_schema=vol.Schema({}),
        )

    async def async_step_auth_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Stuur gebruiker naar reconfigure flow voor nieuwe local_key."""
        if user_input is not None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="auth_failed",
            data_schema=vol.Schema({}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    return TjillaRepairFlow(hass, issue_id, data)
