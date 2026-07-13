"""小米云集成配置流程."""

import voluptuous as vol
from homeassistant import config_entries

def _mask_username(username: str) -> str:
    """脱敏用户名，手机号前3后4，其余前3."""
    if not username or len(username) <= 3:
        return "***"
    if len(username) >= 7:
        return username[:3] + "*" * (len(username) - 7) + username[-4:]
    return username[:3] + "*" * (len(username) - 3)


from .const import (
    DOMAIN,
    CONF_ENDPOINT,
    DEFAULT_ENDPOINT,
    CONF_GAODE_APIKEY,
    CONF_ENABLE_GAODE_MORE_INFO,
    DEFAULT_ENABLE_GAODE_MORE_INFO,
    CONF_ACTIVE_LOCATE,
    DEFAULT_ACTIVE_LOCATE,
)


class XiaomiCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """小米云集成配置流程."""
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return XiaomiCloudOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            username = user_input["username"]
            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"小米云-{_mask_username(username)}",
                data={
                    "username":                  username,
                    "password":                  user_input["password"],
                    CONF_ENDPOINT:               user_input.get(CONF_ENDPOINT, DEFAULT_ENDPOINT).strip().rstrip("/"),
                    CONF_GAODE_APIKEY:           user_input.get(CONF_GAODE_APIKEY, ""),
                    CONF_ENABLE_GAODE_MORE_INFO: user_input.get(CONF_ENABLE_GAODE_MORE_INFO, DEFAULT_ENABLE_GAODE_MORE_INFO),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ENDPOINT): str,
                vol.Required("username"): str,
                vol.Required("password"): str,
                vol.Optional(CONF_GAODE_APIKEY, default=""): str,
                vol.Optional(CONF_ENABLE_GAODE_MORE_INFO, default=DEFAULT_ENABLE_GAODE_MORE_INFO): bool,
            }),
            errors=errors,
        )


class XiaomiCloudOptionsFlow(config_entries.OptionsFlow):
    """处理选项更新."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    def _get(self, key, default):
        return self._config_entry.options.get(
            key, self._config_entry.data.get(key, default)
        )

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data={
                "username":                  user_input.get("username", self._config_entry.data.get("username", "")),
                "password":                  user_input.get("password", self._config_entry.data.get("password", "")),
                CONF_ENDPOINT:               user_input.get(CONF_ENDPOINT, self._get(CONF_ENDPOINT, DEFAULT_ENDPOINT)).strip().rstrip("/"),
                CONF_ACTIVE_LOCATE:          user_input.get(CONF_ACTIVE_LOCATE, DEFAULT_ACTIVE_LOCATE),
                CONF_GAODE_APIKEY:           user_input.get(CONF_GAODE_APIKEY, ""),
                CONF_ENABLE_GAODE_MORE_INFO: user_input.get(CONF_ENABLE_GAODE_MORE_INFO, DEFAULT_ENABLE_GAODE_MORE_INFO),
            })

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_ENDPOINT, default=self._get(CONF_ENDPOINT, DEFAULT_ENDPOINT)): str,
                vol.Optional("username", default=self._config_entry.data.get("username", "")): str,
                vol.Optional("password", default=self._config_entry.data.get("password", "")): str,
                vol.Optional(
                    CONF_ACTIVE_LOCATE,
                    default=self._get(CONF_ACTIVE_LOCATE, DEFAULT_ACTIVE_LOCATE),
                ): bool,
                vol.Optional(CONF_GAODE_APIKEY, default=self._get(CONF_GAODE_APIKEY, "")): str,
                vol.Optional(
                    CONF_ENABLE_GAODE_MORE_INFO,
                    default=self._get(CONF_ENABLE_GAODE_MORE_INFO, DEFAULT_ENABLE_GAODE_MORE_INFO),
                ): bool,
            }),
        )
