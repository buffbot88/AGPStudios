import type { Message } from 'ai';
import React from 'react';
import { normalizeBuildEvent } from '~/lib/runtime/agent-parts';
import { AgentPart, StreamingAgentStatus } from './AgentPart';
import { AgentActivity } from './AgentActivity';
import type { AgentEvent } from '~/lib/runtime/galileo-stream';
import { classNames } from '~/utils/classNames';
import { AssistantMessage } from './AssistantMessage';
import { UserMessage } from './UserMessage';

interface MessagesProps {
  id?: string;
  className?: string;
  isStreaming?: boolean;
  messages?: Message[];
  onEdit?: (index: number, content: string) => void;
  onResend?: (index: number) => void;
  jobEvents?: string[];
  streamingTool?: { name: string; args: Record<string, unknown> } | null;
  activityEvents?: AgentEvent[];
}

export const Messages = React.forwardRef<HTMLDivElement, MessagesProps>((props: MessagesProps, ref) => {
  const { id, isStreaming = false, messages = [], onEdit, onResend, jobEvents = [], streamingTool = null, activityEvents = [] } = props;

  return (
    <div id={id} ref={ref} className={props.className}>
      {messages.length > 0
        ? messages.map((message, index) => {
            const { role, content } = message;
            const isUserMessage = role === 'user';
            const isFirst = index === 0;
            const isLast = index === messages.length - 1;

            return (
              <div
                key={index}
                className={classNames('galileo-message flex gap-4 p-6 w-full rounded-[calc(0.75rem-1px)]', {
                  'bg-bolt-elements-messages-background': isUserMessage || !isStreaming || (isStreaming && !isLast),
                  'bg-gradient-to-b from-bolt-elements-messages-background from-30% to-transparent':
                    isStreaming && isLast,
                  'mt-4': !isFirst,
                  'galileo-user-message': isUserMessage,
                  'galileo-assistant-message': !isUserMessage,
                })}
              >
                {isUserMessage && (
                  <div className="flex items-center justify-center w-[34px] h-[34px] overflow-hidden bg-white text-gray-600 rounded-full shrink-0 self-start">
                    <div className="i-ph:user-fill text-xl"></div>
                  </div>
                )}
                <div className="grid grid-col-1 w-full">
                  {isUserMessage ? (
                    <UserMessage content={content} onEdit={!isStreaming ? () => onEdit?.(index, content) : undefined} />
                  ) : (
                    <AssistantMessage content={content} onResend={!isStreaming && isLast ? () => onResend?.(index) : undefined} />
                  )}
                </div>
              </div>
            );
          })
        : null}
      {jobEvents.length > 0 && (
        <details open className="galileo-verification-card mt-4 rounded border border-bolt-elements-borderColor p-4 text-sm">
          <summary className="cursor-pointer">Agent activity ({jobEvents.length})</summary>
          <div className="mt-2 space-y-1">{jobEvents.map((event, index) => <AgentPart key={`${event}-${index}`} part={normalizeBuildEvent(event, index)} />)}</div>
        </details>
      )}
      {activityEvents.length > 0 && <AgentActivity events={activityEvents} />}
      {streamingTool && <AgentPart part={{ type: 'tool-call', id: 'streaming-tool', tool: streamingTool.name, args: streamingTool.args, status: 'running', agent: 'Galileo' }} />}
      {isStreaming && !streamingTool && <StreamingAgentStatus />}
    </div>
  );
});
