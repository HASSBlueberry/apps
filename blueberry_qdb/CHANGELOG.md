# 0.5.1

Detect the silent `uiautomator` failure caused by a privileged adbd, return adbd to the shell user and retry once before reporting `android_screen_unreadable`, restore non-root adbd after module setup, discover the managed ADB target when selecting a device, connect network ADB targets before waiting for them, and read recovery screens through the same dump helper.

# 0.5.0

Add a persistent managed ADB target, ReDroid-only screenshots and input, Zygisk status, and pinned module setup with architecture and Google services checks. BlueberryDroid supplies the remote SSH tunnel and ingress UI.

# 0.4.1

Add key-protected Android runtime status and a panel check for Android, ADB root, Magisk, Google Play services and MyQ installation.

# 0.4.0

Add optional TLS IMAP and bounded ADB login recovery, visible renewal state, explicit token reveal, and a longer App Check observation window. Native calibration no longer blocks cloud renewal.

# 0.3.0

Add native MyQ UI control with one sign-in inside the official app. Add an
immediate Check connection now action, read-only control calibration, state-
checked single gestures and a dedicated native HASS mode. Keep OAuth and App
Check out of native setup. Add explicit cancellation of old Chrome sessions.
Preserve native bindings in version 3 app state. Native operation is covered
with synthetic screens; physical door operation remains unverified.

# 0.2.1

Separate Chrome startup and human sign-in budgets. Allow 15 minutes after the
browser is ready, publish callbacks before cleanup, and bound cleanup waits.
Retain newer App Check caches, request refresh through the official app when
needed, and avoid interrupting managed Chrome sign-in. Add safe phase status.

# 0.2.0

Add phone Chrome OAuth transport with short-lived sessions and callback
interception. Home Assistant retains PKCE and obtains account tokens
without copying a callback or access token. Clean up the dedicated tab and
ADB forward on completion, cancellation and timeout. Add optional manual
App Check cache import and safe login phase status in Ingress.

# 0.1.0

Package ADB for USB phone discovery and runtime MyQ App Check extraction.
Add persistent keys, automatic polling, authenticated integration API,
Supervisor discovery, Ingress phone selection and state migration.

Verify the authenticated bridge with a live MyQ refresh and read-only discovery
using a migrated private token cache. Build and smoke-test the amd64 container.
