# Deploying the A2 Remote web page (GitHub Pages)

The web page is a single static file (`index.html`). It holds no secrets:
your pairing key only ever reaches it through the pairing link's `#`
fragment, which stays inside your browser. Hosting it publicly is safe.

You do this once, after deploying the relay (see `remote/worker/DEPLOY.md`).

## Steps

1. Sign in to https://github.com (a free account is fine).

2. Create a new repository: click the "+" in the top right, "New
   repository". Name it, for example, `a2-remote`. Set it to Public
   (GitHub Pages on a free account needs a public repo). Click "Create".

3. Upload the page: on the new repo's page, click "uploading an existing
   file" (or Add file, then Upload files), drag in the `index.html` from
   THIS folder, and press "Commit changes".

4. Turn on Pages: in the repo, go to Settings, then Pages (left sidebar).
   Under "Build and deployment", set Source to "Deploy from a branch",
   Branch to `main` and folder `/ (root)`, then Save.

5. Wait a minute, refresh the Pages settings page, and it shows your URL:

       https://YOURNAME.github.io/a2-remote/

   Open it; you should see the "Not paired" screen.

6. Paste that URL into the "Web page URL" box in the A2 Macro Controller's
   Remote panel.

7. Pair a device: in the app, press "Copy pairing link" and get that link
   onto your phone (send it to yourself over something private, e.g. a
   note-to-self chat you trust, then delete the message). Open the link
   once on the phone; the page remembers the pairing in that browser until
   you press "Forget this device".

## Updating the page later

If a new app version ships a newer `index.html`, upload it to the repo
again (same steps, it overwrites the old one).

## Alternative hosts

Any static host works (Cloudflare Pages, Netlify, your own server), as
long as the page is served over https; the browser's crypto features
require it.
