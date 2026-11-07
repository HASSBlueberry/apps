# Native app controller in 0.3.0

Native mode uses the account already signed in inside the official app.
Open the QDB panel, cancel any pending Chrome flow, open MyQ and show its
door list. Enter the exact door name and use Check connection now. This
checks the native screen and reads the reported state. It does not require
App Check or an OAuth account token.

Choose the actual state/toggle control offered by the inspection, select Tap
or Hold for 1.5 seconds to match the app, and save it. The bridge re-inspects
before saving and sends no input during calibration. If no uniquely labelled
state control exists, setup refuses calibration instead of guessing. Buttons
that only say Open or Close without a reliable state identity are not a
substitute for a calibrated state/toggle control.

HASS integration 0.7.0 includes a native setup form. Its submit action checks
the saved controls through the private bridge API and creates covers from
reported app state. Existing bridge settings can be reused without copying
account credentials. Native entries use a separate unique identity from cloud
entries; they do not silently replace existing cloud entities. Disable an old
cloud entry manually once native mode is verified.

Each command inspects the selected phone, verifies the foreground package and
checks for a locked screen or blocking confirmation. It locates the calibrated
control by semantic attributes within the selected door card. Coordinates
come from that fresh hierarchy. An already-satisfied action sends no input.
Moving and unknown states, stale requests, ambiguous selectors, offline
banners, changed phones and pending commands are refused. A requested gesture
is sent once. A subsequent screen inspection detects confirmation dialogs
without guessing their buttons. No optimistic physical state is reported.

A native command can open or close a real garage door. Initial calibration and
all automated tests in this workspace use synthetic screens and send no input
to a physical phone. Actual operation must be verified on the connected device.
The app's UI can change, and this release does not claim support for every
MyQ Android layout. Keep the phone unlocked and dedicated to the app while
using it as a controller. If MyQ requests a new login or a confirmation, complete
it on the phone. The relay does not bypass those prompts.

Native API endpoints require the bridge key. Ingress users can inspect and
save a calibration with CSRF protection, but cannot call motion endpoints using
Ingress authorization alone. Only the bearer-authenticated native command API
sends a gesture. Raw screen XML is neither returned by the API nor persisted.
Calibration selectors and the bridge key are stored privately in app state.
State versions 1 and 2 upgrade to 3 while retaining existing keys and caches.
No OAuth credentials are needed or copied into native HASS entries.

# don't use this please chamberlain will C&D me and i will cry
## Phone-browser OAuth transport

With app 0.2.0 and integration 0.6.0, choose Sign in on connected phone in the
MyQ setup menu. HASS creates state and a PKCE verifier, then sends only the
registered authorization URL to `POST /v1/oauth/sessions`. QDB restricts that
URL to the MyQ identity origin, the registered native callback and S256 PKCE.
Only one phone flow can be pending at a time. The phone selection cannot be
changed while that flow is pending.

The selected phone needs Chrome in its primary Android profile. QDB creates a
uniquely marked tab and a loopback ADB forward to Chrome's debugger socket.
It intercepts the identity server's final native-callback redirect before the
MyQ application can consume it. The HASS integration polls the authenticated
session endpoint, validates the callback again with its own state and verifier,
and exchanges it using App Check and an explicit BlueberryQ User-Agent.
The HASS progress dialog finishes with account/device discovery. Closing it
cancels the remote browser flow. No provider password or verification code is
requested by the bridge.

`GET /v1/oauth/sessions/<id>` is bearer-key protected even for Ingress requests.
Only this private endpoint can return the callback, and only within the
session's lifetime. The normal panel status reports phase and safe error codes
without the callback, authorization URL or state. Sessions are kept in memory,
not `/data`. `DELETE /v1/oauth/sessions/<id>` releases the session. The API
never performs an OAuth exchange and never receives an OAuth verifier.

An optional manually pasted App Check token can be imported through
`POST /v1/app-check` by HASS, using the same connection key. This seeds the
selected phone's cache after local format/expiry checks; only MyQ validates its
signature. The token endpoint stays separate from the OAuth callback transport.
If App Check is unavailable, setup stops before opening an authorization flow.

For existing cloud entries, Reconfigure offers Sign in on the connected phone
instead. Reauthentication automatically selects phone sign-in when a bridge
is already configured. The existing account identity check still prevents
reconfiguration from switching a cover to a different account.

## Timing and cache behavior in 0.2.1

The browser preparation budget is 180 seconds. Once debugger interception is
installed and the sign-in page is about to open, the user receives a separate
900-second authentication window. Individual debugger-command timeouts do
not serve as the user deadline. A fallback creates a dedicated debugger tab
when Chrome does not preserve the about:blank marker from an Android intent.
No unrelated existing tab is selected.

Captured callbacks become available to HASS before tab and ADB cleanup finish.
The integration caches App Check metadata in memory for the duration of an
attempt, reuses it while valid, and fetches new material on expiry. A failed
second bridge lookup therefore cannot discard a valid preflight token. The
bridge also refuses to overwrite a newer imported cache with an older log
message. Account access and refresh tokens remain in Home Assistant.

With `auto_refresh_app_check: true`, the app can open official MyQ to request
fresh SDK activity for missing or near-expiry tokens, at most once per five
minutes. An explicit HASS request uses `POST /v1/app-check/refresh`. This API
only requests native app activity and observes logs; it cannot manufacture
attestation. While Chrome login is active, refresh does not steal focus. The
status response distinguishes refresh activity and browser phases.

HASS waits for attestation before opening an authorization flow. If the token
expires during human sign-in, it waits briefly for replacement before
spending the captured code. Token exchange remains a single request, without
automatically retrying a potentially consumed authorization code. Network
errors while polling the phone callback receive a short recovery window.
