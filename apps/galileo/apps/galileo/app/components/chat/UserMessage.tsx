import { modificationsRegex } from '~/utils/diff';
import { Markdown } from './Markdown';

interface UserMessageProps {
  content: string;
  onEdit?: () => void;
  timestamp?: Date;
}

export function UserMessage({ content, onEdit, timestamp }: UserMessageProps) {
  return (
    <div className="relative overflow-hidden pt-[4px]">
      <Markdown limitedMarkdown>{sanitizeUserMessage(content)}</Markdown>
      {timestamp && <span className="galileo-msg-time">{timestamp.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span>}
      {onEdit && (
        <button type="button" className="mt-2 text-xs text-bolt-elements-textTertiary hover:text-bolt-elements-textPrimary" onClick={onEdit}>
          Edit
        </button>
      )}
    </div>
  );
}

function sanitizeUserMessage(content: string) {
  return content.replace(modificationsRegex, '').trim();
}
