# Vendored browser libraries

## three.js

- **File:** `three.module.min.js`
- **Version:** r169 (`three@0.169.0`, `build/three.module.min.js`)
- **Licence:** MIT — see `three.LICENSE.txt`
- **Source:** <https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.min.js>

Used by `../viewer.js` to render the tessellated CAD mesh.

### Why it is checked in rather than loaded from a CDN

The rest of this project runs with no network access once the Python
dependencies are installed, and it is meant to be usable on a workstation
behind a corporate proxy. A CDN reference would make the 3D viewer fail
offline, and the failure would look like a bug in the app rather than a missing
download.

### Why there is still no build step

This is the ES-module build, imported directly by `viewer.js` with a relative
path. Browsers resolve that natively, so there is no bundler, no `npm install`,
and no `node_modules` — the file is served as-is by the same FastAPI static
mount as the rest of the UI.

Only `three.js` core is vendored. The camera controls in `viewer.js` are
written by hand rather than pulling in `OrbitControls`, because the viewer needs
fit-to-view, reset, and keyboard support that would have to be written on top of
it anyway.

### Upgrading

Replace the file with a newer `build/three.module.min.js`, update the version
above, and re-run the test suite — `tests/test_cad_viewer.py` asserts the file
is present, is an ES module, and carries its licence header.
