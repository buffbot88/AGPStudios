import { beforeEach, describe, expect, it, vi } from 'vitest';
import { map } from 'nanostores';

type FileMap = Record<string, { type: 'file'; content: string; isBinary: boolean } | { type: 'folder' } | undefined>;

// Hermetic: a plain map stands in for workbenchStore.files — no WebContainer, no store side effects.
vi.mock('~/lib/stores/workbench', () => ({ workbenchStore: { files: map<FileMap>({}) } }));

import { workbenchStore } from '~/lib/stores/workbench';
import { ensureProjectContext, projectContextStore, setActiveProjectId } from './project-context';

function setFiles(files: FileMap) {
  workbenchStore.files.set(files);
}

function file(content: string, isBinary = false): { type: 'file'; content: string; isBinary: boolean } {
  return { type: 'file', content, isBinary };
}

/**
 * Node test environment has no window/localStorage — stub a minimal
 * in-memory shim; project-context only touches window.localStorage.
 */
const memoryStorage = new Map<string, string>();
vi.stubGlobal('window', {
  localStorage: {
    getItem: (key: string) => memoryStorage.get(key) ?? null,
    setItem: (key: string, value: string) => memoryStorage.set(key, value),
    clear: () => memoryStorage.clear(),
  },
});

describe('project context', () => {
  beforeEach(() => {
    memoryStorage.clear();
    setActiveProjectId('spec-project');
    projectContextStore.set({ state: 'idle', context: '' });
    setFiles({});
  });

  it('builds a tree plus key-file heads and caps at 15k characters', () => {
    setFiles({
      'package.json': file('{"name":"demo","scripts":{"dev":"vite"}}'),
      'README.md': file('# Demo'),
      'src/App.tsx': file('export default function App() { return null; }'),
      'src/deep/extra.ts': file('export const x = 1;'),
    });

    const context = ensureProjectContext('spec-project') as unknown as Promise<string>;

    return context.then((value) => {
      expect(value).toContain('FILE: package.json');
      expect(value).toContain('FILE: src/deep/extra.ts');
      expect(value).toContain('FILE CONTENT: package.json');
      expect(value.length).toBeLessThanOrEqual(15_000);
    });
  });

  it('serves the cache without rebuilding when the signature is unchanged', async () => {
    setFiles({ 'package.json': file('{"name":"demo"}') });
    const first = await ensureProjectContext('spec-project');

    // Rewrite the workspace with identical content lengths: same signature.
    setFiles({ 'package.json': file('{"name":"demo"}') });
    const spy = vi.spyOn(projectContextStore, 'set');
    const second = await ensureProjectContext('spec-project');

    expect(second).toBe(first);
    // Cache hits must not pass through the scanning state.
    expect(spy.mock.calls.every(([value]) => value.state !== 'scanning')).toBe(true);
    spy.mockRestore();
  });

  it('rebuilds when the workspace drifts and excludes node_modules and dotfiles', async () => {
    setFiles({ 'package.json': file('{"name":"demo"}') });
    const first = await ensureProjectContext('spec-project');

    setFiles({
      'package.json': file('{"name":"demo"}'),
      'node_modules/vite/index.js': file('module.exports = {}'),
      '.env': file('SECRET=1'),
      'src/new.ts': file('export const y = 2;'),
    });
    const second = await ensureProjectContext('spec-project');

    expect(second).not.toBe(first);
    expect(second).toContain('src/new.ts');
    expect(second).not.toContain('node_modules');
    expect(second).not.toContain('.env');
  });

  it('force rebuilds even when the signature matches', async () => {
    setFiles({ 'README.md': file('# Demo') });
    const first = await ensureProjectContext('spec-project');
    const second = await ensureProjectContext('spec-project', { force: true });

    expect(second).toBe(first);
    expect(projectContextStore.get().state).toBe('ready');
  });

  it('keeps binary and folder entries out of the tree', async () => {
    setFiles({
      'logo.png': file('binary-bytes', true),
      'src': { type: 'folder' },
      'src/main.tsx': file('render();'),
    });

    const context = await ensureProjectContext('spec-project');
    expect(context).toContain('src/main.tsx');
    expect(context).not.toContain('logo.png');
    expect(context).not.toMatch(/^FILE: src$/m);
  });
});
