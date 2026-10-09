# Fixture capture

`codex_tools_0160.json` was captured from Codex CLI 0.160.1.
The CLI sent one Responses request to a temporary local LiteLLM callback.
The callback wrote only the tools array and stopped the request before upstream handling.
The fixture retains tool types, names, namespace children, and JSON Schemas.
Descriptions and non-schema metadata were removed during sanitization.
No request body, credentials, or personal paths are included.
`automation_update_anyof.json` is a hand-written composed-schema fixture for F2 coverage.
