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

export async function executeReadOnlyTool(name: string, args: Record<string, unknown>) {
  if (!TOOLS.has(name)) return { ok: false, error: 'Tool is not permitted' };
  try {
    const container = await webcontainer;
    const path = safePath(args.path);
    if (name === 'read') return { ok: true, result: `FILE: ${path}\n${(await container.fs.readFile(path)).slice(0, 20000)}` };
    if (name === 'list') return { ok: true, result: (await container.fs.readdir(path)).join('\n') };
    if (name === 'search') {
      const query = typeof args.query === 'string' ? args.query : '';
      const output = await container.spawn('jsh', ['-c', `grep -RIn --exclude-dir=node_modules -- ${shellQuote(query)} ${shellQuote(path)}`]);
      return { ok: true, result: (await readOutput(output.output)).slice(0, 20000) };
    }
    return { ok: true, result: 'Project context refreshed from the active WebContainer.' };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : 'Tool failed' };
  }
}
