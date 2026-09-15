import { memo, useMemo, useState } from 'react';
import { normalizeChatMessage } from '~/lib/runtime/agent-parts';
import { AgentPart } from './AgentPart';
import { Markdown } from './Markdown';

interface AssistantMessageProps {
  content: string;
  onResend?: () => void;
  timestamp?: Date;
}

export const AssistantMessage = memo(({ content, onResend, timestamp }: AssistantMessageProps) => {
  const message = useMemo(() => normalizeChatMessage({ id: 'assistant', role: 'assistant', content }), [content]);
  const text = message.parts.find((part) => part.type === 'text');
  const activity = message.parts.filter((part) => part.type !== 'text');
  const [showMarkdown, setShowMarkdown] = useState(false);
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null);

  const body = text?.type === 'text' ? text.text : '';

  const copy = () => {
    navigator.clipboard
      .writeText(body)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1_500);
      })
      .catch(() => setCopied(false));
  };

  return (
    <div className="galileo-assistant-card overflow-hidden w-full">
      <div className="galileo-agent-chip mb-2">
        <span className="i-ph:sparkle-fill text-accent-500" />
        Galileo Agent
        {timestamp && <time>· {timestamp.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>}
      </div>
      {activity.map((part, index) => (
        <AgentPart key={`${part.type}-${index}`} part={part} />
      ))}
      {body && (showMarkdown ? (
        <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-lg border border-bolt-elements-borderColor bg-black/40 p-3 text-xs text-bolt-elements-textSecondary">{body}</pre>
      ) : (
        <Markdown html>{body}</Markdown>
      ))}
      <div className="galileo-msg-actions">
        <button aria-label="Copy message" className="galileo-msg-action" onClick={copy} title="Copy" type="button">
          <span className={copied ? 'i-ph:check text-sm' : 'i-ph:copy text-sm'} />
        </button>
        <button
          aria-label="Helpful response"
          className={`galileo-msg-action ${feedback === 'up' ? 'is-on' : ''}`}
          onClick={() => setFeedback((value) => (value === 'up' ? null : 'up'))}
          title="Good response"
          type="button"
        >
          <span className="i-ph:thumbs-up text-sm" />
        </button>
        <button
          aria-label="Unhelpful response"
          className={`galileo-msg-action ${feedback === 'down' ? 'is-on' : ''}`}
          onClick={() => setFeedback((value) => (value === 'down' ? null : 'down'))}
          title="Needs work"
          type="button"
        >
          <span className="i-ph:thumbs-down text-sm" />
        </button>
        {onResend && (
          <button className="galileo-msg-action" onClick={onResend} title="Regenerate" type="button">
            <span className="i-ph:arrows-clockwise text-sm" />
          </button>
        )}
        {body && (
          <button className="galileo-view-md" onClick={() => setShowMarkdown((value) => !value)} type="button">
            <span className="i-ph:code text-sm" />
            {showMarkdown ? 'View as rich text' : 'View as Markdown'}
          </button>
        )}
      </div>
    </div>
  );
});
