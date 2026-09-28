# Changelog

## 0.3.2

- "Test connection" replaces athlete ID `0` (owner of the key) with the real ID, e.g. `i123456`

## 0.3.1

- intervals.icu API key and athlete ID can be managed on the settings page (masked, applied without restart);
  the add-on option remains as fallback
- "Test connection" shows account, Garmin status, latest activity and wellness days
- The automatic sync starts as soon as a key is set, no restart needed

## 0.3.0

- First public release as a Home Assistant add-on repository (AGPL-3.0)
- Prebuilt images for `aarch64` and `amd64`
- Web UI via ingress only by default; MCP port disabled by default
- JavaScript libraries bundled (no CDN requests)
- English/German option translations and documentation

## 0.2.x

- Add-on operation: ingress web UI, MCP over HTTP with token, periodic sync, one-time database import
- Review fixes: atomic plan confirmation, conflict-checked undo, publisher revisions, strict workout parser
