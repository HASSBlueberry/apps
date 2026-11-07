# BlueberryQuantum

BlueberryQuantum is an independent local app and Home Assistant integration for gateway and DNS providers. Protocol adapters own their configuration schemas, resource schemas, current values, capabilities and setup actions. Both Ingress and the HA card use the same renderer. Neither frontend embeds a provider list, provider URLs, settings fields or router-specific forms.

Install the generated app from `https://git.crit.rip/BlueberryFaygo/apps` and open its Ingress panel. In Provider settings, select a provider type and enter its advertised fields. Configuration changes retain blank credentials only at the same provider type and URL. Changing destinations clears old credentials. Credentials are saved privately with mode 0600 and omitted from configuration responses.

For G3100, the provider supplies the default `https://192.168.1.1` URL and `admin` username. Enter the router administration password. Disable certificate verification for that provider if its gateway uses a self-signed certificate. Enable supported writes and save the selected provider. The adapter automatically inspects its firmware before advertising writable controls. Updating the name, write flag or discovery networks retains the existing session and inspection. Changing connection settings starts a new session and inspection. Inspect provider retries a failed inspection. Eero requires verified HTTPS and offers verification-code sign-in through provider-defined actions.

Each settings section displays the provider's current values. Supported writes use a five-minute review plan tied to that provider, selected network, resource state and resource schema. Changing the selected provider clears the frontend review. Reconfiguration and network selection invalidate backend plans. Submitted writes return refreshed state; HTTP acceptance alone does not prove that firmware persisted a change. Forms cannot be edited while an operation is completing.

| Feature | G3100 | Eero | BlueberryProxy |
| --- | --- | --- | --- |
| Devices | Connected inventory, block and restore access | Connected inventory, pause and restore internet access | Discovery evidence, no router disconnect control |
| UPnP | Status and on/off | Status and on/off when advertised | Unsupported |
| Router lighting | Status, on/off, brightness and timeout | Per-node status and on/off when advertised | Unsupported |
| Port forwarding | List, create, edit, enabled state and delete | List, create, edit, enabled state and delete | Unsupported as WAN NAT |
| DHCP | Primary LAN pool on/off, range and lease time | Automatic/custom subnet and lease range | Unsupported |
| DMZ | IPv4 host status and configuration | No DMZ host API established | Unsupported |
| DNS | Host records and upstream configuration | Upstream IPv4 resolvers | Host records and upstream resolver management |

Router access controls do not physically remove cables or Wi-Fi associations. Eero pause blocks internet access and may leave local LAN communication available. G3100 DMZ and DHCP changes can affect connectivity, and Eero DNS changes can restart the gateway. Notices appear with the corresponding review. IPv6 DNS, IPv6 DMZ, DHCP reservation CRUD, port ranges and forwarding schedules are not implemented by these adapters. BlueberryProxy upstream API changes affect the running resolver; configure app options for restart defaults.

The G3100 adapter reads form-field identifiers from authenticated CGI responses and gates writes using inspected SPA markers. Its public asset collector successfully read 91 assets from the requested gateway, including the forms used by these features. Eero request contracts were extracted from the supplied Android app. Adapter behavior is validated with simulated gateways and APIs. Live authenticated control remains unvalidated without gateway and Eero account access.

Add the BlueberryQuantum integration using only the app URL. No app API token is required for the WebUI or API. Existing HA entries migrate automatically and discard their old app token. Add BlueberryQuantum through the dashboard card picker or use `custom:blueberry-quantum-card`. The card includes a visual editor and follows HA theme colors, including the Proxy-style controls. Administrative actions pass through HA's authenticated WebSocket API. Inventory count sensors are discovered from readable collection descriptors, including collections supplied by extensions. A removed or unavailable capability becomes unavailable rather than reporting a false zero.

## Adding providers and settings

Built-in adapters live in `backend/blueberry_quantum/drivers`. Eero code stays in `BlueberryEero/provider.py`. `providers.py` retains compatibility exports and loads installed Python entry points in the `blueberry_quantum.providers` group. A provider extends `Provider` and declares a unique `kind`, a `configuration_schema()`, `snapshot()`, `resource_specs(snapshot)` and `apply(resource, operation, values)`. Setup actions use `actions(snapshot)` and `invoke(action, values)`. Installed entry points and external providers need no frontend changes. Failed optional plugins do not prevent other providers from starting.

The API's `GET /api/providers` returns provider instances and definitions. Each snapshot includes resource descriptors with `id`, `title`, `schema`, `operations`, `value`, `read` and `write`. Collection descriptors also provide `columns` and optional `allow_create`. Per-operation input schemas can be supplied as `operation_schemas`. Unavailable features include a reason. The same schema drives server validation and both frontend forms.

Schemas support bounded objects, strings, integers, numbers, booleans and arrays, including nested objects, required properties, defaults, enums, constants, minimum/maximum values and array/text length limits. Formats include IP, IPv4, IPv4 network, URL and DNS hostname. `writeOnly` marks secrets; `readOnly` marks display-only properties. `enumNames` supplies choice labels. `x-reconnect` set to false retains a live session when a configuration field changes. `x-hidden` hides identity fields and `x-immutable` prevents renaming existing record keys. Unknown fields and unadvertised operations are rejected. This is a documented schema subset, not a complete JSON Schema implementation.

An external provider uses kind `http_json` and serves `GET /v1/snapshot` and optionally `GET /v1/schema`. Its schema response contains a `resources` object keyed by resource ID. Resources may extend or replace the standard descriptors and introduce entirely new settings sections. Report read/write capabilities in the snapshot, and enable writes in Quantum. CRUD uses `POST /v1/{resource}` and `DELETE /v1/{resource}/{id}`. Other advertised operations use `POST /v1/{resource}/{operation}`, distinguishing device disconnect from reconnect. Resource and operation IDs use lowercase letters, digits and underscores. Old servers without `/v1/schema` retain the standard capability-based descriptors.

`frontend/quantum-panel.js` is the canonical renderer. Repository generation copies it into the standalone app and integration, and the app Dockerfile copies the canonical source during its build. `scripts/setup-ha-repositories.sh` regenerates distributable apps and integrations. `scripts/deployment-migrate-auto` publishes according to `repo.config.json`.

Standalone execution uses Python 3.11 or later.

```sh
python -m pip install -r sre/BlueberryQuantum/backend/requirements.txt
PYTHONPATH=sre/BlueberryQuantum/backend QUANTUM_DATA=./quantum-data python -m blueberry_quantum.server
```

The app listens on port 8060. Both direct access and Ingress connect immediately without an app token. Gateway administration passwords and Eero sign-in remain provider credentials. Provider settings edit the selected instance by default. Choose Add another provider to create a separate instance.

The read-only collector does not sign in or execute downloaded JavaScript.

```sh
PYTHONPATH=sre/BlueberryQuantum/backend python -m blueberry_quantum.collect --url https://192.168.1.1 --self-signed --output /tmp/quantum-spa-report.json
```

See the [Eero research notes](https://git.crit.rip/BlueberryFaygo/BlueberryFaygo/src/branch/main/sre/BlueberryQuantum/BlueberryEero/README.md) and [deployment instructions](https://git.crit.rip/BlueberryFaygo/BlueberryFaygo/src/branch/main/scripts/DEPLOYMENT.md). Protocol references include the router's public SPA and the [quantum_gateway project](https://github.com/cisasteelersfan/quantum_gateway). Adapters use original code based on observed wire formats.
