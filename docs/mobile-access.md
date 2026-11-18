# Opening the office from your phone

The office runs on your Mac. A phone can reach it in two ways, and you can use
both:

| | Home network | Remote access |
|---|---|---|
| Where the phone can be | On the same Wi-Fi as the Mac | Anywhere with internet |
| What you need | Nothing extra | A free Tailscale account, and the Tailscale app on the phone |
| Address | `http://192.168.x.x:8000` | `https://quarterdeck.<your-tailnet>.ts.net` |
| Who can reach it | Devices on your home network | Only devices signed in to your tailnet |

Neither one puts the office on the public internet. Both need you to sign the
phone in once.

## Before you start: keep the Mac reachable

The phone reaches the office on the Mac, so the Mac has to stay powered,
online and awake, with Docker running. On a Mac mini, open System Settings →
Energy and turn on "Prevent automatic sleeping when the display is off". The
office does not change this setting for you.

## Home network

1. On the Mac, open Terminal in the office folder and run:

   ```
   python3 -m scripts.access home
   ```

   This finds the Mac's own network address, publishes the office there and
   restarts the office container. It changes nothing else on the Mac. At this
   point the office still refuses requests at that address.
2. In the office, go to Office settings → **Mobile & remote access**. Under
   Home network, choose the address (the number is the most reliable) and
   press **Check and turn on**. The office checks that it answers there and
   only then accepts the address.
3. Press **Connect your phone**. With the phone on the same Wi-Fi, scan the QR
   code with its camera and open the link. The phone is signed in.
4. On the phone, use Share → **Add to Home Screen** to keep the office one tap
   away.

To stop it, press **Turn off**: the office refuses that address again
straight away. To stop publishing it on the network as well, run
`python3 -m scripts.access home --off`.

If the router gives the Mac a new address, run step 1 again and turn the mode
on with the new address. You can avoid this by reserving the Mac's address in
your router.

## Remote access (Tailscale)

1. Create a Tailscale account at tailscale.com, if you don't have one, and
   install the Tailscale app on your phone. Sign in to the same account.
2. In the office, go to Office settings → **Mobile & remote access** → Remote
   access, and press **Connect to Tailscale**. Press **Approve "quarterdeck" in
   Tailscale** and approve the device in the page that opens. Back in the
   office, press **I've approved it**.

   If you prefer, use an auth key instead: create a one-off key in the
   Tailscale admin console and paste it under "Use an auth key instead". The
   office uses it once and does not keep it.
3. Press **Turn on remote access**. The first time, Tailscale may ask you to
   turn on HTTPS certificates for your tailnet. The office shows a link for
   this; turn them on, then press the button again.
4. When the card shows **Ready for phone test**, press **Connect your phone**.
   Make sure Tailscale is connected on the phone, then scan the code and open
   the link. Once the phone has signed in, the card shows **Confirmed from
   phone**.

**Turn off** stops the route and checks that it stopped. The Mac itself keeps
working. If you want this office removed from your tailnet, go to
**Advanced** → **Forget this device**.

## What the office does and doesn't do

- It accepts requests only at addresses it has checked, and only when you
  have turned them on. Requests under any other address are refused.
- The QR code and link hold a sign-in code that works once and expires after
  two minutes. They contain no password or lasting key.
- The `remote` container runs Tailscale's own image. It shares the office
  container's network so that Tailscale Serve can reach the office. It has no
  ports on the Mac and no access to Docker, and it never uses Tailscale
  Funnel.
- The home network address is plain HTTP inside your home network. Use remote
  access, which is HTTPS, when you aren't at home.

## When something is wrong

| What you see | What to do |
|---|---|
| Home network: "did not answer at that address" | Check the Mac is on that network, then run `python3 -m scripts.access home` again |
| The phone shows "This site can't be reached" at home | Check the phone is on the same Wi-Fi, not mobile data. Try the number address rather than `.local` |
| Remote access: Setup required, "container is not running" | Run `docker compose up -d remote` in the office folder |
| Remote access: HTTPS certificates are needed | Open the link shown and turn them on, or turn on HTTPS under DNS in the Tailscale admin console |
| Remote access: Connection problem | Press Turn off, then turn it on again. If it persists, check `docker compose logs remote` |
| Remote access stopped after restarting only the office container | Run `docker compose restart remote`: it shares the office container's network |
| Nothing works away from home | Check the Mac is awake and Tailscale is connected on the phone |
