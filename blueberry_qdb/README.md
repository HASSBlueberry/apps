# OAuth and App Check renewal

Integration 0.8.0 offers OAuth credentials plus App Check, or OAuth browser
sign-in through BlueberryQDB. Other setup methods are deprecated and hidden.
Existing entries are retained until the user migrates them. OAuth refresh
rotation is saved immediately, even if a later device request fails.
Home Assistant diagnostic sensors show access-token expiry, last OAuth
renewal, App Check expiry and sync time, and the last device sync. Secrets
are not published as entity attributes. A manually pasted App Check token
cannot renew itself. Configure BlueberryQDB to retrieve replacements.

BlueberryQDB 0.4.0 keeps App Check in private persistent storage, opens MyQ
near expiry, and polls its process logs for up to 60 seconds. The panel shows
expiry and recovery state and can explicitly reveal the usable token for
60 seconds. Do not post that token in support messages.

Optional login recovery is disabled by default. In the Home Assistant app
Configuration tab, set `login_recovery_enabled`, `myq_email`, `myq_password`,
`imap_enabled`, `imap_host`, `imap_port` (993), `imap_username`,
`imap_password` and `imap_folder` (INBOX). Use the mailbox provider's supported
IMAP credential or app password. Recovery only reads mail over verified TLS.
It does not mark messages read, delete mail, or send mail. IMAP credentials
are stored in Home Assistant app options, so restrict access to configuration
and backups. Never paste credentials into chat.

The bridge records a mailbox baseline before acting on the phone. It accepts
only new messages for the configured MyQ email, from notification@myq.com,
with the exact six-character verification subject. Recovery stops after
three minutes or on an unknown, locked or obstructed screen. It submits
credentials and a verification code at most once per attempt. It can select
an explicitly labelled email delivery button. Prefilled fields that differ
from the configured values must be cleared on the phone first. UI text input
currently supports printable ASCII without spaces or percent characters.
Automatic recovery has a 30-minute cooldown. It does not log out an existing
session. The app must actually issue and log a fresh token for recovery to
succeed. A fresh-token path has not been validated on the real login screen.

ADB targets may point to an externally managed Android environment. This
release does not package ReDroid, Magisk, Zygisk or an integrity provider.
The current host has no Android Binder devices, and an attestation-capable
emulator has not been established. Do not assume an emulator will renew
App Check successfully.

Cloud status is read every 30 seconds and several more times during the
30 seconds after a command. The integration reports provider states and
never treats successful command delivery as confirmation of movement.
Download diagnostics to see allowlisted state values and provider timestamps.
On the live account inspected during development, MyQ returned a garage door
with closed state, matching the official app's Closed for 4 hours screen.
The response contained no light state or capabilities. Separate devices with
light_state are now retained for light sensors when supplied by MyQ, but
light actuation and this opener's light availability remain unverified.
No physical garage command was sent during development.

## Earlier releases and research

# BlueberryQDB native MyQ phone controller

With app 0.3.0 and Blueberry MyQ integration 0.7.0, native mode uses the
signed-in official MyQ app as the controller. Sign in once inside that app.
There is no separate Chrome login, OAuth access token, client secret, App
Check requirement or 15-minute login window for native mode.

Open the QDB panel, select the connected phone and use Open MyQ on phone.
Unlock it and keep its door list visible. If an older Chrome flow is still
active, use Cancel Chrome sign-in. Enter the exact door name and press
Check connection now. Select the actual door state/control button and its
required tap or hold gesture, then Save selected door control. Inspection and
calibration do not operate the door.

In Home Assistant, add Blueberry MyQ and choose Use the signed-in MyQ app
through BlueberryQDB. Confirm the discovered bridge connection, or enter its
URL and key. Submitting the native form immediately verifies the app screen
and creates covers for calibrated doors. Disable the old cloud entry after
you verify the new controller, to avoid two separate controls for the opener.

The phone must remain unlocked, connected over ADB and showing MyQ. The relay
uses current UI bounds and state, never saved coordinates. It refuses moving,
unknown or offline states, changed or ambiguous controls, rapid repeated
commands and confirmation dialogs. It does not guess or automatically press
confirmation buttons. Some app layouts or confirmation workflows may need
additional support after inspection on the actual phone. Native mode is more
fragile than a supported local hardware controller.

Account tokens and passwords stay in the MyQ app. The bridge keeps a private
calibration, phone selection and connection key. Native bindings survive
restart and app state versions 1/2 migrate to version 3. Automatic App Check
foreground activity is disabled when native bindings exist. Native mode still
uses the official app's network connection to MyQ; it is not an offline opener
protocol.

Synthetic hierarchy, HTTP, gesture and setup tests pass. The real phone is
not attached to this workstation, so neither its current UI layout nor
physical door operation has been validated by this release.

The advanced App Check and separate cloud OAuth features remain available
below for users who choose that mode.


BlueberryQDB is a separate Home Assistant app that packages ADB and reads
runtime App Check messages from the official Android MyQ application. It
supplies the current token to the Blueberry MyQ integration over a private,
key-protected API. Home Assistant Core does not need ADB installed.

Install this app from the BlueberryFaygo Apps repository, then connect the
phone directly to the Home Assistant host by USB. Enable USB debugging,
unlock the phone, and approve this host's debugging key. In the app's Open
Web UI panel, select the phone if more than one is attached. The phone needs
the official MyQ app signed in. The panel reports unauthorized and offline
phones separately. Persistent app storage retains the ADB vendor key across
restarts and updates, so Android can remember the authorization.

The app polls for phones and extracts unexpired App Check tokens from retained
MyQ process logs. It never clears logs or signs out the phone. Opening MyQ is
an explicit panel action. If MyQ has not logged an unexpired token, open the
official app and complete sign-in there. Token extraction was observed on
MyQ 5.323.0.1132341. Future versions may stop logging tokens. This app cannot
mint attestation on behalf of the official app or guarantee unattended renewal.

Supervisor discovery offers the Blueberry MyQ setup flow with the app URL and
connection key already supplied. Install integration version 0.5.0 or later.
Choose email/password, browser sign-in, or existing OAuth credentials. The
integration fetches the current App Check token before token exchange or
refresh. It communicates directly with MyQ for account, device and door APIs;
BlueberryQDB only supplies attestation and does not operate the door.
## Automatic account sign-in with 0.2.0

Install Blueberry MyQ integration 0.6.1 and BlueberryQDB app 0.2.1. In the
integration setup, choose Sign in on connected phone with BlueberryQDB.
Supervisor discovery supplies the bridge URL and connection key. If connecting
manually, copy those connection settings from the app panel. No OAuth client
secret or pre-existing OAuth access token is needed for the tested client.

Connect and authorize the phone, select it in BlueberryQDB, and make sure an
App Check token is available. A current manually obtained App Check token can
also be entered in the phone sign-in form to seed the bridge cache. The setup
opens a dedicated Chrome tab on the selected phone. Unlock the phone and
complete any MyQ password, verification or provider prompts. An existing
provider session may complete without asking again. Home Assistant finishes
OAuth exchange and device discovery automatically. There is no callback URL
or access token to copy.

Home Assistant owns the PKCE verifier and account tokens. BlueberryQDB only
carries the authorization request and transient callback through Chrome. It
never receives the verifier, MyQ password or account tokens. Chrome preparation has a separate three-minute budget, followed by a
15-minute sign-in window. Pending callbacks disappear on app restart and are
removed after exchange or cancellation. The dedicated Chrome tab and its local ADB forward
are cleaned up, without closing the user's other tabs.

The automated path has HTTP and browser-protocol regression coverage. The
underlying phone callback interception previously succeeded in a live research
flow, but the packaged 0.2.0 path still needs a run on the HASS-connected phone.
App Check acquisition remains dependent on what the official MyQ release logs.

## Timing and token reuse in 0.2.1

The integration retains a valid App Check snapshot through the phone attempt,
so a temporary bridge failure after preflight does not discard it. The app
keeps the newer cached token when an older acquisition message remains in
logcat. Expired snapshots cause the integration to request fresh material.

`auto_refresh_app_check` defaults to true. While a selected phone is connected,
missing tokens and tokens within five minutes of expiry trigger a best-effort
request through the official MyQ app. Automatic attempts are limited to one
per five minutes. This may bring MyQ to the foreground on the phone; disable
the option if that is unwanted. Refresh never switches away from an active
QDB Chrome login. The bridge observes new SDK output and cannot mint a signed
App Check token itself. Provider prompts or a MyQ release that stops logging
tokens can still require manual action.

A captured callback is published to HASS before browser cleanup, allowing
token exchange to begin immediately. Cleanup has bounded debugger waits.
HASS and Ingress report preparation, user sign-in, callback and token-exchange
phases. The initial form no longer displays a misleading empty token-response
placeholder. Pre-exchange errors report the failed phase and a safe reason.
