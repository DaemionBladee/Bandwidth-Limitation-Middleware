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
2. A parser converts the received accounting messages into the middleware's supported append-only RADIUS JSONL records. rsyslog reception alone does not perform this conversion. The current branch has no raw Cisco syslog parser.
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

Once the parser exists, configure `ACCOUNTING_SOURCE=radius-jsonl` and `ACCOUNTING_FILE` to its normalized output, not the raw rsyslog file. Grant the service account read access and retain the processed prefix required by the checkpoint. The normalization stage must preserve stable event IDs, session uniqueness, counter direction, and any required syslog-fragment reassembly.

## Collect logs before the parser is available

The [rsyslog receiver example](../deploy/rsyslog/30-bandwidth-engine.conf) captures JSON envelopes containing the original syslog message, sender IP, reported timestamp, and receipt timestamp. It allows messages up to 128 KiB; review that global rsyslog limit against your host's existing configuration. It does not interpret counters, create subscribers, or issue ANC requests. Its file is a raw capture, not the accounting adapter's input format.

Install the receiver on the Ubuntu server:

```bash
sudo apt install rsyslog
sudo install -d -o syslog -g syslog -m 0750 /var/log/bandwidth-engine
sudo install -m 0644 deploy/rsyslog/30-bandwidth-engine.conf /etc/rsyslog.d/30-bandwidth-engine.conf
sudo rsyslogd -N1
sudo systemctl restart rsyslog
```

The example initially listens only on **127.0.0.1 TCP port 5514** and accepts the loopback sender. Test capture locally:

```bash
logger --tcp --server 127.0.0.1 --port 5514 --tag bandwidth-capture-test 'Receiver connectivity test; not an accounting measurement'
sudo tail -n 1 /var/log/bandwidth-engine/ise-raw.jsonl
```

For actual ISE collection, edit the input's `address` to the Ubuntu server's appropriate interface address and the ruleset's sender check to the trusted ISE node address. Allow additional legitimate sending nodes explicitly if needed. Configure that same transport/port as the ISE logging target, permit only those senders through the firewall, and validate/restart rsyslog again. The TCP example has no TLS settings; use the actual deployment's supported secure transport where required. If ISE is configured for UDP, change to the corresponding `imudp` module/input and test that transport instead of assuming it matches TCP.

Enable the relevant accounting logging categories and interim updates in the actual ISE/WLC setup. Verify that the received messages contain transfer counters; authentication-only logs will not establish a quota total. Preserve all message fragments and their sequence fields. Keep a representative redacted capture for the parser implementation; do not treat the local connectivity-test message as a Cisco accounting sample.

Raw capture retention/rotation can be managed separately from future normalized accounting output. The latter must satisfy the adapter's append-only checkpoint rules. Keep live enforcement on mock while the required log parser, endpoint identity mapping, ANC body, and confirmation mapping remain incomplete.
