export async function authenticated(request: Request) {
  const response = await fetch('https://agpstudios.org/api/auth/session', {
    headers: { cookie: request.headers.get('cookie') || '' },
  });
  if (!response.ok) return false;
  const data = (await response.json()) as { authenticated?: boolean };
  return data.authenticated === true;
}
