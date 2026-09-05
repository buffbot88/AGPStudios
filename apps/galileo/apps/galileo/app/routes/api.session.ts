import { json } from '@remix-run/node';
import { authenticated, getUser } from '~/lib/.server/auth';

/**
 * Returns the signed-in account for the header identity chip. The session
 * itself lives on agpstudios.org; this only forwards what the user sees.
 */
export async function loader({ request }: { request: Request }) {
  if (!(await authenticated(request))) {
    return json({ authenticated: false as const });
  }

  const user = await getUser(request);

  return json({ authenticated: true as const, user });
}
