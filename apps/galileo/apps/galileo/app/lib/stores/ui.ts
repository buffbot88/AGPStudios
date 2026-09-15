import { atom, map } from 'nanostores';

export type RailTab = 'project' | 'agent' | 'files' | 'session';

export const sidebarOpen = atom(true);

export const railOpen = atom(true);

export const railTab = atom<RailTab>('project');

export const workbenchOpen = atom(false);

export const autoEnhance = map({
  enabled: false,
});
