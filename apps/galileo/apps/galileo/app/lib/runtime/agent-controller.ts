import type { Message } from 'ai';
import { decodeGalileoEvents, type AgentEvent } from './galileo-stream';
import { executeReadOnlyTool } from './tool-executor';

interface AgentControllerOptions {
  api?: string;
  /**
   * Static context or a getter re-evaluated before every model iteration so a
   * mid-turn `refresh_context` call is reflected in the next request.
   */
  projectContext?: string | (() => string);
  signal?: AbortSignal;
}

export async function* runAgentTurn(messages: Message[], options: AgentControllerOptions = {}): AsyncGenerator<AgentEvent> {
  const conversation = [...messages];
  const startedAt = Date.now();
  const repeatedCalls = new Map<string, number>();
  const maxIterations = 5;
  const maxDurationMs = 120_000;
  const maxToolOutput = 20_000;
  const tools = [
    {
      type: 'function' as const,
      function: {
        name: 'list',
        description: "List a directory's entries in the project workspace. Start here to discover the project's file and folder structure before reading anything.",
        parameters: { type: 'object', properties: { path: { type: 'string', description: 'Relative directory path; defaults to the workspace root "."' } } },
      },
    },
    {
      type: 'function' as const,
      function: {
        name: 'read',
        description: 'Read one file from the project workspace (output capped at 20k characters). Use it for manifests, entry points, and any file you must quote or modify.',
        parameters: { type: 'object', properties: { path: { type: 'string', description: 'Relative file path, e.g. "package.json"' } }, required: ['path'] },
      },
    },
    {
      type: 'function' as const,
      function: {
        name: 'search',
        description: 'Search file contents across the workspace for a text pattern (recursive grep, node_modules excluded). Use it to locate where a symbol, route, or error string lives.',
        parameters: { type: 'object', properties: { path: { type: 'string', description: 'Relative directory to search in; defaults to the workspace root' }, query: { type: 'string', description: 'Text to search for' } }, required: ['query'] },
      },
    },
    {
      type: 'function' as const,
      function: {
        name: 'refresh_context',
        description: 'Re-scan the workspace and rebuild the cached project context after files were created or changed. Call it once after finishing edits, not repeatedly.',
        parameters: { type: 'object', properties: {} },
      },
    },
  ];
  for (let turn = 0; turn < maxIterations; turn += 1) {
    if (options.signal?.aborted) throw new DOMException('Agent aborted', 'AbortError');
    if (Date.now() - startedAt >= maxDurationMs) {
      yield { type: 'error', code: 'tool_loop_timeout', message: 'Tool loop timed out', retryable: false };
      return;
    }
    const projectContext = typeof options.projectContext === 'function' ? options.projectContext() : options.projectContext || '';
    const response = await fetch(options.api || '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-Galileo-Protocol': 'events' },
      body: JSON.stringify({ messages: conversation, projectContext, tools }),
      signal: options.signal,
    });
    if (!response.ok || !response.body) throw new Error(`Galileo returned HTTP ${response.status}`);

    const calls = new Map<string, { name: string; arguments: Record<string, unknown> }>();
    let assistantText = '';
    for await (const event of decodeGalileoEvents(response.body)) {
      if (options.signal?.aborted) throw new DOMException('Agent aborted', 'AbortError');
      if (event.type === 'text.delta') {
        assistantText += event.delta;
        if (assistantText.length > maxToolOutput) {
          yield { type: 'error', code: 'agent_output_limit', message: 'Agent output exceeded its limit', retryable: false };
          return;
        }
      }
      yield event;
      if (event.type === 'tool.start') calls.set(event.id, { name: event.name, arguments: {} });
      if (event.type === 'tool.arguments' && calls.has(event.id) && typeof event.arguments === 'object' && event.arguments !== null) calls.get(event.id)!.arguments = event.arguments as Record<string, unknown>;
    }
    if (!calls.size) return;

    const assistantToolInvocations = [] as Array<{ toolCallId: string; toolName: string; args: Record<string, unknown> }>;
    for (const [id, call] of calls) {
      const signature = `${call.name}:${JSON.stringify(call.arguments)}`;
      const count = (repeatedCalls.get(signature) || 0) + 1;
      repeatedCalls.set(signature, count);
      if (count > 2) {
        yield { type: 'error', code: 'repeated_tool_call', message: `Tool call repeated too many times: ${call.name}`, retryable: false };
        return;
      }
      assistantToolInvocations.push({ toolCallId: id, toolName: call.name, args: call.arguments });
    }
    conversation.push({ id: crypto.randomUUID(), role: 'assistant', content: assistantText, toolInvocations: assistantToolInvocations } as Message);
    for (const [id, call] of calls) {
      if (options.signal?.aborted) throw new DOMException('Agent aborted', 'AbortError');
      if (Date.now() - startedAt >= maxDurationMs) {
        yield { type: 'error', code: 'tool_loop_timeout', message: 'Tool loop timed out', retryable: false };
        return;
      }
      const result = await executeReadOnlyTool(call.name, call.arguments, options.signal);
      const output = result.ok && typeof result.result === 'string' ? result.result.slice(0, maxToolOutput) : result.result;
      yield {
        type: 'tool.result',
        id,
        ok: result.ok,
        ...(result.ok ? { result: output } : { error: { code: 'tool_failed', message: result.error || 'Tool failed' } }),
      };
      conversation.push({ id: `${id}-result`, role: 'tool', content: JSON.stringify({ tool_call_id: id, ...(result.ok ? { result: output } : { error: { code: 'tool_failed', message: result.error || 'Tool failed' } }) }) });
    }
  }
  yield { type: 'error', code: 'tool_loop_limit', message: 'Tool loop exceeded its limit', retryable: false };
}
