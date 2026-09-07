// Legacy alias for GET /api/specimens/:slug/tdn.
//
// The route moved to `tdxn.ts` when the format was renamed TDN -> TDXN. This
// path is the FROZEN C4 contract that already-shipped Embody builds hit, so it
// keeps serving forever -- it just forwards to the canonical handler.
export { GET, prerender } from "./tdxn";
