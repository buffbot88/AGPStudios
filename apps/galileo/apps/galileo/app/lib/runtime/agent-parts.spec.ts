import type { Message } from 'ai';
import { describe, expect, it } from 'vitest';
import type { ActionState } from './action-runner';
import { normalizeAction, normalizeBuildEvent, normalizeChatMessage, normalizeChatMessages } from './agent-parts';

describe('agent parts normalization', () => {
  it('normalizes ordinary user and assistant messages', () => {
    const messages = normalizeChatMessages([
      { id: 'u1', role: 'user', content: 'Inspect the app' },
      { id: 'a1', role: 'assistant', content: 'I will inspect it.<!-- GALILEO_BUILD_READY -->' },
    ] as Message[]);

    expect(messages[0].parts).toEqual([{ type: 'text', text: 'Inspect the app' }]);
    expect(messages[1].parts).toEqual([{ type: 'text', text: 'I will inspect it.' }]);
  });

  it('normalizes JSON tool requests and results', () => {
    const request = normalizeChatMessage({ id: 'a1', role: 'assistant', content: '{"tool":{"name":"read","path":"src/App.tsx"}}' } as Message);
    const result = normalizeChatMessage({ id: 'r1', role: 'user', content: '{"tool_result":{"ok":true,"result":"content"}}' } as Message);

    expect(request.parts).toEqual([{ type: 'tool-call', id: 'a1-tool', tool: 'read', args: { path: 'src/App.tsx' }, status: 'complete' }]);
    expect(result.parts).toEqual([{ type: 'tool-result', toolCallId: 'unknown', result: 'content' }]);

    expect(normalizeChatMessages([
      { id: 'a1', role: 'assistant', content: '{"tool":{"name":"read","path":"src/App.tsx"}}' },
      { id: 'r1', role: 'user', content: '{"tool_result":{"ok":true,"result":"content"}}' },
    ] as Message[])[1].parts).toEqual([{ type: 'tool-result', toolCallId: 'a1-tool', result: 'content' }]);
  });

  it('ignores unsupported tool requests', () => {
    const message = normalizeChatMessage({ role: 'assistant', content: '{"tool":{"name":"shell","path":"pwd"}}' } as Message);
    expect(message.parts).toEqual([{ type: 'text', text: '{"tool":{"name":"shell","path":"pwd"}}' }]);
  });

  it('normalizes file and shell actions', () => {
    const file = { type: 'file', filePath: 'src/App.tsx', content: 'new content', status: 'complete' } as ActionState;
    const shell = { type: 'shell', content: 'pnpm test', status: 'failed', error: 'exit 1' } as ActionState;

    expect(normalizeAction(file, 'file-1')).toMatchObject({ type: 'file-change', path: 'src/App.tsx', operation: 'update', status: 'complete' });
    expect(normalizeAction(shell, 'command-1')).toMatchObject({ type: 'command', id: 'command-1', command: 'pnpm test', status: 'failed', output: 'exit 1' });
  });

  it('turns build event names into status parts', () => {
    expect(normalizeBuildEvent('tests_started')).toEqual({ type: 'status', state: 'verifying', text: 'Tests Started' });
    expect(normalizeBuildEvent('worker_running')).toEqual({ type: 'status', state: 'working', text: 'Worker Running' });
  });
});
