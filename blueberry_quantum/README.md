# BlueberryQuantum

BlueberryQuantum is a separate local application and Home Assistant integration for multiple gateway and DNS providers. It includes a Quantum G3100 CGI adapter, a BlueberryProxy DNS adapter and an external HTTP JSON adapter. Verizon-specific code stays here. BlueberryProxy remains independent.

Install the app from the generated `dist/apps` repository and open its Home Assistant Ingress panel. Add a G3100 provider using `https://192.168.1.1`, username `admin` and your gateway administration password. Select the self-signed certificate option if required by your router. Verification is configurable per provider and remains enabled by default. Credentials are saved in the app's private data directory with mode 0600 and omitted from provider-list responses. Back up this directory as confidential data.

Start with reads. Use Inspect gateway SPA to inspect the router's own public assets, then enable writes in provider settings when you want DNS or port-forward management. Saving settings resets the session, so inspect again afterward. DNS and forwarding changes first produce a five-minute review plan. Applying consumes the plan and rejects it if the resource snapshot changed. The returned snapshot shows the subsequent state. A successful submission alone does not establish that the gateway persisted a change.

The G3100 adapter implements challenge authentication, known-device inventory, primary LAN CIDR discovery, IPv4 DNS host entries and single-port TCP, UDP or combined forwarding. The gateway's advertised DNS form-field identifiers are read dynamically. It creates or deletes forwarding rules, and creates, updates or deletes DNS host entries. Forward ranges, schedules, firewall policy changes, DHCP DNS assignment and IPv6 DNS writes are not implemented. Existing raw forwarding records retain the gateway's port-rule references.

The read-only probe against the requested gateway successfully collected 91 public Vue/Webpack assets and confirmed login, DNS and forwarding request construction. Authentication and writes have been tested against a simulated gateway using those observed schemas. No real gateway password was available to this development session, so live authenticated inventory and writes remain unvalidated. Authentication is attempted once per configured session. Use Sign in again to retry after fixing credentials, rather than repeatedly submitting a rejected password during polling.

Add a BlueberryProxy provider using the proxy's reachable API URL, normally `http://LAN-IP:8055`. Select the gateway and that proxy, then import inventory or scan the advertised CIDRs. Imported IP/MAC pairs retain inventory-source evidence, and UScan publishes discovery events. Routed CIDRs support unicast scanning when reachable. mDNS, WSD and ONVIF multicast remain subject to local interface and router multicast boundaries. If the router does not advertise its subnet, enter explicit CIDRs in provider settings.

Copy the app API token using the Ingress panel's Home Assistant connection token section. Install the integration from `dist/repo/blueberry_quantum`, add BlueberryQuantum in Home Assistant with the app URL and token, then add a `custom:blueberry-quantum-card`. The card selects providers, manages provider configuration, inspects the SPA, reviews and applies DNS/NAT plans, and imports or scans inventory through BlueberryProxy. Administrative WebSocket requests require an HA administrator. Provider device, DNS and forwarding counts are exposed as sensors. Those counts describe inventory, not device control entities.

Standalone execution uses Python 3.11 or later.

```sh
python -m pip install -r sre/BlueberryQuantum/backend/requirements.txt
PYTHONPATH=sre/BlueberryQuantum/backend QUANTUM_DATA=./quantum-data python -m blueberry_quantum.server
```

The app listens on port 8060. Direct access requires the token in `quantum-data/api-token`. Ingress bootstrap accepts only the HA Ingress proxy TCP peer `172.30.32.2`; it ignores forwarded IP headers. If your installation uses a different proxy source address, use the API token directly. The browser keeps the token in memory. Do not publish the app port on the Internet.

A separate read-only collector downloads only same-origin public JavaScript assets, does not execute JavaScript and does not sign in.

```sh
PYTHONPATH=sre/BlueberryQuantum/backend python -m blueberry_quantum.collect --url https://192.168.1.1 --self-signed --output /tmp/quantum-spa-report.json
```

External providers use kind `http_json` and implement `GET /v1/snapshot`, `POST /v1/dns`, `DELETE /v1/dns/{id}`, `POST /v1/forwards` and `DELETE /v1/forwards/{id}`. A snapshot supplies `status`, `devices`, `networks`, `dns`, `forwards` and `capabilities`. Capabilities include `dns_read`, `dns_write`, `forwards_read`, `forwards_write` and `devices`. Devices carry observed `ip`, optional `mac` and `hostname`. Networks are explicit IPv4 CIDRs. DNS values use `name` and `ip`. Forward values use `name`, `private_ip`, `protocol`, `external_port` and `internal_port`. An optional bearer token is stored privately. Multiple external providers may be registered at runtime without changing the proxy or Quantum code.

Protocol research references include the gateway's public SPA and the [quantum_gateway project](https://github.com/cisasteelersfan/quantum_gateway). This adapter uses original code based on observed wire formats.

## Eero and per-provider DNS

The HA card now shows DNS host records for the selected provider, with add, edit and delete controls. Each change is reviewed against that provider's revision. Switching providers clears the review, and backend reconfiguration or Eero network selection invalidates pending plans. Unsupported and read-only capabilities disable the corresponding controls.

Eero is available as kind `eero`, with email/phone verification, saved-token authentication and an account-network selector in both the app and HA card. It supplies connected client inventory, network CIDRs where advertised, forwarding controls and a separate upstream DNS form. Eero upstream DNS changes can reboot the gateway and preserve the existing IPv6 DNS configuration. See [BlueberryEero](https://git.crit.rip/BlueberryFaygo/BlueberryFaygo/src/branch/main/sre/BlueberryQuantum/BlueberryEero/README.md) for the extracted contracts and current validation limits.

Publication is configured in the workspace's `repo.config.json` and handled by `scripts/deployment-migrate-auto`. The correct app-store repository is the published `BlueberryFaygo/apps` repository. See [deployment instructions](https://git.crit.rip/BlueberryFaygo/BlueberryFaygo/src/branch/main/scripts/DEPLOYMENT.md) for token requirements and mirror configuration.
