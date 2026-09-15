import { json, type LoaderFunctionArgs } from '@remix-run/node';
import { AuthGate } from '~/components/AuthGate';
import { Header } from '~/components/header/Header';
import { Chat } from '~/components/chat/Chat.client';

export async function loader(args: LoaderFunctionArgs) {
  return json({ id: args.params.id, project: true });
}

export default function ChatRoute() {
  return <AuthGate><div className="flex h-full w-full flex-col"><Header /><Chat /></div></AuthGate>;
}
