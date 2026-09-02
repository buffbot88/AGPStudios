import { readFileSync } from 'node:fs';
import { cwd } from 'node:process';

export interface AgentEndpoint {
  id: string;
  url: string;
}

interface GalileoConfig {
  gateway?: {
    url?: string;
    api_key?: string;
  };
  agents?: AgentEndpoint[];
}

const DEFAULT_GATEWAY_URL = 'http://127.0.0.1:3000';
const DEFAULT_AGENTS: AgentEndpoint[] = [
  { id: 'omega', url: 'https://129.213.94.124' },
  { id: 'beta', url: 'https://150.136.208.93:8082' },
  { id: 'delta', url: 'https://129.213.147.225:8088' },
];

let cached: { gatewayURL: string; apiKey: string; agents: AgentEndpoint[] } | undefined;

/**
 * Loads config.json from the working directory once per process; a missing
 * file falls back to the built-in defaults while malformed JSON fails loudly.
 * Restart Galileo after editing the file.
 */
function loadConfig() {
  if (cached) {
    return cached;
  }

  const configPath = `${cwd().replace(/[\\/]+$/, '')}/config.json`;
  let fileConfig: GalileoConfig = {};

  try {
    fileConfig = JSON.parse(readFileSync(configPath, 'utf8')) as GalileoConfig;
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') {
      throw new Error(`Failed to read ${configPath}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const agents = (fileConfig.agents ?? []).filter((agent) => agent?.id && agent?.url);

  cached = {
    gatewayURL: process.env.ASHAT_GATEWAY_URL?.trim() || fileConfig.gateway?.url?.trim() || DEFAULT_GATEWAY_URL,
    apiKey: process.env.ASHAT_GATEWAY_API_KEY || fileConfig.gateway?.api_key || '',
    agents: agents.length > 0 ? agents : DEFAULT_AGENTS,
  };

  return cached;
}

export function getGatewayURL() {
  return loadConfig().gatewayURL;
}

export function getAPIKey() {
  return loadConfig().apiKey;
}

export function getAgents(): AgentEndpoint[] {
  return loadConfig().agents;
}
