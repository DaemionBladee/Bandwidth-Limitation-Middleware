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

## Planned rsyslog input and ANC enforcement

The selected deployment receives logs through **rsyslog on Ubuntu Server** and uses the operator-supplied **`PUT /ers/config/ancendpoint/apply`** operation on the ISE ERS service at port **9060**. The actual appliance version, request body, policy mappings, and session confirmation are still to be supplied and inspected. Providing this route does not complete or enable the live integration.

The intended flow is:

1. The WLC/ISE accounting infrastructure sends logs to the configured rsyslog target. Select the supported transport, port, and trusted senders for the actual deployment.
2. The included [rsyslog accounting parser](rsyslog-parser.md) reads the raw receiver envelopes through `ACCOUNTING_SOURCE=rsyslog`. It supports the documented CISE/RADIUS formats, sender validation, and fragment reassembly; verify the actual deployment's messages. Existing independent RADIUS JSONL exporters remain supported.
3. The worker registers unknown accounting identities using the enabled default plan, normalizes upload/download counters, and evaluates the current allowance.
4. At/above quota, the worker requests LIMITED and queues enforcement. The live adapter applies the verified ANC policy to the correctly resolved endpoint(s).
5. ISE authorization rules map that ANC state to an authorization profile containing the WLC-supported speed attributes. Required reauthorization and active-session readback establish whether the intended profile is actually applied.
6. At reset, the middleware requests NORMAL. Deployment configuration must explicitly establish whether that means applying a NORMAL ANC policy or clearing the limited ANC policy, and verify the resulting active-session profile.

The ANC apply operation selects a policy; it does not by itself accept arbitrary download/upload speeds from a middleware quota plan. Each configured speed pair needs an existing, verified ISE/WLC mapping. For the demo allowance that means normal **20,000/10,000 kbps** and limited **2,000/1,000 kbps**. Do not use quarantine or another disruptive ANC action as a substitute for a verified rate-limiting policy.

Configure connection values in the server's untracked `.env`:

```dotenv
ISE_BASE_URL=https://<ISE-IP-or-certificate-hostname>:9060
ISE_USERNAME=<ANC-authorized-ERS-username>
ISE_PASSWORD=<ERS-password>
ISE_CA_BUNDLE=/etc/bandwidth-engine/ise-ca.pem
ISE_CONTRACT_FILE=/etc/bandwidth-engine/ise-contract.json
```

Keep `ISE_MODE=mock` until the live contract and identity mapping are complete. The certificate must validate for the configured IP/hostname. `ISE_USERNAME` and `ISE_PASSWORD` are the original credentials: the HTTP client builds Basic authorization itself. It sends `Accept: application/json`; JSON mutation bodies also set `Content-Type: application/json`. Do not put a pre-encoded Authorization header, passwords, or tokens in the contract.

After inspection, the contract's `update.method` can select `PUT` and `update.path` can select `/ers/config/ancendpoint/apply`, with the **actual appliance-supported request body**. Do not set verification flags based on the route alone. The current adapter has username/profile/speed bindings, but no MAC/IP endpoint binding or multi-device session resolution; extend it using verified identities from the real accounting/session data before using an ANC body that requires those identifiers. Do not substitute a username for a MAC or IP address.

To complete the implementation, supply these redacted deployment details:

- **ISE version/patch**, ANC-enabled API capabilities, and a representative apply request body and response.
- **Received accounting log samples**, including Start/Interim/Stop where available. Retain field names for username, MAC/IP, NAS, stable session/event identifiers, timestamp, upload/download counters, and any message sequence/fragment fields. Authentication or disconnect messages without transfer counters cannot establish byte usage.
- **Existing ANC policy names and authorization mappings** for NORMAL and LIMITED, including the intended reset/removal action and whether ANC triggers the required reauthorization automatically.
- **Supported readback** that confirms the policy/profile on every relevant active endpoint/session. HTTP success or an ANC assignment alone is not proof that the WLC has applied the requested speeds.

For direct capture ingestion, configure `ACCOUNTING_SOURCE=rsyslog`, `ACCOUNTING_FILE` to the raw capture, and `RSYSLOG_ALLOWED_SENDERS` to the trusted nodes. Grant the service account read access and retain the append-only source required by its checkpoint. Preflight the capture and verify event/session/counter semantics using the [parser guide](rsyslog-parser.md). Use `radius-jsonl` only for a separate exporter that already writes the supported RADIUS measurement schema.

<a id="collect-logs-before-the-parser-is-available"></a>

## Capture and parser setup

Follow the **[rsyslog installation and middleware integration guide](rsyslog-setup.md)** for the complete procedure on CachyOS and Ubuntu Server: installation, configuration directories/includes, service ownership, local tests, TCP/UDP port selection, trusted senders, firewall rules, ISE forwarding, normalized accounting, and troubleshooting.

The shared [receiver example](../deploy/rsyslog/30-bandwidth-engine.conf) starts with loopback-only TCP port `5514` and Ubuntu file ownership. The guide adapts ownership/read permissions for CachyOS and Ubuntu. The included parser connects supported raw messages to accounting; verified ANC identity/confirmation mappings remain integration work. Raw captures are valid input for `rsyslog`, while the other JSONL adapters require their own documented schemas.
