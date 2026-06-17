# inthermo-shop

Internal shop-floor web tools for Inthermo LLC. Static frontends (HTML/JS/CSS, no build step)
hosted on GitHub Pages, served **top-level** (not inside an Apps Script iframe) so browser APIs
like the camera (`getUserMedia`) work on the shop-floor iPads.

Each tool's frontend calls an Apps Script web app as a JSON backend via `fetch()`. The Apps
Script projects (the backends) live in the `inthermo-scripts` repo; only the presentation layer
lives here.

## Why this repo exists

The Receiving Intake "Snap PO Photo" camera never worked because Apps Script serves its pages
inside a sandboxed cross-origin iframe that strips the camera permission. Moving the frontend
here (top-level HTTPS origin) removes that ceiling. See the Receiving Intake memory for history.

## Layout

```
receiving/        Receiving Intake — touchscreen receiving entry + PO photo capture
                  Backend: "Receiving Intake" Apps Script project (doPost JSON API).
```

## Backend contract

Frontends POST `{op, payload}` as `Content-Type: text/plain` (a CORS "simple request" — no
preflight, which Apps Script can't answer). The backend replies with JSON and permissive CORS.
The auth token stays server-side in the backend's Script Properties; it is never shipped to the
browser.

Add `?selftest=1` to any tool's URL to run its cross-origin round-trip self-test.

## Hosting

- **Now (preview):** mirrored into the `inthermo/website` repo at `/shop/receiving/` as a stopgap,
  reachable at `inthermo.github.io/website/shop/receiving/`, until this repo gets its own Pages.
- **Target:** push this repo to `inthermo/shop`, enable Pages, point `tools.myinthermo.com` at it.
  Requires `gh auth login` (creating a new repo needs auth that SSH-to-existing-repos doesn't give).
