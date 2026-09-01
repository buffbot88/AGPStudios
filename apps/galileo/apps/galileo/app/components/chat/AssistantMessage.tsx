import { memo } from 'react';
import { normalizeChatMessage } from '~/lib/runtime/agent-parts';
import { AgentPart } from './AgentPart';
import { Markdown } from './Markdown';

interface AssistantMessageProps {
  content: string;
  onResend?: () => void;
}

export const AssistantMessage = memo(({ content, onResend }: AssistantMessageProps) => {
  const message = normalizeChatMessage({ id: 'assistant', role: 'assistant', content });
  const text = message.parts.find((part) => part.type === 'text');
  const activity = message.parts.filter((part) => part.type !== 'text');

  return (
    <div className="overflow-hidden w-full">
      {activity.map((part, index) => <AgentPart key={`${part.type}-${index}`} part={part} />)}
      {text?.type === 'text' && text.text && <Markdown html>{text.text}</Markdown>}
      {onResend && <button type="button" className="mt-2 text-xs text-bolt-elements-textTertiary hover:text-bolt-elements-textPrimary" onClick={onResend}>Resend</button>}
    </div>
  );
});
