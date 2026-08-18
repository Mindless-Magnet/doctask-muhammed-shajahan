# Review interface

The queue a person works in. React, Vite, no router and no state library — this is one screen and
adding either would be architecture in search of a problem.

```bash
npm install
npm run dev          # http://localhost:5173, proxies /api to localhost:8000
```

Or `docker compose up -d review` from the parent directory, which builds and serves the bundle.

## What it does not do

It performs no operation the API and the MCP server do not already expose. Approve, reject and
commit are the same calls a program makes. That is what keeps behaviour 4 honest: the gate is an
operation, and this is one caller of it rather than the place it lives.

Everything it needed from the backend is a read, and those live in `src/ledgerline/api/review.py`.

## Files

| File | |
|---|---|
| `src/App.jsx` | the whole interface: run summary, item queue, evidence panel |
| `src/api.js` | the only place that talks to the API, and the only place that parses a proposed-change payload |
| `src/styles.css` | one stylesheet, no framework |

`api.js` parses defensively in one place on purpose. Proposed-change content arrives JSON-encoded on
some surfaces and as an object on others; parsing at each use site is how "every field reads as
undefined" spreads through a codebase.

## Configuration

`VITE_LEDGERLINE_TOKEN` is baked into the bundle at build time, so it is a display token for a local
stack and nothing more. A real deployment would put the API behind a session and never ship a token
to a browser at all.
