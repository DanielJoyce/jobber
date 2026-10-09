# Vendored assets

The console works offline; nothing here is loaded from a CDN at runtime. Each file was
downloaded once with curl and committed unmodified.

| File | Version | Source | License | sha256 |
|---|---|---|---|---|
| htmx-2.0.4.min.js | 2.0.4 | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | 0BSD | e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447 |
| d3-7.9.0.min.js | 7.9.0 | https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js | ISC | f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539 |
| topojson-client-3.1.0.min.js | 3.1.0 | https://cdn.jsdelivr.net/npm/topojson-client@3.1.0/dist/topojson-client.min.js | ISC | 25cd02ae486cc5063e0215a4e4cfb15de83700c87ac48bac4d57dc6aaf3ebb89 |
| us-atlas-3.0.1-states-albers-10m.json | 3.0.1 | https://cdn.jsdelivr.net/npm/us-atlas@3.0.1/states-albers-10m.json | ISC (data from the public-domain US Census Bureau cartographic boundary files) | 6e7bb086a3c791490361968a3094f377f7726c5d0c4900fec03cc42db2305a3d |

`states-albers-10m.json` is already projected (Albers USA, 975 x 610 viewport) with Alaska and
Hawaii as insets, so it is drawn with `d3.geoPath(null)`. Feature ids are 2-digit FIPS codes;
`jobhunter.core.geo.by_fips` maps them to USPS codes. Territories are not included.
