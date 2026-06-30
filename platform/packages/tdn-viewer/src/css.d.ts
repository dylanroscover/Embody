// Side-effect CSS imports (the React Flow stylesheet and our own tdnViewer.css)
// are resolved by vite/astro at build time, but `tsc --noEmit` needs an ambient
// declaration to accept them. Type-only; no runtime effect.
declare module "*.css";
