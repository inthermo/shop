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

- **Live:** GitHub repo `inthermo/shop` (public), GitHub Pages from `main` / root.
  Receiving Intake → **https://inthermo.github.io/shop/receiving/**
- **Next:** point `tools.myinthermo.com` at it (add a DNS CNAME → `inthermo.github.io` + a
  `CNAME` file in this repo). Deferred until the DNS cutover.

Push path: `git push` over Windows SSH (`git@github.com:inthermo/shop.git`).
