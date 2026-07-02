# Forge Web UI

React + TypeScript SPA that provides the `forge web` chat interface.

## Building

```bash
npm install
npm run build
```

This outputs a production-ready bundle to `dist/`, which is committed to the
repository so that users who install Forge via `pip install` get the UI without
needing Node.js.

The `forge web` command (FastAPI) serves `web/dist/` at `/`.

## Development

```bash
npm install
npm run dev
```

Starts a Vite dev server (default port 5173) with hot-module replacement.
API calls to `/api/*` are proxied to `http://127.0.0.1:8099` (the local
`forge web` backend). Run `forge web` in a separate terminal first.

## Updating the built dist

After any change to `src/`, rebuild and commit:

```bash
npm run build
git add dist
git commit -m "build(web-ui): rebuild dist"
```
