import { createServer } from 'node:http';

/**
 * Local mock of the Alpha gateway (OpenAI-compatible /v1/chat/completions)
 * for end-to-end development without the Alpha host.
 *
 * Two dialects, matching stream-text.ts:
 * - Requests with `x-galileo-protocol: events` receive Galileo SSE events.
 *   The first turn of a conversation emits a `list` tool call; the second
 *   turn (after tool results arrive) streams a final answer containing a
 *   boltArtifact so the workbench, action runner, and message parser all
 *   execute for real.
 * - Plain requests (prompt enhancer) receive OpenAI-style `data:` chunks.
 */

const PORT = 9090;

function sse(controller, event) {
  controller.write(new TextEncoder().encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`));
}

function openaiChunk(controller, text) {
  const payload = { id: 'mock', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { content: text } }] };
  controller.write(new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`));
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const ARTIFACT = [
  '<boltArtifact id="hello-site" title="Hello site">',
  '<boltAction type="file" filePath="index.html">',
  '<!doctype html>\\n<html>\\n<head><title>Hello</title></head>\\n<body>\\n  <h1>Hello from the Galileo pipeline</h1>\\n</body>\\n</html>',
  '</boltAction>',
  '<boltAction type="shell">echo workspace-ready</boltAction>',
  '</boltArtifact>',
].join('\\n');

const server = createServer((request, response) => {
  // Alpha-compatible probes consumed by GET /api/status.
  if (request.method === 'GET' && request.url.startsWith('/health')) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ status: 'ok', version: 'mock-alpha' }));

    return;
  }

  if (request.method === 'GET' && request.url.startsWith('/status')) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(
      JSON.stringify({
        queued_requests: 0,
        max_queue: 8,
        active_requests: 0,
        available_liquid_slots: 1,
        available_vision_slots: 0,
      }),
    );

    return;
  }

  if (request.method === 'GET' && request.url.startsWith('/workers')) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(
      JSON.stringify({ liquid_backend_configured: true, liquid_backend_healthy: true, vision_worker_active: false }),
    );

    return;
  }

  if (request.method !== 'POST' || !request.url.includes('/v1/chat/completions')) {
    response.writeHead(404).end();
    return;
  }

  let body = '';
  request.on('data', (chunk) => (body += chunk));
  request.on('end', async () => {
    const hasToolResults = (() => {
      try {
        return (JSON.parse(body).messages || []).some((message) => message.role === 'tool');
      } catch {
        return false;
      }
    })();
    const events = request.headers['x-galileo-protocol'] === 'events';

    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });

    const controller = response;

    if (!events) {
      // Prompt enhancer: deterministic improved prompt.
      for (const piece of ['Create a tiny web page', ' that displays a bold Hello heading', ' and nothing else.']) {
        openaiChunk(controller, piece);
        await delay(40);
      }
      controller.write('data: [DONE]\n\n');
      controller.end();

      return;
    }

    if (!hasToolResults) {
      // First turn: inspect the project with the `list` tool.
      sse(controller, { type: 'response.start', response_id: 'mock-response' });
      await delay(30);
      sse(controller, { type: 'text.delta', delta: 'Let me inspect the project first. ' });
      await delay(30);
      sse(controller, { type: 'tool.start', id: 'call_list_1', name: 'list' });
      await delay(30);
      sse(controller, { type: 'tool.arguments', id: 'call_list_1', arguments: { path: '.' } });
      await delay(30);
      sse(controller, { type: 'response.complete' });
      controller.end();

      return;
    }

    // Second turn: stream the final answer with a workbench artifact.
    sse(controller, { type: 'response.start', response_id: 'mock-response-2' });

    for (const piece of [
      'The project is ready. Here is a hello page:\\n\\n',
      ARTIFACT,
      '\\n\\nOpen the workbench to see the file and shell action run.',
    ]) {
      sse(controller, { type: 'text.delta', delta: piece });
      await delay(50);
    }
    sse(controller, { type: 'response.complete' });
    controller.end();
  });
});

server.listen(PORT, () => {
  console.log(`[mock-gateway] listening on http://127.0.0.1:${PORT}`);
});
