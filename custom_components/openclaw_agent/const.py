"""Constants for the OpenClaw Agent (native) integration."""

DOMAIN = "openclaw_agent"

# Config keys
CONF_GATEWAY_URL = "gateway_url"
CONF_GATEWAY_TOKEN = "gateway_token"
CONF_AGENT_NAME = "agent_name"
CONF_SESSION_KEY = "session_key"
CONF_INCLUDE_INSTRUCTIONS = "include_instructions"
CONF_EXTRA_INSTRUCTIONS = "extra_instructions"

# Defaults
DEFAULT_GATEWAY_URL = "http://192.168.10.40:18789"
DEFAULT_AGENT_NAME = "nabu-home"
DEFAULT_SESSION_KEY = "casajaz-nabuhome"
DEFAULT_INCLUDE_INSTRUCTIONS = True

# Persistence
STORAGE_KEY = "openclaw_agent.chain"
STORAGE_VERSION = 1

# API
RESPONSES_PATH = "/v1/responses"
