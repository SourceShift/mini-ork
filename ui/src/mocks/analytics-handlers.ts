// Stub: the upstream agent-canvas SPA registered mock handlers for its
// third-party analytics ingestion. The minio-ork fork has no client-side
// analytics; this stub keeps the import surface stable so callers can still
// re-export ANALYTICS_HANDLERS without dragging in the upstream SDK.
export const ANALYTICS_HANDLERS = [];
