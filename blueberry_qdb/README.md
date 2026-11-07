# BlueberryQDB

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