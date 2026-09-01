import type { Message } from 'ai';
import type { ActionState } from './action-runner';

export type AgentStatus = 'thinking' | 'planning' | 'working' | 'verifying';
export type AgentName = 'Galileo' | 'Oracle' | 'Builder' | 'Verifier';
export type AgentPartStatus = 'pending' | 'running' | 'complete' | 'failed' | 'aborted';

export interface AgentTask {
  id: string;
  label: string;
  status: 'pending' | 'running' | 'complete' | 'failed';
}

export type AgentPart =
  | { type: 'text'; text: string }
  | { type: 'status'; state: AgentStatus; text: string; agent: AgentName }
  | { type: 'tool-call'; id: string; tool: string; args: Record<string, unknown>; status: AgentPartStatus; agent: AgentName }
  | { type: 'tool-result'; toolCallId: string; result?: unknown; error?: string; durationMs?: number }
  | { type: 'file-change'; path: string; operation: 'create' | 'update'; content?: string; status: AgentPartStatus }
  | { type: 'command'; id: string; command: string; status: AgentPartStatus; output?: string; exitCode?: number }
  | { type: 'task-status'; tasks: AgentTask[] };

export interface AgentMessage {
  id: string;
  role: 'user' | 'assistant';
  parts: AgentPart[];
  createdAt?: number;
}

const READ_ONLY_TOOLS = new Set(['list', 'read', 'search', 'refresh_context']);

export function normalizeChatMessage(message: Message, index = 0): AgentMessage {
  const id = message.id || `message-${index}`;
  const content = typeof message.content === 'string' ? message.content : '';

  if (message.role === 'user') {
    const toolResult = parseToolResult(content);

    if (toolResult) {
      return {
        id,
        role: 'assistant',
        parts: [{ type: 'tool-result', ...toolResult }],
      };
    }

    return { id, role: 'user', parts: [{ type: 'text', text: content }] };
  }

  const tool = parseToolRequest(content);

  if (tool) {
    return {
      id,
      role: 'assistant',
      parts: [{ type: 'tool-call', id: `${id}-tool`, tool: tool.name, args: tool.args, status: 'complete', agent: agentForTool(tool.name) }],
    };
  }

  return { id, role: 'assistant', parts: [{ type: 'text', text: stripBuildMarker(content) }] };
}

export function normalizeChatMessages(messages: Message[]): AgentMessage[] {
  const normalized: AgentMessage[] = [];
  let pendingToolCallId: string | undefined;

  for (const [index, message] of messages.entries()) {
    const normalizedMessage = normalizeChatMessage(message, index);
    const part = normalizedMessage.parts[0];

    if (part?.type === 'tool-call') {
      pendingToolCallId = part.id;
    } else if (part?.type === 'tool-result' && part.toolCallId === 'unknown' && pendingToolCallId) {
      normalizedMessage.parts[0] = { ...part, toolCallId: pendingToolCallId };
      pendingToolCallId = undefined;
    }

    normalized.push(normalizedMessage);
  }

  return normalized;
}

export function normalizeAction(action: ActionState, id: string): AgentPart {
  if (action.type === 'file') {
    return {
      type: 'file-change',
      path: action.filePath,
      operation: action.content ? 'update' : 'create',
      content: action.content,
      status: action.status,
    };
  }

  return {
    type: 'command',
    id,
    command: action.content,
    status: action.status,
    ...(action.status === 'failed' ? { output: action.error } : {}),
  };
}

export function normalizeBuildEvent(kind: string, index = 0): AgentPart {
  const normalized = kind.toLowerCase();
  const status: AgentStatus = normalized.includes('test') || normalized.includes('validat') ? 'verifying' : 'working';

  return {
    type: 'status',
    state: status,
    text: formatBuildEvent(kind),
    agent: status === 'verifying' ? 'Verifier' : 'Builder',
  };
}

function parseToolRequest(content: string): { name: string; args: Record<string, unknown> } | null {
  try {
    const value = JSON.parse(content) as { tool?: { name?: unknown; path?: unknown; query?: unknown } };
    const tool = value.tool;

    if (!tool || typeof tool.name !== 'string' || !READ_ONLY_TOOLS.has(tool.name)) return null;

    const args: Record<string, unknown> = {};
    if (typeof tool.path === 'string') args.path = tool.path;
    if (typeof tool.query === 'string') args.query = tool.query;
    return { name: tool.name, args };
  } catch {
    return null;
  }
}

function parseToolResult(content: string): Extract<AgentPart, { type: 'tool-result' }> | null {
  try {
    const value = JSON.parse(content) as { tool_result?: { ok?: boolean; result?: unknown; error?: unknown } };
    const result = value.tool_result;
    if (!result || typeof result.ok !== 'boolean') return null;

    return result.ok
      ? { type: 'tool-result', toolCallId: 'unknown', result: result.result }
      : { type: 'tool-result', toolCallId: 'unknown', error: typeof result.error === 'string' ? result.error : 'Tool failed' };
  } catch {
    return null;
  }
}

function agentForTool(tool: string): AgentName {
  return tool === 'refresh_context' ? 'Oracle' : 'Galileo';
}

function stripBuildMarker(content: string) {
  return content.replace('<!-- GALILEO_BUILD_READY -->', '').trim();
}

function formatBuildEvent(kind: string) {
  return kind
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
