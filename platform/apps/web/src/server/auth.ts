export interface RequestUser {
  id: string;
  handle: string;
}

// TODO: Replace this stub with Better Auth session validation in a later backend task.
export async function getRequestUser(
  _request: Request,
  _env: CloudflareEnv
): Promise<RequestUser | null> {
  return {
    id: "dev-user",
    handle: "dev"
  };
}

export async function requireUser(request: Request, env: CloudflareEnv): Promise<RequestUser> {
  const user = await getRequestUser(request, env);
  if (!user) {
    throw new Error("Authentication required.");
  }
  return user;
}
