# Vendored protocol helpers

Source: https://github.com/ayamedog/fgo-stat-exporter

Reference commit: `dda16a4409fa1c33fc9d296d02ae96ce84adfa16`.

Only the five modules required for login, response decoding and statistics field extraction are included. Their original MIT license is retained in `LICENSE`. Actual application configuration and account credentials are not included.

Copied via FGO-Capture-Kit/local-qr/vendor. Local changes: package-relative imports;
`GameClient.toplogin()` exposes the successfully validated response in memory, with
`statistics()` retaining its filtered export interface. This lets the plugin use
the same gacha counters and capture-local mstGacha types as Capture Kit without
exporting unrelated account tables or ignoring field-filter failures.
