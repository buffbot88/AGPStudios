import { json, type MetaFunction } from '@remix-run/node';
import { ClientOnly } from 'remix-utils/client-only';
import { BaseChat } from '~/components/chat/BaseChat';
import { Chat } from '~/components/chat/Chat.client';
import { Header } from '~/components/header/Header';
import { AuthGate } from '~/components/AuthGate';
import { EcosystemNavbar } from '~/components/EcosystemNavbar';

export const meta: MetaFunction = () => {
  return [{ title: 'Galileo' }, { name: 'description', content: 'Talk with Galileo, an AI assistant by AGP Studios' }];
};

export const loader = () => json({});

export default function Index() {
  return (
    <AuthGate><div className="flex flex-col h-full w-full">
      <Header />
      <EcosystemNavbar />
      <ClientOnly fallback={<BaseChat />}>{() => <Chat />}</ClientOnly>
    </div></AuthGate>
  );
}
