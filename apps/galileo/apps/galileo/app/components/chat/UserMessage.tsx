import { modificationsRegex } from '~/utils/diff';
import { Markdown } from './Markdown';

interface UserMessageProps {
  content: string;
  onEdit?: () => void;
}

export function UserMessage({ content, onEdit }: UserMessageProps) {
  return (
    <div className="overflow-hidden pt-[4px]">
      <Markdown limitedMarkdown>{sanitizeUserMessage(content)}</Markdown>
      {onEdit && <button type="button" className="mt-2 text-xs text-bolt-elements-textTertiary hover:text-bolt-elements-textPrimary" onClick={onEdit}>Edit</button>}
    </div>
  );
}

function sanitizeUserMessage(content: string) {
  return content.replace(modificationsRegex, '').trim();
}
