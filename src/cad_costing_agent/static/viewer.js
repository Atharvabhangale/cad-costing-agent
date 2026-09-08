// 3D CAD viewer. Visualization only — there is no costing arithmetic here, and
// nothing this file computes is ever sent back to the server.
//
// It receives the common renderable mesh built by the backend (`/cad/mesh`):
// a flat `positions` array and a flat `indices` array. The browser never parses
// STEP or STL itself; OpenCASCADE and trimesh already did that server-side.
//
// Normals are derived here with computeVertexNormals() rather than transmitted.
// That is correct because the backend deliberately does not merge vertices
// across surface boundaries, so averaging stays within a single surface and
// real edges stay sharp.

import * as THREE from "./vendor/three.module.min.js";

const host = document.getElementById("viewer-canvas");
const overlay = document.getElementById("viewer-overlay");

// CAD is conventionally Z-up; three.js defaults to Y-up. Standing the world up
// makes the default view read like a CAD front-right-top isometric.
const UP = new THREE.Vector3(0, 0, 1);

// Isometric default: looking from +X, -Y, +Z, i.e. the direction (1, -1, 1).
const DEFAULT_POLAR = Math.atan(Math.SQRT2); // ~54.7 degrees down from +Z
const DEFAULT_AZIMUTH = -Math.PI / 4;

const MIN_POLAR = 0.001;
const MAX_POLAR = Math.PI - 0.001;
const FIT_MARGIN = 1.35;
const FIELD_OF_VIEW = 45;

// EdgesGeometry is O(triangles) and its buffers rival the mesh's own, so real
// CAD edges are only drawn while that stays cheap.
const MAX_TRIANGLES_FOR_EDGES = 50000;

let renderer = null;
let scene = null;
let camera = null;
let modelGroup = null;
let boundingSphere = null;
let unavailableReason = null;

// Camera state, integrated by the interaction handlers below.
const target = new THREE.Vector3();
const spherical = { radius: 10, polar: DEFAULT_POLAR, azimuth: DEFAULT_AZIMUTH };

let frameQueued = false;

// ---------------------------------------------------------------- overlay

// Modifiers are `is-`-prefixed so they cannot collide with the page-level
// `.error` rule, which would otherwise repaint the overlay as a form error box.
function setOverlay(kind, message) {
  if (!overlay) return;
  overlay.className = `viewer-overlay is-${kind}`;
  overlay.innerHTML =
    kind === "loading"
      ? `<span class="spinner" aria-hidden="true"></span><span>${message}</span>`
      : `<span>${message}</span>`;
  overlay.hidden = false;
}

function hideOverlay() {
  if (overlay) overlay.hidden = true;
}

// ---------------------------------------------------------------- setup

function initialise() {
  if (renderer || unavailableReason) return;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  } catch (error) {
    unavailableReason =
      "This browser could not start WebGL, so the 3D preview is unavailable. " +
      "Geometry and costing are unaffected.";
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x11161d, 1);
  host.appendChild(renderer.domElement);
  renderer.domElement.setAttribute("aria-label", "3D model preview");

  scene = new THREE.Scene();

  camera = new THREE.PerspectiveCamera(FIELD_OF_VIEW, 1, 0.1, 1000);
  camera.up.copy(UP);

  // Two lights parented to the camera so the part is always lit from the
  // viewer's side however it is rotated, plus a hemisphere fill so faces
  // pointing away are shaded rather than black.
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(0.6, 0.5, 1);
  const rim = new THREE.DirectionalLight(0xffffff, 0.7);
  rim.position.set(-0.8, -0.4, 0.3);
  camera.add(key, rim);
  scene.add(camera);
  scene.add(new THREE.HemisphereLight(0xdfe9f5, 0x2a2f38, 1.1));

  modelGroup = new THREE.Group();
  scene.add(modelGroup);

  attachInteraction();

  const resize = new ResizeObserver(() => requestRender());
  resize.observe(host);
  requestRender();
}

function resizeToHost() {
  const width = host.clientWidth;
  const height = host.clientHeight;
  if (!width || !height) return false;
  const size = renderer.getSize(new THREE.Vector2());
  if (size.x !== width || size.y !== height) {
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  return true;
}

function requestRender() {
  if (frameQueued || !renderer) return;
  frameQueued = true;
  requestAnimationFrame(() => {
    frameQueued = false;
    if (!resizeToHost()) return;
    applyCamera();
    renderer.render(scene, camera);
  });
}

function applyCamera() {
  const sinPolar = Math.sin(spherical.polar);
  camera.position.set(
    target.x + spherical.radius * sinPolar * Math.cos(spherical.azimuth),
    target.y + spherical.radius * sinPolar * Math.sin(spherical.azimuth),
    target.z + spherical.radius * Math.cos(spherical.polar)
  );
  camera.lookAt(target);

  // Keep the depth range tight around the model so z-fighting does not appear
  // on large parts, and distant parts do not get clipped.
  const extent = boundingSphere ? boundingSphere.radius : 1;
  camera.near = Math.max(spherical.radius * 0.001, extent * 0.0005);
  camera.far = spherical.radius + extent * 8;
  camera.updateProjectionMatrix();
}

// ---------------------------------------------------------------- model

function disposeModel() {
  modelGroup.traverse((child) => {
    if (child.geometry) child.geometry.dispose();
    if (child.material) child.material.dispose();
  });
  modelGroup.clear();
  boundingSphere = null;
  componentMeshes = [];
  componentIndex = new Map();
  explodeOffsets = new Map();
  explodeFactor = 0;
  connectorLines = null;
  surfaceMesh = null;
  edgeLines = null;
  isolated = null;
}

// An assembly is drawn as one THREE.Mesh per occurrence, all sharing one
// geometry per definition — so a part used twelve times is uploaded to the GPU
// once and drawn twelve times. Each mesh carries its CAD placement and, when
// the view is exploded, a display offset on top of it. The CAD placement is
// never overwritten: `userData.origin` keeps it, and every explosion is
// recomputed from that, so returning to 0% restores the file's own arrangement
// exactly rather than approximately.
let componentMeshes = [];   // one per occurrence
let componentIndex = new Map(); // occurrence_id -> mesh
let explodeOffsets = new Map(); // occurrence_id -> [dx, dy, dz] at factor 1
let explodeFactor = 0;
let connectorLines = null;
let surfaceMesh = null;
let edgeLines = null;
let isolated = null;

function buildGeometry(mesh) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(Float32Array.from(mesh.positions), 3)
  );
  geometry.setIndex(new THREE.Uint32BufferAttribute(Uint32Array.from(mesh.indices), 1));
  geometry.computeVertexNormals();
  geometry.computeBoundingSphere();
  return geometry;
}

/**
 * Render a mesh from `/cad/mesh`. `mesh.positions` and `mesh.indices` are used
 * verbatim; nothing about the shape is recomputed here.
 */
function show(mesh) {
  initialise();
  if (unavailableReason) {
    setOverlay("error", unavailableReason);
    return;
  }

  clearMarker();
  disposeModel();

  const geometry = buildGeometry(mesh);
  isolated = null;

  const surface = new THREE.Mesh(geometry, baseMaterial());
  surfaceMesh = surface;
  modelGroup.add(surface);

  // Real model edges (not the triangulation) so the part reads as an
  // engineering shape rather than a blob.
  if (mesh.triangle_count <= MAX_TRIANGLES_FOR_EDGES) {
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry, 25),
      new THREE.LineBasicMaterial({ color: 0x1a2733 })
    );
    edgeLines = edges;
    modelGroup.add(edges);
  }

  boundingSphere = geometry.boundingSphere.clone();
  addGroundGrid(mesh);

  spherical.polar = DEFAULT_POLAR;
  spherical.azimuth = DEFAULT_AZIMUTH;
  fit();
  hideOverlay();
}

// A grid under the part, sized to it, so rotation and scale are readable.
function addGroundGrid(mesh) {
  const size = Math.max(
    mesh.bounds.max[0] - mesh.bounds.min[0],
    mesh.bounds.max[1] - mesh.bounds.min[1],
    mesh.bounds.max[2] - mesh.bounds.min[2],
    1e-6
  );
  const grid = new THREE.GridHelper(size * 3, 24, 0x30465c, 0x222d38);
  grid.rotation.x = Math.PI / 2; // GridHelper lies in XZ; stand it up into XY
  grid.position.set(
    (mesh.bounds.min[0] + mesh.bounds.max[0]) / 2,
    (mesh.bounds.min[1] + mesh.bounds.max[1]) / 2,
    mesh.bounds.min[2]
  );
  grid.material.transparent = true;
  grid.material.opacity = 0.5;
  modelGroup.add(grid);
}

function clear() {
  if (renderer) {
    clearMarker();
    disposeModel();
    requestRender();
  }
  setOverlay("empty", "Upload a CAD file to see it here.");
}

// ------------------------------------------------------------ feature marker
//
// A selected feature is *pointed at* in 3D, never measured in 3D. The location
// and axis come from the backend's recognized feature — the same numbers the
// features table prints — and are used verbatim. Nothing is read back out of
// the marker and no engineering value is derived from it; it exists so that
// "H-001" in a table and a spot on the part are visibly the same thing.
//
// No scaling is applied, and none should be: feature coordinates already share
// the mesh's coordinate space. For an analytic STEP both come from the same
// OpenCASCADE shape; for a faceted export both come from the same polygons.

const MARKER_COLOR = 0xffa726;

let markerGroup = null;

function clearMarker() {
  if (!markerGroup) return;
  markerGroup.traverse((child) => {
    if (child.geometry) child.geometry.dispose();
    if (child.material) child.material.dispose();
  });
  markerGroup.clear();
  requestRender();
}

/**
 * Mark a recognized feature on the model: a dot at its location plus, where the
 * feature has one, a line along its axis — a hole's drilling direction, or a
 * planar face's outward normal.
 */
function markFeature(feature) {
  initialise();
  if (unavailableReason || !boundingSphere) return;
  if (!markerGroup) {
    markerGroup = new THREE.Group();
    scene.add(markerGroup);
  }
  clearMarker();
  if (!feature || !feature.location) return;

  // Sized from the model so the marker stays visible on a 10 mm insert and on a
  // 2 m frame alike.
  const size = Math.max(boundingSphere.radius * 0.035, 1e-9);
  const point = new THREE.Vector3(feature.location.x, feature.location.y, feature.location.z);

  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(size, 20, 14),
    new THREE.MeshBasicMaterial({ color: MARKER_COLOR })
  );
  dot.position.copy(point);
  markerGroup.add(dot);

  if (feature.axis) {
    const direction = new THREE.Vector3(feature.axis.x, feature.axis.y, feature.axis.z);
    if (direction.lengthSq() > 0) {
      const reach = size * 7;
      markerGroup.add(
        new THREE.Line(
          new THREE.BufferGeometry().setFromPoints([
            point.clone().addScaledVector(direction, -reach),
            point.clone().addScaledVector(direction, reach),
          ]),
          new THREE.LineBasicMaterial({ color: MARKER_COLOR })
        )
      );
    }
  }
  requestRender();
}

// ---------------------------------------------------------------- framing

/** Frame the whole model, keeping the current viewing angle. */
function baseMaterial() {
  return new THREE.MeshStandardMaterial({
    color: 0x9fb1c4,
    metalness: 0.25,
    roughness: 0.55,
    side: THREE.DoubleSide,
  });
}

function highlightMaterial() {
  return new THREE.MeshStandardMaterial({
    color: 0x1f6feb,
    metalness: 0.2,
    roughness: 0.45,
    side: THREE.DoubleSide,
  });
}

/**
 * Render an assembly from `/cad/assembly`: one geometry per definition, one
 * mesh per occurrence.
 *
 * `exploded` is the backend's display-only arrangement — each occurrence's CAD
 * placement beside the offset it would take at full explosion. Interpolating
 * between them here is a multiply and an add per component, which is why the
 * slider needs no request, no re-tessellation and no reparse.
 */
function showAssembly(mesh, exploded) {
  initialise();
  if (unavailableReason) {
    setOverlay("error", unavailableReason);
    return;
  }

  clearMarker();
  disposeModel();
  isolated = null;
  explodeFactor = 0;

  // One geometry per definition. A definition placed twelve times is uploaded
  // once; the twelve meshes share it.
  const geometries = new Map();
  (mesh.definitions || []).forEach((definition) => {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(Float32Array.from(definition.positions), 3)
    );
    geometry.setIndex(new THREE.Uint32BufferAttribute(Uint32Array.from(definition.indices), 1));
    geometry.computeVertexNormals();
    geometry.computeBoundingSphere();
    geometries.set(definition.definition_id, geometry);
  });

  explodeOffsets = new Map();
  ((exploded && exploded.component_transforms) || []).forEach((placement) => {
    explodeOffsets.set(placement.occurrence_id, placement.offset);
  });

  (mesh.instances || []).forEach((instance) => {
    const geometry = geometries.get(instance.definition_id);
    if (!geometry) return;

    const part = new THREE.Mesh(geometry, baseMaterial());
    // The transform arrives as twelve numbers, row-major 3x4. THREE's Matrix4
    // takes them row-major too, so the rotation goes in as written.
    const m = instance.transform;
    part.matrixAutoUpdate = false;
    part.userData.origin = [m[9], m[10], m[11]];
    part.userData.rotation = m.slice(0, 9);
    part.userData.occurrenceId = instance.occurrence_id;
    part.userData.definitionId = instance.definition_id;
    part.userData.name = instance.name;
    applyPlacement(part, 0);

    componentMeshes.push(part);
    componentIndex.set(instance.occurrence_id, part);
    modelGroup.add(part);
  });

  if (!componentMeshes.length) {
    setOverlay("error", "This assembly has no components that could be drawn.");
    return;
  }

  boundingSphere = assemblySphere();
  addGroundGrid(mesh);

  spherical.polar = DEFAULT_POLAR;
  spherical.azimuth = DEFAULT_AZIMUTH;
  fit();
  hideOverlay();
}

// Place one component at `origin + offset x factor`, rebuilt from the stored
// CAD placement every time so repeated slider moves cannot drift.
function applyPlacement(part, factor) {
  const r = part.userData.rotation;
  const o = part.userData.origin;
  const d = explodeOffsets.get(part.userData.occurrenceId) || [0, 0, 0];
  part.matrix.set(
    r[0], r[1], r[2], o[0] + d[0] * factor,
    r[3], r[4], r[5], o[1] + d[1] * factor,
    r[6], r[7], r[8], o[2] + d[2] * factor,
    0, 0, 0, 1
  );
  part.matrixWorld.copy(part.matrix);
}

function assemblySphere() {
  const box = new THREE.Box3();
  componentMeshes.forEach((part) => {
    if (!part.visible) return;
    const local = part.geometry.boundingSphere;
    if (!local) return;
    const centre = local.center.clone().applyMatrix4(part.matrix);
    box.expandByPoint(centre.clone().addScalar(local.radius));
    box.expandByPoint(centre.clone().addScalar(-local.radius));
  });
  const sphere = new THREE.Sphere();
  box.getBoundingSphere(sphere);
  return sphere;
}

/**
 * Move the assembly to an explosion factor. 0 is the arrangement the CAD file
 * states; 1 is fully separated.
 *
 * This touches transforms and nothing else — no geometry is rebuilt, nothing is
 * re-fetched, and no engineering value exists in this file to disturb.
 */
function setExplode(factor) {
  if (!componentMeshes.length) return 0;
  explodeFactor = Math.max(0, Math.min(1, Number(factor) || 0));
  componentMeshes.forEach((part) => applyPlacement(part, explodeFactor));
  drawConnectors();
  boundingSphere = assemblySphere();
  requestRender();
  return explodeFactor;
}

function resetExplode() {
  return setExplode(0);
}

// Faint lines from where a component belongs to where it is drawn. A display
// aid for reading the explosion, and nothing more: they are not assembly paths
// and imply no order.
function drawConnectors() {
  if (connectorLines) {
    modelGroup.remove(connectorLines);
    connectorLines.geometry.dispose();
    connectorLines.material.dispose();
    connectorLines = null;
  }
  if (explodeFactor <= 0) {
    requestRender();
    return;
  }

  const points = [];
  componentMeshes.forEach((part) => {
    if (!part.visible) return;
    const o = part.userData.origin;
    const d = explodeOffsets.get(part.userData.occurrenceId);
    if (!d || (d[0] === 0 && d[1] === 0 && d[2] === 0)) return;
    points.push(new THREE.Vector3(o[0], o[1], o[2]));
    points.push(
      new THREE.Vector3(
        o[0] + d[0] * explodeFactor,
        o[1] + d[1] * explodeFactor,
        o[2] + d[2] * explodeFactor
      )
    );
  });
  if (!points.length) return;

  connectorLines = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color: 0x4a5b6d, transparent: true, opacity: 0.55 })
  );
  modelGroup.add(connectorLines);
}

/**
 * Colour one component and leave the rest as they are.
 *
 * Passing null restores every component to the base material. A component id
 * the current mesh does not contain is ignored rather than guessed at — a
 * viewer that highlighted *something* when asked for a component it does not
 * have would be worse than one that highlighted nothing.
 */
function highlightComponent(occurrenceId) {
  if (!componentMeshes.length) return false;
  let found = false;
  componentMeshes.forEach((part) => {
    const match = part.userData.occurrenceId === occurrenceId;
    const wanted = match ? highlightMaterial() : baseMaterial();
    part.material.dispose();
    part.material = wanted;
    if (match) found = true;
  });
  requestRender();
  return found;
}

/**
 * Draw one component and nothing else, by narrowing the draw range to its own
 * slice of the index buffer.
 */
function isolateComponent(occurrenceId) {
  if (!componentIndex.has(occurrenceId)) return false;
  componentMeshes.forEach((part) => {
    part.visible = part.userData.occurrenceId === occurrenceId;
  });
  isolated = occurrenceId;
  highlightComponent(occurrenceId);
  drawConnectors();
  boundingSphere = assemblySphere();
  requestRender();
  return true;
}

/** Put every component back. */
function restoreComponents() {
  if (!componentMeshes.length) return;
  componentMeshes.forEach((part) => {
    part.visible = true;
  });
  isolated = null;
  highlightComponent(null);
  drawConnectors();
  boundingSphere = assemblySphere();
  requestRender();
}

function componentState() {
  return {
    available: componentMeshes.length > 0,
    count: componentMeshes.length,
    definitions: new Set(componentMeshes.map((part) => part.userData.definitionId)).size,
    isolated,
    explodeFactor,
    exploding: explodeOffsets.size > 0,
  };
}

/** Where a component is currently drawn, for the panel to report. */
function componentPlacement(occurrenceId) {
  const part = componentIndex.get(occurrenceId);
  if (!part) return null;
  const d = explodeOffsets.get(occurrenceId) || [0, 0, 0];
  const o = part.userData.origin;
  return {
    name: part.userData.name,
    definitionId: part.userData.definitionId,
    original: o,
    displayed: [
      o[0] + d[0] * explodeFactor,
      o[1] + d[1] * explodeFactor,
      o[2] + d[2] * explodeFactor,
    ],
  };
}

function fit() {
  if (!boundingSphere) return;
  target.copy(boundingSphere.center);
  const halfFov = THREE.MathUtils.degToRad(camera.fov) / 2;
  const halfFovHorizontal = Math.atan(Math.tan(halfFov) * Math.max(camera.aspect || 1, 0.2));
  const limiting = Math.min(halfFov, halfFovHorizontal);
  spherical.radius = (boundingSphere.radius / Math.sin(limiting)) * FIT_MARGIN;
  requestRender();
}

/** Fit, and return to the default isometric orientation. */
function reset() {
  spherical.polar = DEFAULT_POLAR;
  spherical.azimuth = DEFAULT_AZIMUTH;
  fit();
}

// ---------------------------------------------------------------- interaction

function attachInteraction() {
  const canvas = renderer.domElement;
  canvas.tabIndex = 0;
  canvas.style.touchAction = "none";

  const pointers = new Map();
  let mode = null; // "orbit" | "pan"
  let lastPinch = 0;
  let lastPanCentre = null;

  const centreOf = () => {
    const points = [...pointers.values()];
    return {
      x: points.reduce((sum, p) => sum + p.x, 0) / points.length,
      y: points.reduce((sum, p) => sum + p.y, 0) / points.length,
    };
  };
  const pinchDistance = () => {
    const [a, b] = [...pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  };

  canvas.addEventListener("pointerdown", (event) => {
    try {
      canvas.setPointerCapture(event.pointerId);
    } catch {
      // Capture is an optimisation for drags that leave the canvas; a browser
      // that refuses it must not break dragging altogether.
    }
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    canvas.focus({ preventScroll: true });
    if (pointers.size === 2) {
      mode = "pinch";
      lastPinch = pinchDistance();
      lastPanCentre = centreOf();
    } else {
      // Middle/right button, or shift-drag, pans; plain left-drag orbits.
      mode = event.button === 1 || event.button === 2 || event.shiftKey ? "pan" : "orbit";
    }
  });

  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  canvas.addEventListener("pointermove", (event) => {
    const previous = pointers.get(event.pointerId);
    if (!previous) return;
    const dx = event.clientX - previous.x;
    const dy = event.clientY - previous.y;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (mode === "pinch" && pointers.size === 2) {
      const distance = pinchDistance();
      if (lastPinch > 0) dolly(lastPinch / distance);
      lastPinch = distance;
      const centre = centreOf();
      if (lastPanCentre) pan(centre.x - lastPanCentre.x, centre.y - lastPanCentre.y);
      lastPanCentre = centre;
      return;
    }
    if (mode === "orbit") orbit(dx, dy);
    else if (mode === "pan") pan(dx, dy);
  });

  const release = (event) => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) {
      lastPinch = 0;
      lastPanCentre = null;
      mode = pointers.size === 1 ? "orbit" : null;
    }
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      dolly(Math.exp(event.deltaY * 0.001));
    },
    { passive: false }
  );

  // Keyboard equivalents for every mouse gesture, so the viewer is usable
  // without a pointing device.
  canvas.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 20 : 6;
    const handlers = {
      ArrowLeft: () => (event.ctrlKey ? pan(step * 3, 0) : orbit(step, 0)),
      ArrowRight: () => (event.ctrlKey ? pan(-step * 3, 0) : orbit(-step, 0)),
      ArrowUp: () => (event.ctrlKey ? pan(0, step * 3) : orbit(0, step)),
      ArrowDown: () => (event.ctrlKey ? pan(0, -step * 3) : orbit(0, -step)),
      "+": () => dolly(0.9),
      "=": () => dolly(0.9),
      "-": () => dolly(1 / 0.9),
      f: fit,
      F: fit,
      r: reset,
      R: reset,
    };
    const handler = handlers[event.key];
    if (handler) {
      event.preventDefault();
      handler();
    }
  });
}

function orbit(dx, dy) {
  spherical.azimuth -= dx * 0.008;
  spherical.polar = Math.min(MAX_POLAR, Math.max(MIN_POLAR, spherical.polar - dy * 0.008));
  requestRender();
}

// Pan in the camera's own screen plane, scaled so the model tracks the cursor
// roughly one-to-one at the target's depth.
function pan(dx, dy) {
  const height = host.clientHeight || 1;
  const worldPerPixel =
    (2 * spherical.radius * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / height;

  const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
  const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
  target.addScaledVector(right, -dx * worldPerPixel);
  target.addScaledVector(up, dy * worldPerPixel);
  requestRender();
}

function dolly(factor) {
  const extent = boundingSphere ? boundingSphere.radius : 1;
  spherical.radius = Math.min(
    Math.max(spherical.radius * factor, extent * 0.05),
    extent * 200
  );
  requestRender();
}

// ---------------------------------------------------------------- wiring

document.getElementById("viewer-fit").addEventListener("click", fit);
document.getElementById("viewer-reset").addEventListener("click", reset);

// app.js is a classic script and cannot import from a module, so the viewer
// publishes the small surface it needs.
window.CadViewer = {
  show,
  clear,
  fit,
  reset,
  markFeature,
  clearMarker,
  showAssembly,
  highlightComponent,
  isolateComponent,
  restoreComponents,
  componentState,
  componentPlacement,
  setExplode,
  resetExplode,
  loading: (message) => setOverlay("loading", message || "Preparing 3D preview…"),
  error: (message) => setOverlay("error", message),
};

clear();
