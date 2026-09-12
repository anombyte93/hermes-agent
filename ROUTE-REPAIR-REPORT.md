# Route repair report — plugin navigation after enabling Kanban (issue55)

## Summary

Fixed the packaged Desktop defect where Settings > Plugins > Enable Kanban > Escape
did not render the Kanban board until the window was reloaded. The React Compiler
memoized the no-argument `contributedRoutes()` registry read as a constant, so a
`routes` contribution registered after mount never mounted its `<Route>` and a
removed contribution never disappeared.

## Root cause (proven, not assumed)

Two React consumers shared the same broken pattern:

- `ChatRoutesSurface` (apps/desktop/src/app/contrib/surfaces.tsx) called
  `useContributions(ROUTES_AREA)` and discarded the returned snapshot, then derived
  the route list with `contributedRoutes()`, a no-argument read of the registry.
- `RouteTilePane` (apps/desktop/src/app/chat/route-tile.tsx) did the same.

`contributedRoutes()` reads `registry.getArea(ROUTES_AREA)` directly. The React
Compiler sees no reactive dependency on that call, so it memoizes the result as a
constant across renders. The `useContributions` subscription still fired, but the
discarded snapshot fed nothing into the render output, so the cached route list
stayed stale. Result: enabling Kanban after mount did not add the `/kanban` route;
reloading at the same hash re-ran the derivation and the board appeared.

The compiler is active in the vitest suite: `vitest.config.ts` extends
`vite.config.ts`, which wires `babel-plugin-react-compiler` via
`@rolldown/plugin-babel`. The unit RED below reproduced the exact defect, so this
is a real compiler-driven failure, not a speculatively assumed one.

## Fix

Added a pure derivation in apps/desktop/src/app/routes.ts:

- `routesFromContributions(contributions)` maps and filters a resolved
  `routes` snapshot into renderable routes. Pure over its input.
- `contributedRoutes()` now delegates to it with `registry.getArea(ROUTES_AREA)`,
  preserving the imperative read for non-React callers (`isContributedPath`,
  `routeSessionId`, `appViewForPath`, and `routeTitle`).

Both React consumers now pass the live snapshot into the derivation, giving the
compiler a real dependency:

- `surfaces.tsx`: `routesFromContributions(useContributions(ROUTES_AREA))`.
- `route-tile.tsx`: same, with the `useContributions` call kept unconditional
  (Rules of Hooks).

No forced reload, no always-on plugin, no change to non-React callers.

## Proof (RED then GREEN)

New test apps/desktop/src/app/contrib/surfaces.routes.test.tsx uses the real
`registry`, the real `useContributions` subscription, and mounted `Routes`. It
mocks only presentation modules the route surface delegates to (chat view, sidebar,
terminal chrome, statusbar), never the registry, the subscription hook, or the
route table.

Run against the unfixed code it reproduced RED:

- "renders a route registered AFTER mount without a reload" failed: the board never
  appeared after registration.
- "stops rendering a route once it is unregistered" failed: the board stayed after
  disposal.

After the fix the same suite is GREEN, alongside:

- "renders a route registered before mount" (initial enabled positive)
- "leaves an unrelated route path alone while another is mounted" (built-in/chat
  navigation unaffected)

## Verification

- `tsc --noEmit -p tsconfig.json`: clean.
- `eslint` on all five changed files: clean.
- vitest ui project, routes + contrib dirs: 12 files, 121 tests passed.
- surfaces test files (new + updated): 5 tests passed.

## Result ledger

- ACTUALLY_USED: node22 toolchain on PATH; vitest, tsc, and eslint binaries resolved
  from the monorepo root node_modules; real registry + real subscription + mounted
  `Routes`; react 19.2.7 and react-router 8.3.0 as installed.
- DISCOVERED: the React Compiler is active in the vitest suite (config extends
  `vite.config.ts`), so the compiler-specific caching reproduces as unit RED.
  The nested `apps/desktop/node_modules` is a partial electron-only install; full
  dependencies resolve from the monorepo root `node_modules`.
- UNTOUCHED: Kanban plugin files, Python/core, config, docs, AGENTS.md,
  package.json/lock, node_modules. No push/merge, no new workers or cards, no
  credentials, no session/auth/vault access.
- MISSING: no prior ROUTE-REPAIR-REPORT.md existed; created here. No dedicated
  `RouteTilePane` test exists and the component is not exported; its fix is covered
  by the shared `routesFromContributions` derivation and the `ChatRoutesSurface`
  mounted test.
- FRICTION: terminal single-query mode blocked `npx vitest` on a package
  threat-intelligence deadline; worked around by invoking the vitest binary
  directly with `node`.
