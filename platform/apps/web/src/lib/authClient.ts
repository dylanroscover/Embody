import { createAuthClient } from "better-auth/client";

// Browser-side Better Auth client. Same-origin: the catch-all route is mounted
// at /api/auth, which is Better Auth's default basePath, so no baseURL needed.
export const authClient = createAuthClient();
