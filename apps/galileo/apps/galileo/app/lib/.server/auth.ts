export type GalileoUser = { username: string; githubLinked: boolean; csrf: string };

async function session(request: Request) {
  const response = await fetch('https://agpstudios.org/api/auth/session', {
    headers: { cookie: request.headers.get('cookie') || '' },
  });
  if (!response.ok) return undefined;
  return (await response.json()) as { authenticated?: boolean; user?: { username?: string }; github_linked?: boolean; csrf?: string };
}

export async function authenticated(request: Request) {
  // Local dev has no agpstudios.org session cookies; treat requests as signed
  // in so authed routes (projects, chat, session) are exercisable on localhost.
  if (import.meta.env.DEV) {
    return true;
  }

  const data = await session(request);
  return data?.authenticated === true;
}

export async function getUser(request: Request): Promise<GalileoUser | undefined> {
  if (import.meta.env.DEV) {
    return { username: 'dev', githubLinked: false, csrf: '' };
  }

  const data = await session(request);
  return data?.authenticated && data.user?.username
    ? { username: data.user.username, githubLinked: data.github_linked === true, csrf: data.csrf || '' }
    : undefined;
}
