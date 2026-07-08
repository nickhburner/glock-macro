# Deploying the A2 Remote relay (Cloudflare Worker)

The relay is a tiny "mailbox" that runs on YOUR free Cloudflare account.
Your PC drops its status into the mailbox, your phone picks it up, and
commands travel the other way. Nothing ever connects to your home network
from outside, and Cloudflare's servers take the traffic, not your PC.

You do this once. It takes about 10 minutes.

## What you need

- A free Cloudflare account: sign up at https://dash.cloudflare.com/sign-up
- Node.js installed on your PC (any recent version): https://nodejs.org
  (pick the LTS installer, click through it with the defaults)

## Steps

1. Open a terminal in THIS folder (the one with `worker.js` in it).
   In Windows Explorer: click the address bar, type `cmd`, press Enter.

2. Log wrangler (Cloudflare's deploy tool) into your account:

       npx wrangler login

   A browser window opens; click "Allow". The first run also installs
   wrangler itself, which can take a minute.

3. Create the storage space the relay uses:

       npx wrangler kv namespace create RELAY_KV

   (If that says the command is unknown, your wrangler is older; try
   `npx wrangler kv:namespace create RELAY_KV` with a colon.)

   The command prints a block with an `id = "...."` line. Copy that id.

4. Open `wrangler.toml` (this folder) in Notepad and replace
   `PASTE_YOUR_KV_NAMESPACE_ID_HERE` with the id you just copied.
   Save the file.

5. Deploy:

       npx wrangler deploy

   At the end it prints your relay's address, something like:

       https://a2-remote-relay.yourname.workers.dev

6. Test it: open that address in a browser. You should see:

       {"ok":true,"service":"a2-remote-relay"}

7. Paste the address into the "Relay URL" box in the A2 Macro Controller's
   Remote panel. Done with this half; now deploy the web page
   (see `remote/pages/DEPLOY.md`).

## Updating the relay later

If a new version of the app ships a newer `worker.js`, just run
`npx wrangler deploy` in this folder again.

## Usage limits (please read this once)

The relay stores data in Cloudflare KV. On the FREE Cloudflare plan, KV
allows 1,000 writes per day, and the only writes this feature makes are the
PC's status pushes (one write each). To stay well inside that budget the
companion pushes status only about **once a minute** while idle (it speeds up
to a few seconds for a short burst right after you send a command, then slows
back down). One write a minute is roughly 16 hours of remote-enabled runtime
per day before the write budget is touched, and the companion only runs while
the app is open, so a normal day never comes close.

Everything the phone does, checking status and sending commands, counts as
"reads" (100,000 per day on the free plan) and will never realistically run
out. The web page also **stops polling entirely while its tab is hidden or
closed**, so a forgotten open tab costs nothing and cannot lock you out.

If you want fresher idle status and do not mind spending more of the write
budget, lower `REMOTE_PUSH_INTERVAL` in `settings.json` (seconds between idle
pushes, default 60, minimum 30). To raise the ceiling instead, the Workers
Paid plan ($5/month) lifts the limits far beyond what this feature can use.

## Security notes, in plain words

- The relay never learns your secret token. It only ever sees the token's
  fingerprint (a hash), which cannot be reversed.
- Commands are signed with the token by your phone and checked by your PC.
  The relay (even its operator, which is you) cannot fake a command.
- Whoever operates a relay could read the status blobs stored in it. They
  contain only the macro's status text and recent log lines, no secrets.
  Since you deploy your own relay, that operator is you.
- Nobody can find your data on the relay without your install id, and the
  install id is only inside your pairing link. If a link ever leaks, press
  "Regenerate token" in the app: all old links die instantly.
- Floods and junk traffic get rate limited at Cloudflare's edge and by the
  worker itself (429 responses). None of it touches your home connection.
