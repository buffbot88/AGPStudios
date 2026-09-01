import { useState } from 'react';
import type { AgentPart as AgentPartData } from '~/lib/runtime/agent-parts';
import { classNames } from '~/utils/classNames';

interface AgentPartProps {
  part: AgentPartData;
}

export function AgentPart({ part }: AgentPartProps) {
  switch (part.type) {
    case 'text':
      return null;
    case 'status':
      return <ActivityRow icon="i-ph:circle-notch" label={part.text} status="running" />;
    case 'tool-call':
      return <ToolCallPart part={part} />;
    case 'tool-result':
      return <ToolResultPart part={part} />;
    case 'file-change':
      return <ActivityRow icon="i-ph:file-code" label={`${part.operation === 'create' ? 'Create' : 'Update'} ${part.path}`} status={part.status} />;
    case 'command':
      return <CommandPart part={part} />;
    case 'task-status':
      return (
        <div className="my-2 rounded-md border border-bolt-elements-borderColor px-3 py-2 text-xs">
          <div className="mb-1 font-medium text-bolt-elements-textSecondary">Tasks</div>
          {part.tasks.map((task) => <ActivityRow key={task.id} icon="i-ph:check-square" label={task.label} status={task.status} />)}
        </div>
      );
  }
}

function ToolCallPart({ part }: { part: Extract<AgentPartData, { type: 'tool-call' }> }) {
  const [open, setOpen] = useState(false);
  const summary = getToolSummary(part.tool, part.args);

  return (
    <div className="my-2 rounded-md border border-bolt-elements-borderColor text-xs">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-bolt-elements-item-backgroundActive" onClick={() => setOpen((value) => !value)}>
        <StatusIcon status={part.status} />
        <span className="flex-1 text-bolt-elements-textSecondary">{summary}</span>
        <span className={open ? 'i-ph:caret-up' : 'i-ph:caret-down'} />
      </button>
      {open && <pre className="overflow-x-auto border-t border-bolt-elements-borderColor px-3 py-2 text-[11px] text-bolt-elements-textTertiary">{JSON.stringify(part.args, null, 2)}</pre>}
    </div>
  );
}

function ToolResultPart({ part }: { part: Extract<AgentPartData, { type: 'tool-result' }> }) {
  const [open, setOpen] = useState(false);
  const failed = Boolean(part.error);
  const value = failed ? part.error : typeof part.result === 'string' ? part.result : JSON.stringify(part.result, null, 2);

  return (
    <div className={classNames('my-2 rounded-md border text-xs', failed ? 'border-bolt-elements-icon-error' : 'border-bolt-elements-borderColor')}>
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-bolt-elements-item-backgroundActive" onClick={() => setOpen((current) => !current)}>
        <StatusIcon status={failed ? 'failed' : 'complete'} />
        <span className="flex-1 text-bolt-elements-textSecondary">{failed ? 'Tool failed' : 'Tool result'}</span>
        <span className={open ? 'i-ph:caret-up' : 'i-ph:caret-down'} />
      </button>
      {open && <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t border-bolt-elements-borderColor px-3 py-2 text-[11px] text-bolt-elements-textTertiary">{value}</pre>}
    </div>
  );
}

function CommandPart({ part }: { part: Extract<AgentPartData, { type: 'command' }> }) {
  const [open, setOpen] = useState(part.status === 'running' || part.status === 'failed');

  return (
    <div className="my-2 rounded-md border border-bolt-elements-borderColor text-xs">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-bolt-elements-item-backgroundActive" onClick={() => setOpen((value) => !value)}>
        <StatusIcon status={part.status} />
        <code className="flex-1 truncate text-bolt-elements-textSecondary">{part.command}</code>
        <span className={open ? 'i-ph:caret-up' : 'i-ph:caret-down'} />
      </button>
      {open && part.output && <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t border-bolt-elements-borderColor px-3 py-2 text-[11px] text-bolt-elements-textTertiary">{part.output}</pre>}
    </div>
  );
}

function ActivityRow({ icon, label, status }: { icon: string; label: string; status: string }) {
  return <div className="my-2 flex items-center gap-2 text-xs text-bolt-elements-textSecondary"><StatusIcon status={status} fallbackIcon={icon} /><span>{label}</span></div>;
}

function StatusIcon({ status, fallbackIcon }: { status: string; fallbackIcon?: string }) {
  const icon = status === 'running' ? 'i-svg-spinners:90-ring-with-bg' : status === 'complete' ? 'i-ph:check' : status === 'failed' || status === 'aborted' ? 'i-ph:x' : fallbackIcon || 'i-ph:circle';
  return <span className={classNames('shrink-0 text-base', status === 'complete' ? 'text-bolt-elements-icon-success' : status === 'failed' || status === 'aborted' ? 'text-bolt-elements-icon-error' : 'text-bolt-elements-textTertiary', icon)} />;
}

function getToolSummary(tool: string, args: Record<string, unknown>) {
  if (tool === 'read' && typeof args.path === 'string') return `Read ${args.path}`;
  if (tool === 'search' && typeof args.query === 'string') return `Search ${JSON.stringify(args.query)}`;
  if (tool === 'list' && typeof args.path === 'string') return `List ${args.path}`;
  return tool.replace(/_/g, ' ');
}
