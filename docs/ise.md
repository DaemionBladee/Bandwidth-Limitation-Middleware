# ISE deployment contract

No live ISE configuration is present. The live adapter is a guarded integration framework, not a claim that any Cisco version supports an invented endpoint or direct per-user bandwidth assignment. The mock adapter is fully runnable and isolated in `mock_ise_states`.

Before live mode, inspect the deployment to establish:

1. Exact appliance version/patch, enabled ERS/OpenAPI/MnT APIs, authentication, CA chain, and supported authorization-update and CoA mechanisms. Use the appliance's own API documentation and current configuration.
2. Accounting-to-ISE identity/session mapping, NAS capabilities, authorization rules, and all sessions that must change.
3. Existing NORMAL/LIMITED authorization profiles for each speed pair. Verify that actual NAS-supported attributes produce the specified rates. The engine does not configure WLC QoS or manufacture Cisco attributes.
4. The supported identity/policy update selecting those profiles, whether reauthorization is required, and repeatability after a timeout/crash.
5. Readback proving the active session has the target profile: matching identity, session ID, and active status. A desired user attribute or submitted policy is insufficient.

After inspection, copy `ise.contract.example.json` to a local, untracked path such as `data/ise-contract.json`; replace placeholders with verified paths, JSON schemas, field mappings, and exact version. Set verification flags true only after the corresponding checks. The example is deliberately unusable; its placeholders are not Cisco endpoints.

Set `ISE_MODE=live`, `ISE_BASE_URL=https://<appliance>:<port>`, `ISE_USERNAME`, `ISE_PASSWORD`, and an absolute `ISE_CONTRACT_FILE`. Set `ISE_CA_BUNDLE` for a private PEM trust bundle. TLS verification cannot be disabled. Restart API/workers after environment changes; adapter changes queue a new generation. Inspect reconciliation attempts in the dashboard.

The current adapter supports **HTTPS Basic authentication and JSON REST responses**, one normalized active-session object per subscriber, and appliance-relative paths with optional `{username}`. Payloads support `{username}`, `{profile}`, `{mode}`, `{download_kbps}`, `{upload_kbps}`, `{period_id}`, and `{version_id}`. Paths URL-encode identity; payloads serialize as JSON. Field selectors are dotted object keys. Redirects are disabled. The exact configured version is checked before mutations on every attempt. Speed mapping is exact; unmapped speeds fail before network I/O.

If the actual deployment uses XML MnT/CoA, OAuth, session/MAC-based paths, or multiple simultaneous sessions, **extend the adapter for that verified interface before live use**. Do not mark a mismatched contract verified or use an arbitrary first session as confirmation. An existing trusted gateway may expose this normalized contract; no gateway is installed or asserted here. Deployment-specific Cisco endpoints and multi-session handling remain integration work until the actual deployment is known.

Reconciliation reads the session first. If already confirmed, no update is repeated. Otherwise it sends the configured update, optional required reauthorization, and reads back again. HTTP success without session confirmation remains pending. Inactive/missing sessions are not called applied. Errors exclude upstream bodies, headers, and credentials. Retries are durable and bounded per burst; later reconciliation checks failures and drift.

Keep contract files read-only to the service account and inject credentials through environment settings or a secret store. Do not put secrets in templates or saved setup instructions. Test with an authorized test identity and record real active-session readback before claiming live enforcement.
