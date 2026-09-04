import { webcontainer } from '~/lib/webcontainer';

const TOOLS = new Set(['list', 'read', 'search', 'refresh_context']);

function safePath(value: unknown) {
  const path = typeof value === 'string' ? value : '.';
  return path !== '' && !path.startsWith('/') && !path.split('/').includes('..') ? path : '.';
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

async function readOutput(output: ReadableStream<string>) {
  const reader = output.getReader();
  let result = '';
  for (;;) {
    const next = await reader.read();
    if (next.done) return result;
    result += next.value;
  }
}

export async function executeReadOnlyTool(name: string, args: Record<string, unknown>, signal?: AbortSignal) {
  if (!TOOLS.has(name)) return { ok: false, error: 'Tool is not permitted' };
  const aborted = () => new DOMException('Tool aborted', 'AbortError');
  try {
    const container = await webcontainer;
    if (signal?.aborted) throw aborted();
    const path = safePath(args.path);
    if (name === 'read') {
      const content = await container.fs.readFile(path);
      if (signal?.aborted) throw aborted();
      return { ok: true, result: `FILE: ${path}\n${content.slice(0, 20000)}` };
    }
    if (name === 'list') {
      if (signal?.aborted) throw aborted();
      return { ok: true, result: (await container.fs.readdir(path)).join('\n') };
    }
    if (name === 'search') {
      const query = typeof args.query === 'string' ? args.query : '';
      const process = await container.spawn('jsh', ['-c', `grep -RIn --exclude-dir=node_modules -- ${shellQuote(query)} ${shellQuote(path)}`]);
      const onAbort = () => { process.kill(); };
      signal?.addEventListener('abort', onAbort, { once: true });
      try {
        const result = await readOutput(process.output);
        if (signal?.aborted) throw aborted();
        return { ok: true, result: result.slice(0, 20000) };
      } finally {
        signal?.removeEventListener('abort', onAbort);
      }
    }
    return { ok: true, result: 'Project context refreshed from the active WebContainer.' };
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    return { ok: false, error: error instanceof Error ? error.message : 'Tool failed' };
  }
}
