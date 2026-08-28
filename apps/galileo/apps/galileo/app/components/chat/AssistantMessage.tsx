import { memo } from 'react';
import { Markdown } from './Markdown';

interface AssistantMessageProps {
  content: string;
  onResend?: () => void;
}

export const AssistantMessage = memo(({ content, onResend }: AssistantMessageProps) => {
  return (
    <div className="overflow-hidden w-full">
      <Markdown html>{content}</Markdown>
      {onResend && <button type="button" className="mt-2 text-xs text-bolt-elements-textTertiary hover:text-bolt-elements-textPrimary" onClick={onResend}>Resend</button>}
    </div>
  );
});
