# Mobile access: manual phone checklist

These checks need a real phone, a real network and your tailnet. The
automated suites can't run them. **Every case is unverified until you run
it.** Write the date, the phone and the result next to each case. Never mark a
case passed because the office showed a QR code.

Automated coverage, for reference:

- `tests/integration/test_api.py`: host allow-list, pairing, origin check,
  request ids, home enable/disable, remote stages with a fake container, and
  that the auth key is never returned.
- `tests/unit/test_remote_control.py`: Serve only (never Funnel), the HTTPS
  link, and the auth key file.
- `tests/unit/test_access_script.py`: the Mac's addresses and `.env`.
- `tests/browser`: the access page at 1440 and 390 px.

| # | Case | Steps | Expected | Result |
|---|---|---|---|---|
| H1 | Host step | `python3 -m scripts.access home` | Prints the Mac's IP and `.local` address; the office restarts and the Mac still works at 127.0.0.1:8000 | unverified |
| H2 | Refused before turning on | Phone on Wi-Fi opens `http://<ip>:8000` before Check and turn on | Refused ("host_not_allowed") | unverified |
| H3 | Turn on | Check and turn on with the IP address | Card shows Ready for phone test, with a last-checked time | unverified |
| H4 | Pair | Connect your phone, scan with the camera | Phone opens the office signed in; card shows "Confirmed from iPhone · Safari" (or the phone's own) | unverified |
| H5 | Code used once | Open the same link again on another device | Not signed in | unverified |
| H6 | Code expires | Wait over 2 minutes, then scan | Not signed in | unverified |
| H7 | `.local` name | Turn on with `mac-mini.local`, open it on the phone | Works, or the guide's advice to use the number holds | unverified |
| H8 | Work from the phone | Start a discussion, send a message, open a delivered document | All work; a message resent after toggling airplane mode is stored once | unverified |
| H9 | Turn off | Turn off, then reload on the phone | Refused at once; the Mac still works | unverified |
| H10 | Undo | `python3 -m scripts.access home --off` | Office back on 127.0.0.1 only; the phone can't connect | unverified |
| R1 | Container | `docker compose ps remote` after upgrade | Running; card shows Setup required with Connect to Tailscale | unverified |
| R2 | Login link | Connect to Tailscale, approve in the browser | Card moves Awaiting authorization → Off (connected, route off); device "quarterdeck" in the admin console | unverified |
| R3 | Auth key | Forget this device, then join with a one-off key | Joins; the key appears nowhere in the page, `docker compose logs api remote` or the exchange folder | unverified |
| R4 | HTTPS prompt | Turn on remote access in a tailnet without HTTPS certificates | Shows the link to turn them on; nothing is on until it is done | unverified |
| R5 | Turn on | Turn on remote access | Shows `https://quarterdeck.<tailnet>.ts.net`, Ready for phone test | unverified |
| R6 | Serve keeps the address | Open that address from a laptop in the tailnet | The office loads and a sign-in works (Serve passes the address the office checks) | unverified |
| R7 | Pair from outside | Phone on mobile data with Tailscale on, scan the code | Signed in; card shows Confirmed from phone | unverified |
| R8 | Tailnet only | Phone with Tailscale off opens the address | Doesn't connect | unverified |
| R9 | No Funnel | `docker compose exec remote tailscale serve status` | Shows serve on 443 to 127.0.0.1:8000, no Funnel | unverified |
| R10 | Turn off | Turn off on the Mac, reload on the phone | Phone can't reach it; the Mac still works; serve status is empty | unverified |
| R11 | Restart | `python3 -m scripts.upgrade` with remote on; separately, `docker compose restart api` alone | After the upgrade remote access still works. After a bare api restart it may not until `docker compose restart remote` (it shares api's network); record which | unverified |
| M1 | Phone layout | On the phone: desk, Discussions, a discussion with the keyboard open, Work, a document | Readable, no sideways scrolling, the composer stays above the keyboard | unverified |
| M2 | Sleep | Let the Mac sleep with the energy setting on | Still reachable | unverified |
