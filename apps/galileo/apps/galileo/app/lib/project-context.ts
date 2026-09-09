import { atom } from 'nanostores';
import { workbenchStore } from '~/lib/stores/workbench';
import { createScopedLogger } from '~/utils/logger';

const logger = createScopedLogger('ProjectContext');

const CONTEXT_KEY_PREFIX = 'galileo_project_ctx:';
const MAX_CONTEXT_CHARS = 15_000; // stream-text.ts injects <bootstrap_context> sliced to 15k — a larger cache is dead weight.
const MAX_TREE_ENTRIES = 400;
const HEAD_FILES = /^(package\.json|README(\.\w+)?|index\.html|src\/(main|index|app|App|entry\.[a-z]+)\.[tj]sx?|vite\.config\.[tj]s|tsconfig\.json|astro\.config\.[tj]s|next\.config\.[tj]s)$/i;

interface ContextEntry {
  context: string;
  sig: string;
}

export interface ProjectContextState {
  state: 'idle' | 'scanning' | 'ready' | 'error';
  context: string;
}

export const projectContextStore = atom<ProjectContextState>({ state: 'idle', context: '' });

let activeProjectId: string | undefined;

/** Capture the real project id once per mount; the URL rots to /chat/<chatId> after the first artifact. */
export function setActiveProjectId(projectId: string) {
  activeProjectId = projectId;
}

export function currentProjectId(): string {
  return activeProjectId || 'default';
}

function cacheKey(projectId: string) {
  return `${CONTEXT_KEY_PREFIX}${projectId}`;
}

function loadCache(projectId: string): ContextEntry | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    const raw = window.localStorage.getItem(cacheKey(projectId));
    return raw ? (JSON.parse(raw) as ContextEntry) : undefined;
  } catch {
    return undefined;
  }
}

function saveCache(projectId: string, entry: ContextEntry) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(cacheKey(projectId), JSON.stringify(entry));
  } catch (error) {
    // Non-fatal: the in-memory context still serves this session.
    logger.warn('Unable to cache project context', error);
  }
}

/**
 * Cheap workspace fingerprint: text file count + total content length.
 * node_modules and dot-config churn excluded — install cycles must not force a rebuild.
 */
function workspaceSignature(files: Record<string, { type: string; content?: string; isBinary?: boolean } | undefined>) {
  let count = 0;
  let total = 0;
  for (const [filePath, file] of Object.entries(files)) {
    if (!file || file.type !== 'file' || file.isBinary) continue;
    if (filePath.includes('node_modules') || filePath.split('/').some((segment) => segment.startsWith('.'))) continue;
    count += 1;
    total += file.content?.length ?? 0;
  }
  return `${count}:${total}`;
}

function buildContext(files: Record<string, { type: string; content?: string; isBinary?: boolean } | undefined>) {
  const textFiles = Object.entries(files)
    .filter(([filePath, file]) => file?.type === 'file' && !file.isBinary && !filePath.includes('node_modules') && !filePath.split('/').some((segment) => segment.startsWith('.')))
    .map(([filePath, file]) => [filePath, file!.content ?? ''] as const)
    .sort(([a], [b]) => a.localeCompare(b));

  const tree = textFiles.slice(0, MAX_TREE_ENTRIES).map(([filePath]) => `FILE: ${filePath}`).join('\n');
  const sections: string[] = [tree];

  let budget = MAX_CONTEXT_CHARS - tree.length - 2;
  for (const [filePath, content] of textFiles) {
    if (!HEAD_FILES.test(filePath) || budget <= 0) continue;
    const head = `FILE CONTENT: ${filePath}\n${content.slice(0, Math.min(budget, 12_000))}`;
    sections.push(head);
    budget -= head.length + 2;
    if (budget <= 0) break;
  }

  return sections.filter(Boolean).join('\n\n').slice(0, MAX_CONTEXT_CHARS);
}

/**
 * Build (or serve cached) project context for a project. Signature-matched
 * caches are served instantly; otherwise the workspace snapshot is scanned
 * into a file tree plus heads of key manifests and cached by project id.
 */
export async function ensureProjectContext(projectId: string = currentProjectId(), options: { force?: boolean; files?: Record<string, { type: string; content?: string; isBinary?: boolean }> } = {}) {
  const files = options.files ?? workbenchStore.files.get();
  const sig = workspaceSignature(files);

  if (!options.force) {
    const cached = loadCache(projectId);
    if (cached && cached.sig === sig && cached.context) {
      projectContextStore.set({ state: 'ready', context: cached.context });
      return cached.context;
    }
  }

  projectContextStore.set({ state: 'scanning', context: projectContextStore.get().context });
  try {
    const context = buildContext(files);
    saveCache(projectId, { context, sig });
    projectContextStore.set({ state: 'ready', context });
    return context;
  } catch (error) {
    logger.error('Project context build failed', error);
    projectContextStore.set({ state: 'error', context: '' });
    return '';
  }
}

/** Rebuild the cache for the active project (the `refresh_context` tool body). */
export async function refreshProjectContext() {
  return ensureProjectContext(currentProjectId(), { force: true });
}
